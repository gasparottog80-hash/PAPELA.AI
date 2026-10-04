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
os.environ.setdefault(
    "PAPELA_TENANT_API_KEYS",
    '{"22222222-2222-4222-8222-222222222222":"gate1-synthetic-b-key"}',
)
os.environ.setdefault("PAPELA_OCR_ENGINE", "fake")
os.environ.setdefault("PAPELA_STORAGE_DIR", "./data/test-uploads")
# Tests opt OUT of purge-after-done so retention/audit paths can inspect the
# file; the dedicated purge test re-enables it via Settings.model_copy.
os.environ.setdefault("PAPELA_PURGE_AFTER_DONE", "false")

from app.config import DEVELOPMENT_LEGACY_TENANT_ID, get_settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.main import app  # noqa: E402
from app.repository import JobRepository  # noqa: E402
from app.storage import pdf_exists, purge_expired_jobs  # noqa: E402
from app.worker import process_one  # noqa: E402

AUTH = {"X-API-Key": "test-key"}
AUTH_B = {"X-API-Key": "gate1-synthetic-b-key"}


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


def _digital_text_pdf() -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
    )
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 25 100 Td (Invoice 123) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(content)
    data = io.BytesIO()
    writer.write(data)
    return data.getvalue()


def _db_available() -> bool:
    try:
        with psycopg.connect(get_settings().database_url, connect_timeout=3):
            return True
    except Exception:
        return False


@pytest.fixture()
def client(tmp_path, monkeypatch):
    assert _db_available(), "Integration tests require disposable Postgres; no skips"
    monkeypatch.setattr(get_settings(), "erasure_dir", str(tmp_path / "erasures"))
    with TestClient(app) as c:
        # Clean slate for deterministic assertions.
        with psycopg.connect(get_settings().database_url, autocommit=True) as conn:
            conn.execute("TRUNCATE jobs")
            conn.execute(
                "UPDATE privacy_state SET generation = 0, restore_ready = TRUE"
            )
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
    assert process_one(repo, s) is True

    r = client.get(f"/v1/jobs/{job_id}", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert body["result"]["engine"] == "fake"
    assert body["result"]["page_count"] == 1
    # Schema v3: structured tables must survive the round-trip to Postgres JSONB
    # and back out through the API.
    assert body["result"]["schema_version"] == 3
    page = body["result"]["pages"][0]
    assert "tables" in page
    assert len(page["tables"]) == 1
    table = page["tables"][0]
    assert table["n_rows"] == 3 and table["n_cols"] == 2
    assert table["rows"][0] == ["Item", "Valor"]
    assert table["rows"][2] == ["Servico B", "250,50"]
    # v3: deterministic fiscal fields extracted from the OCR payload survive the
    # JSONB round-trip. The fake table (Item/Valor -> Servico A/100,00, Servico
    # B/250,50) must yield two line items with parsed pt-BR money.
    fields = body["result"]["fields"]
    assert fields["extractor_version"] == 1
    assert len(fields["itens"]) == 2
    assert fields["itens"][0]["descricao"] == "Servico A"
    assert fields["itens"][0]["valor_total"] == "100.00"
    assert fields["itens"][1]["valor_total"] == "250.50"


def test_upload_requires_api_key(client: TestClient):
    # EXPECTED FAILURE: fail-closed auth rejects missing key.
    r = client.post(
        "/v1/upload",
        files={"file": ("x.pdf", io.BytesIO(_minimal_pdf()), "application/pdf")},
    )
    assert r.status_code == 401


def test_health_is_liveness_and_readiness_requires_database_and_storage(
    client: TestClient, monkeypatch, tmp_path
):
    settings = get_settings()
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/readiness").json() == {"status": "ready"}
    monkeypatch.setattr(app.state.db, "is_ready", lambda: False)
    assert client.get("/health").status_code == 200
    assert client.get("/readiness").status_code == 503
    monkeypatch.setattr(app.state.db, "is_ready", lambda: True)
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "missing"))
    assert client.get("/readiness").status_code == 503


