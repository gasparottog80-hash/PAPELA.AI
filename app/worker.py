from __future__ import annotations

import logging
import signal
import time
from pathlib import Path
from types import FrameType

from .config import Settings, get_settings
from .db import Database
from .processing import bounded_process
from .repository import JobRepository
from .sanitize import sanitize_error
from .storage import delete_pdf, get_pdf_path, purge_expired_jobs

logger = logging.getLogger("papela.worker")

_running = True


def _handle_signal(signum: int, _frame: FrameType | None) -> None:
    global _running
    logger.info("worker.stopping", extra={"request_id": None})
    _running = False


def process_one(repo: JobRepository, settings: Settings) -> bool:
    """Claim and process a single job. Returns True if work was done.

    Ordering is crash-safe: the extracted result is persisted (mark_done)
    BEFORE the raw PDF is deleted. So a crash can at worst leave an orphaned
    PDF (cleaned by the retention sweep) — never a purged file with no result.
    """
    job = repo.claim_next()
    if job is None:
        return False

    job_id = str(job["id"])
    logger.info("job.claimed", extra={"request_id": job_id})
    try:
        path = get_pdf_path(job_id, settings.storage_dir)
        if Path(job["storage_path"]).resolve() != Path(path):
            raise ValueError("unsafe job storage path")
        result = bounded_process("ocr", path, settings)
        # 1) Persist the result first (data safety).
        repo.mark_done(job_id, result)
        # 2) Then drop the raw PDF (data minimization). If we crash between
        #    (1) and (2) the job is already done; the retention sweep reclaims
        #    the orphan file later. No sensitive-doc-without-result window.
        if settings.purge_after_done:
            delete_pdf(job_id, settings.storage_dir, reason="purge-after-done")
            repo.mark_purged(job_id)
        logger.info("job.done", extra={"request_id": job_id})
    except Exception as exc:  # noqa: BLE001 - isolate per-job failure
        # LGPD: log the sanitized category, never the raw exception (may carry
        # document content). exc_info is intentionally omitted here.
        logger.error(
            "job.failed",
            extra={"request_id": job_id, "error": sanitize_error(exc)},
        )
        # A cleanup failure after mark_done must not requeue an already persisted
        # result. The retention sweep will retry deletion with purged_at still NULL.
        row = repo.get_internal(job_id)
        if row is not None and row["status"] == "processing":
            repo.mark_failed(
                job_id, sanitize_error(exc), max_attempts=settings.max_attempts
            )
            row = repo.get_internal(job_id)
        if row is not None and row["status"] == "failed":
            delete_pdf(job_id, settings.storage_dir, reason="terminal-failure")
            repo.mark_purged(job_id)
    return True


def run() -> None:
    settings = get_settings()
    from .logging_config import configure_logging

    configure_logging(settings.log_level)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    db = Database(settings)
    db.open()
    repo = JobRepository(db.pool)
    logger.info("worker.started", extra={"request_id": None})

    idle_backoff = 0.5
    last_sweep = 0.0
    heartbeat = Path("/tmp/papela-worker-heartbeat")
    try:
        while _running:
            # The container probe detects a stalled loop, not just a live PID.
            heartbeat.touch()
            now = time.monotonic()
            # Periodic maintenance: reap stalled jobs + LGPD retention sweep.
            if now - last_sweep >= settings.purge_interval_s:
                try:
                    reaped = repo.reap_stalled_jobs(
                        stall_timeout_s=settings.stall_timeout_s,
                        max_attempts=settings.max_attempts,
                    )
                    if reaped:
                        logger.warning("jobs.reaped", extra={"request_id": None})
                except Exception:  # noqa: BLE001 - maintenance must not kill worker
                    logger.exception("reaper.failed", extra={"request_id": None})
                try:
                    n = purge_expired_jobs(
                        repo,
                        settings.storage_dir,
                        retention_days=settings.retention_days,
                    )
                    if n:
                        logger.info("retention.purged", extra={"request_id": None})
                except Exception:  # noqa: BLE001 - sweep must never kill worker
                    logger.exception(
                        "retention.sweep_failed", extra={"request_id": None}
                    )
                last_sweep = now

            did_work = process_one(repo, settings)
            if not did_work:
                time.sleep(idle_backoff)
    finally:
        db.close()
        logger.info("worker.stopped", extra={"request_id": None})


if __name__ == "__main__":
    run()
