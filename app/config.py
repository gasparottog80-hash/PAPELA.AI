from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

QUARANTINED_LEGACY_TENANT_ID = "00000000-0000-0000-0000-000000000001"
DEVELOPMENT_LEGACY_TENANT_ID = "00000000-0000-0000-0000-000000000002"


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
    erasure_dir: str = "./data/erasures"
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
    # JSON object: immutable tenant UUID -> one API key. Production requires this
    # mapping; do not derive ownership from the key or from a client-supplied ID.
    tenant_api_keys: dict[str, str] = Field(default_factory=dict)
    # Deprecated single-key development/test compatibility only. Production
    # refuses it because historical shared keys cannot prove customer ownership.
    api_keys: str = ""

    # --- Rate limiting (per API key, fixed window, in-process) ---
    rate_limit_per_min: int = Field(default=60, gt=0)

    # --- OCR ---
    # "fake"  = dev/CI, no model download
    # "text"  = digital PDFs with an embedded text layer; no native OCR extra
    # "paddle" = text-only PaddleOCR (on-prem)
    # "ppstructure" = layout + table recognition (on-prem, structured tables)
    ocr_engine: Literal["fake", "text", "paddle", "ppstructure"] = "fake"
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
    # Technical default, not a contractual or legal retention period.
    job_retention_days: int = Field(default=30, ge=1)
    # How often the worker runs the retention sweep + stalled-job reaper.
    purge_interval_s: int = Field(default=3600, gt=0)
    # A job stuck in 'processing' longer than this (worker crashed between
    # claim and mark_done/failed) is requeued (or failed) by the reaper.
    stall_timeout_s: int = Field(default=300, gt=0)

    @model_validator(mode="after")
    def ensure_deadline_precedes_reaper(self) -> Settings:
        if self.stall_timeout_s < self.processing_timeout_s + 30:
            raise ValueError("stall timeout must exceed processing deadline")
        seen_keys: set[str] = set()
        for tenant_id, key in self.tenant_api_keys.items():
            try:
                canonical_id = str(UUID(tenant_id))
            except ValueError as exc:
                raise ValueError("tenant IDs must be UUIDs") from exc
            if tenant_id != canonical_id or tenant_id == QUARANTINED_LEGACY_TENANT_ID:
                raise ValueError("tenant ID is non-canonical or reserved")
            if not key or len(key) > 512 or not key.isascii() or key != key.strip():
                raise ValueError("tenant API key has invalid format")
            if key in seen_keys:
                raise ValueError("tenant API keys must be unique")
            seen_keys.add(key)
        if len(self.api_key_set) > 1:
            raise ValueError("legacy API keys must contain at most one key")
        if seen_keys.intersection(self.api_key_set):
            raise ValueError("tenant API keys must be unique")
        return self

    @property
    def api_key_set(self) -> frozenset[str]:
        return frozenset(k.strip() for k in self.api_keys.split(",") if k.strip())

    @property
    def tenant_key_pairs(self) -> tuple[tuple[str, str], ...]:
        pairs = list(self.tenant_api_keys.items())
        if self.env != "production" and self.api_key_set:
            pairs.append((DEVELOPMENT_LEGACY_TENANT_ID, next(iter(self.api_key_set))))
        return tuple(pairs)


@lru_cache
def get_settings() -> Settings:
    # Compose grants only the required /run/secrets files to each service.
    # Keep development/test behavior unchanged; production may also use
    # explicit environment variables supplied by a secret manager.
    if os.environ.get("PAPELA_ENV", "production") == "production":
        # BaseSettings accepts this runtime-only override, but its generated
        # constructor signature does not expose the private settings kwargs.
        return Settings(_secrets_dir="/run/secrets")  # type: ignore[call-arg]
    return Settings()