def test_upload_rejects_non_pdf(client: TestClient):
    # CRITICAL EDGE: bad magic bytes must be rejected before any job is created.
    r = client.post(
        "/v1/upload",
        headers=AUTH,
        files={
            "file": (
                "evil.pdf",
                io.BytesIO(b"GIF89a not a pdf"),
                "application/pdf",
            )
        },
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
    assert process_one(repo, purge_settings) is True

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
    process_one(repo, s)  # done, PDF kept (test env => no purge)
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
            INSERT INTO jobs (id, tenant_id, status, filename, storage_path, size_bytes,
                              pages, attempts, created_at, updated_at)
            VALUES (%s, %s, 'processing', 'stalled.pdf', '/nonexistent.pdf', 1, 1, %s,
                    now() - make_interval(secs => %s),
                    now() - make_interval(secs => %s))
            """,
            (job_id, DEVELOPMENT_LEGACY_TENANT_ID, attempts, age_seconds, age_seconds),
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
    process_one(repo, s.model_copy(update={"purge_after_done": True}))

    body = client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()
    assert body["status"] == "done"
    assert body["result"]["page_count"] == 1  # result survived the purge
    assert pdf_exists(job_id, s.storage_dir) is False


@pytest.mark.parametrize(
    "path", ["/v1/upload", "/v1/jobs/invalid", "/v1/jobs/invalid/audit"]
)
@pytest.mark.parametrize("key", [None, "wrong-key", "test-key,wrong", "x" * 513])
def test_credentials_fail_closed(client, path, key):
    headers = {} if key is None else {"X-API-Key": key}
    response = (
        client.post(path, headers=headers)
        if path == "/v1/upload"
        else (client.get(path, headers=headers))
    )
    assert response.status_code == 401


def test_duplicate_key_header_is_rejected(client):
    response = client.get(
        "/v1/jobs/invalid",
        headers=[
            ("X-API-Key", "test-key"),
            ("X-API-Key", "wrong"),
        ],
    )
    assert response.status_code == 401


@pytest.mark.parametrize("payload", [b"", b"GIF89a", b"%PDF-1.4\ntruncated"])
def test_invalid_upload_cleanup(client, payload):
    from pathlib import Path

    root = Path(get_settings().storage_dir)
    before = set(root.glob("*.pdf"))
    response = client.post(
        "/v1/upload",
        headers=AUTH,
        files={
            "file": ("secret.pdf", payload, "application/pdf"),
        },
    )
    assert response.status_code == 400
    assert set(root.glob("*.pdf")) == before
    assert "truncated" not in response.text


@pytest.mark.parametrize(
    "filename", ["../../nota.pdf", "/tmp/nota.pdf", "C:\\tmp\\nota.pdf"]
)
def test_filename_does_not_control_storage(client, filename):
    from pathlib import Path

    response = client.post(
        "/v1/upload",
        headers=AUTH,
        files={
            "file": (filename, _minimal_pdf(), "application/pdf"),
        },
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert (Path(get_settings().storage_dir) / f"{job_id}.pdf").is_file()
    assert (
        client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()["filename"] == "nota.pdf"
    )


def test_mime_mismatch_and_file_limit(client, monkeypatch):
    response = client.post(
        "/v1/upload",
        headers=AUTH,
        files={
            "file": ("nota.pdf", _minimal_pdf(), "text/plain"),
        },
    )
    assert response.status_code == 400
    monkeypatch.setattr(get_settings(), "max_upload_bytes", 100)
    response = client.post(
        "/v1/upload",
        headers=AUTH,
        files={
            "file": ("nota.pdf", _minimal_pdf(), "application/pdf"),
        },
    )
    assert response.status_code == 413
    response = client.post("/v1/upload", headers=AUTH, content=b"x" * 70000)
    assert response.status_code == 413


def test_invalid_id_does_not_echo_input(client):
    response = client.get("/v1/jobs/SECRET-invalid-id", headers=AUTH)
    assert response.status_code == 422
    assert "SECRET" not in response.text


def test_unknown_resource_and_invalid_credentials(client):
    import uuid

    job_id = _upload(client)
    assert (
        client.get(f"/v1/jobs/{job_id}", headers={"X-API-Key": "other"}).status_code
        == 401
    )
    assert client.get(f"/v1/jobs/{uuid.uuid4()}", headers=AUTH).status_code == 404


def test_request_id_headers_host_docs_and_cors(client):
    import uuid

    response = client.get(
        "/health", headers={"X-Request-ID": "sensitive-untrusted-text"}
    )
    uuid.UUID(response.headers["x-request-id"])
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert "strict-transport-security" not in response.headers
    assert (
        client.get("/health", headers={"Host": "attacker.example"}).status_code == 400
    )
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
    response = client.options(
        "/v1/upload",
        headers={
            **AUTH,
            "Origin": "https://attacker.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-origin" not in response.headers
    assert (
        client.get("/health", headers={"X-Forwarded-For": "127.0.0.1"}).status_code
        == 200
    )


def test_errors_and_orphan_cleanup(client, monkeypatch):
    from pathlib import Path

    root = Path(get_settings().storage_dir)
    before = set(root.glob("*.pdf"))

    def fail(**kwargs):
        raise RuntimeError("SECRET SQL /private/customer.pdf CPF 12345678901")

    monkeypatch.setattr(app.state.repo, "create", fail)
    response = client.post(
        "/v1/upload",
        headers=AUTH,
        files={
            "file": ("nota.pdf", _minimal_pdf(), "application/pdf"),
        },
    )
    assert response.status_code == 500
    assert response.json()["error"] == "internal error"
    assert "SECRET" not in response.text
    assert set(root.glob("*.pdf")) == before


def test_rate_and_storage_limits(client, monkeypatch):
    from app.security import RateLimiter

    app.state.rate_limiter = RateLimiter(1)
    assert client.get("/v1/jobs/invalid", headers=AUTH).status_code == 422
    response = client.get("/v1/jobs/invalid", headers=AUTH)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    app.state.rate_limiter = RateLimiter(60)
    monkeypatch.setattr(get_settings(), "max_storage_bytes", 1)
    response = client.post(
        "/v1/upload",
        headers=AUTH,
        files={
            "file": ("nota.pdf", _minimal_pdf(), "application/pdf"),
        },
    )
    assert response.status_code == 507
    assert response.json()["error"] == "storage capacity exceeded"


def test_worker_rejects_arbitrary_path_and_purges_terminal_failure(client):
    job_id = _upload(client)
    with psycopg.connect(get_settings().database_url, autocommit=True) as conn:
        conn.execute(
            "UPDATE jobs SET storage_path = %s WHERE id = %s", ("/etc/passwd", job_id)
        )
    repo = JobRepository(app.state.db.pool)
    settings = get_settings().model_copy(update={"max_attempts": 1})
    assert process_one(repo, settings)
    body = client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()
    assert body["status"] == "failed"
    assert body["purged_at"] is not None
    assert not pdf_exists(job_id, settings.storage_dir)
    assert "passwd" not in body["error"]


def test_worker_deadline_and_retry_limit(client):
    job_id = _upload(client)
    repo = JobRepository(app.state.db.pool)
    settings = get_settings().model_copy(
        update={
            "max_attempts": 2,
            "processing_timeout_s": 0.000001,
        }
    )
    assert process_one(repo, settings)
    assert repo.get_internal(job_id)["status"] == "pending"
    assert process_one(repo, settings)
    assert repo.get_internal(job_id)["status"] == "failed"
    assert not pdf_exists(job_id, settings.storage_dir)
    assert not process_one(repo, settings)


def test_production_rejects_privileged_db_role(client):
    settings = get_settings().model_copy(update={"env": "production"})
    db = Database(settings)
    with pytest.raises(RuntimeError, match="least-privilege"):
        db.open()


def test_production_rejects_weak_or_missing_keys(monkeypatch):
    monkeypatch.setattr(get_settings(), "env", "production")
    with pytest.raises(RuntimeError):
        with TestClient(app):
            pass


def test_production_rejects_fake_ocr_and_disabled_purge(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "env", "production")
    monkeypatch.setattr(settings, "api_keys", "")
    monkeypatch.setattr(
        settings,
        "tenant_api_keys",
        {"22222222-2222-4222-8222-222222222222": "x" * 32},
    )
    monkeypatch.setattr(settings, "database_url", "postgresql://app:synthetic-strong@db:5432/app")
    with pytest.raises(RuntimeError, match="text-layer"):
        with TestClient(app):
            pass
    monkeypatch.setattr(settings, "ocr_engine", "text")
    monkeypatch.setattr(settings, "purge_after_done", False)
    with pytest.raises(RuntimeError, match="purge"):
        with TestClient(app):
            pass


@pytest.mark.parametrize("stall,processing", [(1, 60), (120, 120)])
def test_reaper_must_follow_processing_deadline(stall, processing):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        type(get_settings()).model_validate(
            get_settings().model_dump()
            | {
                "stall_timeout_s": stall,
                "processing_timeout_s": processing,
            }
        )


def test_tenant_isolation_for_status_result_audit_and_delete(client: TestClient):
    job_a = _upload(client)
    response_b = client.post(
        "/v1/upload",
        headers=AUTH_B,
        files={"file": ("b.pdf", _minimal_pdf(), "application/pdf")},
    )
    assert response_b.status_code == 202
    job_b = response_b.json()["job_id"]
    with psycopg.connect(get_settings().database_url) as conn:
        owners = dict(
            conn.execute(
                "SELECT id::text, tenant_id::text FROM jobs WHERE id IN (%s, %s)",
                (job_a, job_b),
            ).fetchall()
        )
    assert owners[job_a] == DEVELOPMENT_LEGACY_TENANT_ID
    assert owners[job_b] == "22222222-2222-4222-8222-222222222222"

    assert client.get(f"/v1/jobs/{job_a}", headers=AUTH).status_code == 200
    assert client.get(f"/v1/jobs/{job_b}", headers=AUTH_B).status_code == 200
    assert client.delete(f"/v1/jobs/{job_a}").status_code == 401
    assert client.delete(
        f"/v1/jobs/{job_a}", headers={"X-API-Key": "invalid"}
    ).status_code == 401
    for path in (f"/v1/jobs/{job_a}", f"/v1/jobs/{job_a}/audit"):
        denied = client.get(path, headers=AUTH_B)
        missing = client.get(
            "/v1/jobs/00000000-0000-0000-0000-000000000000", headers=AUTH_B
        )
        assert denied.status_code == missing.status_code == 404
        assert denied.json()["error"] == missing.json()["error"]
    assert client.delete(f"/v1/jobs/{job_a}", headers=AUTH_B).status_code == 404
    assert client.delete(f"/v1/jobs/{job_b}", headers=AUTH_B).status_code == 409

    repo = JobRepository(app.state.db.pool)
    assert process_one(repo, get_settings())
    assert client.get(f"/v1/jobs/{job_a}", headers=AUTH).json()["status"] == "done"
    assert client.get(f"/v1/jobs/{job_a}", headers=AUTH_B).status_code == 404
    assert client.delete(f"/v1/jobs/{job_a}", headers=AUTH).status_code == 204
    assert client.get(f"/v1/jobs/{job_a}", headers=AUTH).status_code == 404
    assert client.get(f"/v1/jobs/{job_b}", headers=AUTH_B).status_code == 200


def test_text_layer_pipeline_with_real_extractor(client: TestClient):
    response = client.post(
        "/v1/upload",
        headers=AUTH,
        files={"file": ("digital.pdf", _digital_text_pdf(), "application/pdf")},
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    repo = JobRepository(app.state.db.pool)
    settings = get_settings().model_copy(update={"ocr_engine": "text"})
    assert process_one(repo, settings)
    result = client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()
    assert result["status"] == "done"
    assert result["result"]["engine"] == "text"
    assert result["result"]["pages"][0]["text"] == "Invoice 123"
    assert "fields" in result["result"]


def test_job_tenant_id_cannot_be_changed(client: TestClient):
    job_id = _upload(client)
    with psycopg.connect(get_settings().database_url, autocommit=True) as conn:
        with pytest.raises(psycopg.Error, match="immutable"):
            conn.execute(
                "UPDATE jobs SET tenant_id = %s WHERE id = %s",
                ("22222222-2222-4222-8222-222222222222", job_id),
            )
    assert client.get(f"/v1/jobs/{job_id}", headers=AUTH).status_code == 200
    assert client.get(f"/v1/jobs/{job_id}", headers=AUTH_B).status_code == 404


def test_explicit_migration_preserves_and_quarantines_legacy_rows(client: TestClient):
    from importlib.resources import files

    migration = (
        files("app")
        .joinpath("migrations/0002_tenant_isolation.sql")
        .read_text(encoding="utf-8")
    )

    class RollBackSyntheticSchema(Exception):
        pass

    try:
        with psycopg.connect(get_settings().database_url, autocommit=True) as conn:
            with conn.transaction():
                conn.execute("CREATE SCHEMA gate3_migration_test")
                conn.execute("SET LOCAL search_path TO gate3_migration_test")
                conn.execute(
                    "CREATE TABLE jobs (id UUID PRIMARY KEY, "
                    "created_at TIMESTAMPTZ DEFAULT now())"
                )
                legacy_id = "33333333-3333-4333-8333-333333333333"
                conn.execute("INSERT INTO jobs (id) VALUES (%s)", (legacy_id,))
                conn.execute(migration)
                row = conn.execute(
                    "SELECT id::text, tenant_id::text FROM jobs WHERE id = %s",
                    (legacy_id,),
                ).fetchone()
                assert row == (
                    legacy_id,
                    "00000000-0000-0000-0000-000000000001",
                )
                conn.execute(migration)  # explicit migration is idempotent
                assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
                raise RollBackSyntheticSchema
    except RollBackSyntheticSchema:
        pass
