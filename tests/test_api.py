from __future__ import annotations

import io
import os

import psycopg
import pytest
from fastapi.testclient import TestClient

# Test config MUST be set before app import (settings are cached).
os.environ.setdefault("PAPELA_ENV", "test")
os.environ.setdefault(
    "PAPELA_DATABASE_URL", "postgresql://papela:papela@localhost:5433/papela"
)
os.environ.setdefault("PAPELA_API_KEYS", "test-key")
os.environ.setdefault("PAPELA_OCR_ENGINE", "fake")
os.environ.setdefault("PAPELA_STORAGE_DIR", "./data/test-uploads")

from app.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402
from app.ocr import build_engine  # noqa: E402
from app.repository import JobRepository  # noqa: E402
from app.worker import process_one  # noqa: E402

AUTH = {"X-API-Key": "test-key"}


def _minimal_pdf() -> bytes:
    """A tiny but valid single-page PDF (pypdf can parse it)."""
    return (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
        b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n"
        b"0000000052 00000 n \n0000000101 00000 n \n"
        b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n164\n%%EOF\n"
    )


def _db_available() -> bool:
    try:
        with psycopg.connect(get_settings().database_url, connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_available(), reason="Postgres not reachable on PAPELA_DATABASE_URL"
)


@pytest.fixture()
def client():
    with TestClient(app) as c:
        # Clean slate for deterministic assertions.
        with psycopg.connect(get_settings().database_url, autocommit=True) as conn:
            conn.execute("TRUNCATE jobs")
        yield c


def test_upload_and_process_end_to_end(client: TestClient):
    # 1) SUCCESS: upload accepted -> pending
    r = client.post(
        "/v1/upload",
        headers=AUTH,
        files={"file": ("nota.pdf", io.BytesIO(_minimal_pdf()), "application/pdf")},
    )
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    assert r.json()["status"] == "pending"

    # 2) Drive the worker inline (fake OCR) -> done
    s = get_settings()
    repo = JobRepository(app.state.db.pool)
    engine = build_engine(s.ocr_engine, s.ocr_lang)
    assert process_one(repo, engine, s) is True

    r = client.get(f"/v1/jobs/{job_id}", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert body["result"]["engine"] == "fake"
    assert body["result"]["page_count"] == 1


def test_upload_requires_api_key(client: TestClient):
    # EXPECTED FAILURE: fail-closed auth rejects missing key.
    r = client.post(
        "/v1/upload",
        files={"file": ("x.pdf", io.BytesIO(_minimal_pdf()), "application/pdf")},
    )
    assert r.status_code == 401


def test_upload_rejects_non_pdf(client: TestClient):
    # CRITICAL EDGE: bad magic bytes must be rejected before any job is created.
    r = client.post(
        "/v1/upload",
        headers=AUTH,
        files={"file": ("evil.pdf", io.BytesIO(b"GIF89a not a pdf"), "application/pdf")},
    )
    assert r.status_code == 400
    assert "PDF" in r.json()["error"]
