from __future__ import annotations

import logging
import signal
import time
from types import FrameType

from .config import Settings, get_settings
from .db import Database
from .extract import extract_fields
from .ocr import OcrEngine, build_engine
from .repository import JobRepository
from .sanitize import sanitize_error
from .storage import delete_pdf, purge_expired_jobs

logger = logging.getLogger("papela.worker")

_running = True


def _handle_signal(signum: int, _frame: FrameType | None) -> None:
    global _running
    logger.info("worker.stopping", extra={"request_id": None})
    _running = False


def process_one(repo: JobRepository, engine: OcrEngine, settings: Settings) -> bool:
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
        result = engine.extract(job["storage_path"])
        # Deterministic fiscal-field extraction over the OCR payload (text +
        # structured tables). Additive: attaches result["fields"]. extract_fields
        # never raises, so a bad extraction degrades to nulls instead of failing
        # an otherwise-good OCR job.
        result["fields"] = extract_fields(result)
        purge_now = settings.purge_after_done
        # 1) Persist the result first (data safety).
        repo.mark_done(job_id, result, purged=purge_now)
        # 2) Then drop the raw PDF (data minimization). If we crash between
        #    (1) and (2) the job is already done; the retention sweep reclaims
        #    the orphan file later. No sensitive-doc-without-result window.
        if purge_now:
            delete_pdf(job_id, settings.storage_dir, reason="purge-after-done")
        logger.info("job.done", extra={"request_id": job_id})
    except Exception as exc:  # noqa: BLE001 - isolate per-job failure
        # LGPD: log the sanitized category, never the raw exception (may carry
        # document content). exc_info is intentionally omitted here.
        logger.error(
            "job.failed",
            extra={"request_id": job_id, "error": sanitize_error(exc)},
        )
        repo.mark_failed(job_id, sanitize_error(exc), max_attempts=settings.max_attempts)
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
    engine = build_engine(settings.ocr_engine, settings.ocr_lang)
    logger.info("worker.started", extra={"request_id": None})

    idle_backoff = 0.5
    last_sweep = 0.0
    try:
        while _running:
            now = time.monotonic()
            # Periodic maintenance: reap stalled jobs + LGPD retention sweep.
            if now - last_sweep >= settings.purge_interval_s:
                try:
                    reaped = repo.reap_stalled_jobs(
                        stall_timeout_s=settings.stall_timeout_s,
                        max_attempts=settings.max_attempts,
                    )
                    if reaped:
                        logger.warning(
                            "jobs.reaped", extra={"request_id": None}
                        )
                except Exception:  # noqa: BLE001 - maintenance must not kill worker
                    logger.exception("reaper.failed", extra={"request_id": None})
                try:
                    n = purge_expired_jobs(
                        repo, settings.storage_dir,
                        retention_days=settings.retention_days,
                    )
                    if n:
                        logger.info("retention.purged", extra={"request_id": None})
                except Exception:  # noqa: BLE001 - sweep must never kill worker
                    logger.exception("retention.sweep_failed", extra={"request_id": None})
                last_sweep = now

            did_work = process_one(repo, engine, settings)
            if not did_work:
                time.sleep(idle_backoff)
    finally:
        db.close()
        logger.info("worker.stopped", extra={"request_id": None})


if __name__ == "__main__":
    run()
