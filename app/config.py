from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration, loaded from environment / .env.

    Conservative defaults: assume sensitive-data / prod unless overridden.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="PAPELA_", extra="ignore"
    )

    env: str = "production"
    log_level: str = "INFO"
    service_name: str = "papela-ai"
    # Agent limits (defensive defaults against abuse / runaway cost).
    max_prompt_chars: int = 8_000
    request_timeout_s: float = 30.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
