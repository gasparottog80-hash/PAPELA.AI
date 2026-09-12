from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration, loaded from environment / .env.

    Conservative defaults: assume prod / sensitive data (LGPD) unless overridden.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="PAPELA_", extra="ignore"
    )

    env: str = "production"
    log_level: str = "INFO"
    service_name: str = "papela-ai"

    # --- Postgres (also used as the job queue via SKIP LOCKED) ---
    database_url: str = "postgresql://papela:papela@localhost:5433/papela"
    db_pool_min: int = 1
    db_pool_max: int = 10

    # --- Upload / storage (on-premise disk, no cloud) ---
    storage_dir: str = "./data/pdfs"
    max_upload_bytes: int = 20 * 1024 * 1024  # 20 MiB
    max_pdf_pages: int = 100

    # --- Auth ---
    # Comma-separated API keys. Empty in prod => app refuses to start (fail-closed).
    api_keys: str = ""

    # --- Rate limiting (per API key, fixed window, in-process) ---
    rate_limit_per_min: int = 60

    # --- OCR ---
    ocr_engine: str = "fake"  # "fake" | "paddle"
    ocr_lang: str = "pt"
    max_attempts: int = 3

    # --- LGPD retention / minimization ---
    # Delete the raw PDF from disk right after a successful OCR. Fail-safe
    # default is ON: a product handling sensitive fiscal documents must never
    # leave them on disk by accident. Opt OUT explicitly
    # (PAPELA_PURGE_AFTER_DONE=false) only in dev when you must inspect the file.
    purge_after_done: bool = True
    # Retention for done jobs' raw PDFs; the periodic sweep deletes files
    # older than this. The extracted JSON in Postgres is NOT deleted here.
    retention_days: int = 7
    # How often the worker runs the retention sweep + stalled-job reaper.
    purge_interval_s: int = 3600
    # A job stuck in 'processing' longer than this (worker crashed between
    # claim and mark_done/failed) is requeued (or failed) by the reaper.
    stall_timeout_s: int = 300

    @property
    def api_key_set(self) -> frozenset[str]:
        return frozenset(k.strip() for k in self.api_keys.split(",") if k.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
