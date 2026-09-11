from __future__ import annotations

import logging
import signal
import time
from types import FrameType

from .config import Settings, get_settings
from .db import Database
from .ocr import OcrEngine, build_engine
from .repository import JobRepository
from .storage import delete_pdf, purge_expired_jobs

logger = logging.getLogger("papela.worker")

_running = True


def _handle_signal(signum: int, _frame: FrameType | None) -> None:
    global _running
    logger.info("worker.stopping", extra={"request_id": None})
    _running = False


def process_one(repo: JobRepository, engine: OcrEngine, settings: Settings) -> bool:
    """Claim and process a single job. Returns True if work was done."""
    job = repo.claim_next()
    if job is None:
        return False

    job_id = str(job["id"])
    logger.info("job.claimed", extra={"request_id": job_id})
    try:
        result = engine.extract(job["storage_path"])
        purge_now = settings.should_purge_after_done
        if purge_now:
            # Data minimization: drop the raw PDF the moment we no longer need
            # it, in the SAME step we persist the extracted text, so there is
            # no window where a done job still has a sensitive file on disk.
            delete_pdf(job_id, settings.storage_dir, reason="purge-after-done")
        repo.mark_done(job_id, result, purged=purge_now)
        logger.info("job.done", extra={"request_id": job_id})
    except Exception as exc:  # noqa: BLE001 - isolate per-job failure
        logger.exception("job.failed", extra={"request_id": job_id})
        repo.mark_failed(job_id, str(exc), max_attempts=settings.max_attempts)
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
            # Periodic LGPD retention sweep (belt-and-suspenders: even if
            # purge-after-done is off, nothing outlives retention_days).
            if now - last_sweep >= settings.purge_interval_s:
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
