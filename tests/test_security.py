from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import DEVELOPMENT_LEGACY_TENANT_ID, Settings, get_settings
from app.http_security import SecurityBoundary
from app.logging_config import JsonFormatter
from app.security import RateLimiter, tenant_for_api_key, valid_api_key
from app.storage import get_pdf_path


@pytest.mark.parametrize("key", [None, "", "wrong", "test-key ", "é", "x" * 513])
def test_invalid_key(key):
    assert not valid_api_key(key)


def test_no_configured_keys_fail_closed(monkeypatch):
    monkeypatch.setattr(get_settings(), "api_keys", "")
    assert not valid_api_key("test-key")


def test_valid_keys_resolve_distinct_owners_without_returning_credentials():
    assert tenant_for_api_key("test-key") == DEVELOPMENT_LEGACY_TENANT_ID
    assert tenant_for_api_key("gate1-synthetic-b-key") == (
        "22222222-2222-4222-8222-222222222222"
    )


def test_production_settings_load_mounted_secret_files(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPELA_ENV", "production")
    monkeypatch.setenv("PAPELA_API_KEYS", "")
    monkeypatch.delenv("PAPELA_DATABASE_URL", raising=False)
    monkeypatch.delenv("PAPELA_TENANT_API_KEYS", raising=False)
    (tmp_path / "PAPELA_DATABASE_URL").write_text(
        "postgresql://papela_runtime:synthetic-secret@postgres:5432/papela"
    )
    (tmp_path / "PAPELA_TENANT_API_KEYS").write_text(
        '{"22222222-2222-4222-8222-222222222222":"synthetic-key"}'
    )
    settings = Settings(_secrets_dir=tmp_path, _env_file=None)
    assert settings.database_url.startswith("postgresql://papela_runtime:")
    assert len(settings.tenant_api_keys) == 1


@pytest.mark.parametrize(
    "mapping",
    [
        {"00000000-0000-0000-0000-000000000001": "different-key"},
        {"not-a-uuid": "different-key"},
        {
            "22222222-2222-4222-8222-222222222222": "same-key",
            "33333333-3333-4333-8333-333333333333": "same-key",
        },
    ],
)
def test_invalid_tenant_mapping_fails_closed(mapping):
    with pytest.raises(ValidationError):
        Settings.model_validate(
            get_settings().model_dump() | {"tenant_api_keys": mapping}
        )


@pytest.mark.parametrize("job_id", ["../secret", "/etc/passwd", "not-uuid"])
def test_storage_rejects_untrusted_id(tmp_path, job_id):
    with pytest.raises(ValueError):
        get_pdf_path(job_id, str(tmp_path))


def test_logs_do_not_render_exception_or_interpolated_payload():
    record = logging.LogRecord(
        "papela.test",
        logging.ERROR,
        "private",
        1,
        "failed %s",
        ("SECRET CPF 12345678901",),
        None,
    )
    exc = RuntimeError("SECRET /private/nota.pdf SQL password")
    record.exc_info = (RuntimeError, exc, None)
    text = JsonFormatter().format(record)
    assert "SECRET" not in text and "/private" not in text
    assert json.loads(text)["error"].startswith("pdf_encrypted:")


def _scope(headers):
    return {
        "type": "http",
        "path": "/v1/upload",
        "method": "POST",
        "headers": headers,
        "query_string": b"",
        "app": SimpleNamespace(state=SimpleNamespace(rate_limiter=RateLimiter(60))),
    }


def test_auth_happens_before_any_body_read():
    async def exercise_real():
        messages = []

        async def forbidden(*args):
            raise AssertionError("body must not be consumed")

        async def send(message):
            messages.append(message)

        await SecurityBoundary(forbidden)(_scope([]), forbidden, send)
        assert messages[0]["status"] == 401

    asyncio.run(exercise_real())


@pytest.mark.parametrize(
    "mode,expected",
    [("chunked", 413), ("slow", 408), ("length", 400), ("huge-length", 400)],
)
def test_bounded_receive_without_trusting_content_length(monkeypatch, mode, expected):
    monkeypatch.setattr(get_settings(), "max_upload_bytes", 100)
    monkeypatch.setattr(get_settings(), "upload_timeout_s", 0.01)

    async def exercise():
        messages = []
        headers = [(b"x-api-key", b"test-key")]
        if mode == "length":
            headers.append((b"content-length", b"invalid"))
        if mode == "huge-length":
            headers.append((b"content-length", b"9" * 10000))

        async def receive():
            if mode == "slow":
                await asyncio.sleep(1)
            return {"type": "http.request", "body": b"x" * 70000, "more_body": True}

        async def forbidden(*args):
            raise AssertionError("oversized body must not reach parser")

        async def send(message):
            messages.append(message)

        await SecurityBoundary(forbidden)(_scope(headers), receive, send)
        assert messages[0]["status"] == expected

    asyncio.run(exercise())
