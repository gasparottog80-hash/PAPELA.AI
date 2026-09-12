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
# Tests opt OUT of purge-after-done so retention/audit paths can inspect the
# file; the dedicated purge test re-enables it via Settings.model_copy.
os.environ.setdefault("PAPELA_PURGE_AFTER_DONE", "false")

from app.config import Settings, get_settings  # noqa: E402
from app.main import app  # noqa: E402
from app.ocr import build_engine  # noqa: E402
from app.repository import JobRepository  # noqa: E402
from app.storage import pdf_exists, purge_expired_jobs  # noqa: E402
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


def _upload(client: TestClient) -> str:
    r = client.post(
        "/v1/upload",
        headers=AUTH,
        files={"file": ("nota.pdf", io.BytesIO(_minimal_pdf()), "application/pdf")},
    )
    assert r.status_code == 202, r.text
    return r.json()["job_id"]


def test_purge_after_done_deletes_pdf_and_stamps_audit(client: TestClient):
    # LGPD data minimization: with purge_after_done, the raw PDF must be gone
    # from disk the moment the job is done, and the audit must reflect it.
    s = get_settings()
    job_id = _upload(client)
    assert pdf_exists(job_id, s.storage_dir) is True

    purge_settings = s.model_copy(update={"purge_after_done": True})
    repo = JobRepository(app.state.db.pool)
    engine = build_engine(s.ocr_engine, s.ocr_lang)
    assert process_one(repo, engine, purge_settings) is True

    assert pdf_exists(job_id, s.storage_dir) is False
    audit = client.get(f"/v1/jobs/{job_id}/audit", headers=AUTH).json()
    assert audit["file_exists"] is False
    assert audit["purged_at"] is not None
    # Extracted JSON survives in Postgres even after the PDF is gone.
    assert client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()["status"] == "done"


def test_retention_sweep_purges_expired_only(client: TestClient):
    # CRITICAL EDGE: sweep must purge done jobs past retention and leave
    # in-retention ones untouched.
    s = get_settings()
    job_id = _upload(client)
    repo = JobRepository(app.state.db.pool)
    engine = build_engine(s.ocr_engine, s.ocr_lang)
    process_one(repo, engine, s)  # done, PDF kept (test env => no purge)
    assert pdf_exists(job_id, s.storage_dir) is True

    # retention_days=0 => everything done is expired.
    n = purge_expired_jobs(repo, s.storage_dir, retention_days=0)
    assert n == 1
    assert pdf_exists(job_id, s.storage_dir) is False
    # Idempotent: a second sweep finds nothing (purged_at already set).
    assert purge_expired_jobs(repo, s.storage_dir, retention_days=0) == 0


def _insert_stalled_job(job_id: str, *, attempts: int, age_seconds: int) -> None:
    """Simulate a worker that claimed a job then crashed: status=processing
    with an old updated_at."""
    with psycopg.connect(get_settings().database_url, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, status, filename, storage_path, size_bytes,
                              pages, attempts, created_at, updated_at)
            VALUES (%s, 'processing', 'stalled.pdf', '/nonexistent.pdf', 1, 1, %s,
                    now() - make_interval(secs => %s),
                    now() - make_interval(secs => %s))
            """,
            (job_id, attempts, age_seconds, age_seconds),
        )


def test_reaper_requeues_stalled_job(client: TestClient):
    # FIX 1: a job stuck in 'processing' past the stall timeout must be
    # requeued so another worker can retry it (crash recovery).
    import uuid

    job_id = str(uuid.uuid4())
    _insert_stalled_job(job_id, attempts=1, age_seconds=600)
    repo = JobRepository(app.state.db.pool)

    reaped = repo.reap_stalled_jobs(stall_timeout_s=300, max_attempts=3)
    assert reaped == 1
    assert client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()["status"] == "pending"


def test_reaper_fails_stalled_job_when_attempts_exhausted(client: TestClient):
    # CRITICAL EDGE: a stalled job that already used all attempts must go to
    # 'failed', not loop forever.
    import uuid

    job_id = str(uuid.uuid4())
    _insert_stalled_job(job_id, attempts=3, age_seconds=600)
    repo = JobRepository(app.state.db.pool)

    assert repo.reap_stalled_jobs(stall_timeout_s=300, max_attempts=3) == 1
    body = client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()
    assert body["status"] == "failed"
    # Reaper error text is a safe category, never document content.
    assert body["error"] == "processing_error: StalledJobReaped"


def test_reaper_leaves_fresh_processing_job(client: TestClient):
    # A job just claimed (fresh updated_at) must NOT be reaped.
    import uuid

    job_id = str(uuid.uuid4())
    _insert_stalled_job(job_id, attempts=1, age_seconds=5)
    repo = JobRepository(app.state.db.pool)

    assert repo.reap_stalled_jobs(stall_timeout_s=300, max_attempts=3) == 0
    assert (
        client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()["status"] == "processing"
    )


def test_result_persisted_before_purge_is_crash_safe(client: TestClient):
    # FIX 1 ordering: after processing with purge on, the job is 'done' with a
    # result AND the file is gone. The result must exist even though the PDF
    # was purged (mark_done runs before delete_pdf).
    s = get_settings()
    job_id = _upload(client)
    repo = JobRepository(app.state.db.pool)
    engine = build_engine(s.ocr_engine, s.ocr_lang)
    process_one(repo, engine, s.model_copy(update={"purge_after_done": True}))

    body = client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()
    assert body["status"] == "done"
    assert body["result"]["page_count"] == 1  # result survived the purge
    assert pdf_exists(job_id, s.storage_dir) is False
