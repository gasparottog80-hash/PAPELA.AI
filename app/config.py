from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration, loaded from environment / .env.

    Conservative defaults: assume prod / sensitive data (LGPD) unless overridden.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="PAPELA_", extra="ignore"
    )

    env: Literal["production", "development", "test"] = "production"
    log_level: str = "INFO"
    service_name: str = "papela-ai"

    # --- Postgres (also used as the job queue via SKIP LOCKED) ---
    database_url: str = "postgresql://papela:papela@localhost:5433/papela"
    db_pool_min: int = 1
    db_pool_max: int = 10

    # --- Upload / storage (on-premise disk, no cloud) ---
    storage_dir: str = "./data/pdfs"
    max_upload_bytes: int = Field(default=20 * 1024 * 1024, gt=0, le=20 * 1024 * 1024)
    max_pdf_pages: int = Field(default=100, gt=0, le=100)
    max_storage_bytes: int = Field(default=200 * 1024 * 1024, gt=0)
    upload_timeout_s: float = Field(default=30, gt=0, le=120)
    pdf_timeout_s: float = Field(default=5, gt=0, le=30)
    processing_timeout_s: float = Field(default=60, gt=0, le=120)
    max_result_bytes: int = Field(default=8 * 1024 * 1024, gt=0)
    processing_memory_bytes: int = Field(default=1024 * 1024 * 1024, gt=0)
    max_concurrent_uploads: int = Field(default=2, gt=0, le=8)
    allowed_hosts: str = "localhost,127.0.0.1,testserver"

    # --- Auth ---
    # Comma-separated API keys. Empty in prod => app refuses to start (fail-closed).
    api_keys: str = ""

    # --- Rate limiting (per API key, fixed window, in-process) ---
    rate_limit_per_min: int = Field(default=60, gt=0)

    # --- OCR ---
    # "fake"  = dev/CI, no model download
    # "paddle" = text-only PaddleOCR (on-prem)
    # "ppstructure" = layout + table recognition (on-prem, structured tables)
    ocr_engine: str = "fake"  # "fake" | "paddle" | "ppstructure"
    ocr_lang: str = "pt"
    max_attempts: int = Field(default=3, gt=0, le=10)

    # --- LGPD retention / minimization ---
    # Delete the raw PDF from disk right after a successful OCR. Fail-safe
    # default is ON: a product handling sensitive fiscal documents must never
    # leave them on disk by accident. Opt OUT explicitly
    # (PAPELA_PURGE_AFTER_DONE=false) only in dev when you must inspect the file.
    purge_after_done: bool = True
    # Retention for done jobs' raw PDFs; the periodic sweep deletes files
    # older than this. The extracted JSON in Postgres is NOT deleted here.
    retention_days: int = Field(default=7, ge=0)
    # How often the worker runs the retention sweep + stalled-job reaper.
    purge_interval_s: int = Field(default=3600, gt=0)
    # A job stuck in 'processing' longer than this (worker crashed between
    # claim and mark_done/failed) is requeued (or failed) by the reaper.
    stall_timeout_s: int = Field(default=300, gt=0)

    @model_validator(mode="after")
    def ensure_deadline_precedes_reaper(self) -> Settings:
        if self.stall_timeout_s < self.processing_timeout_s + 30:
            raise ValueError("stall timeout must exceed processing deadline")
        return self

    @property
    def api_key_set(self) -> frozenset[str]:
        return frozenset(k.strip() for k in self.api_keys.split(",") if k.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
