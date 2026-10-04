from __future__ import annotations

import io
import json
import logging
import os
import uuid
from urllib.request import urlopen

import psycopg
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter

# Settings are cached when the app is imported.
os.environ.setdefault("PAPELA_ENV", "test")
os.environ.setdefault(
    "PAPELA_DATABASE_URL", "postgresql://papela:papela@localhost:5433/papela"
)
os.environ.setdefault("PAPELA_API_KEYS", "test-key")
os.environ.setdefault("PAPELA_OCR_ENGINE", "fake")
os.environ.setdefault("PAPELA_STORAGE_DIR", "./data/test-uploads")

from app.config import get_settings  # noqa: E402
from app.error_codes import (  # noqa: E402
    AUTH_INVALID,
    INVALID_PDF,
    INVALID_REQUEST,
    PROCESSING_FAILED,
    PROCESSING_TIMEOUT,
    code_for_exception,
)
from app.logging_config import JsonFormatter  # noqa: E402
from app.main import app  # noqa: E402
from app.metrics import METRICS, Metrics  # noqa: E402
from app.repository import JobRepository  # noqa: E402
from app.worker import process_one  # noqa: E402

AUTH = {"X-API-Key": "test-key"}


class RecordCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    data = io.BytesIO()
    writer.write(data)
    return data.getvalue()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "erasure_dir", str(tmp_path / "erasures"))
    with TestClient(app) as test_client:
        with psycopg.connect(get_settings().database_url, autocommit=True) as conn:
            conn.execute("TRUNCATE jobs")
            conn.execute(
                "UPDATE privacy_state SET generation = 0, restore_ready = TRUE"
            )
        yield test_client


def test_request_id_is_generated_validated_and_returned(client: TestClient):
    generated = client.get("/health").headers["x-request-id"]
    assert str(uuid.UUID(generated)) == generated
    supplied = str(uuid.uuid4())
    assert client.get("/health", headers={"X-Request-ID": supplied}).headers[
        "x-request-id"
    ] == supplied
    invalid = client.get(
        "/health", headers={"X-Request-ID": "SECRET-untrusted-request-id"}
    ).headers["x-request-id"]
    assert invalid != generated and invalid != "SECRET-untrusted-request-id"
    duplicated = client.get(
        "/health",
        headers=[("X-Request-ID", supplied), ("X-Request-ID", supplied)],
    ).headers["x-request-id"]
    assert duplicated != supplied


def test_safe_error_codes_and_no_input_echo(client: TestClient):
    key = "SECRET-INVALID-API-KEY"
    collector = RecordCollector()
    logger = logging.getLogger("papela.api")
    logger.addHandler(collector)
    try:
        auth = client.get("/v1/jobs/SECRET-input", headers={"X-API-Key": key})
        assert auth.status_code == 401
        assert auth.json()["code"] == AUTH_INVALID
        invalid = client.get("/v1/jobs/SECRET-input", headers=AUTH)
        assert invalid.status_code == 422
        assert invalid.json()["code"] == INVALID_REQUEST
        assert invalid.json()["request_id"] == invalid.headers["x-request-id"]
        assert key not in auth.text and "SECRET-input" not in invalid.text
        output = "\n".join(JsonFormatter("api").format(r) for r in collector.records)
        assert key not in output and "SECRET-input" not in output
    finally:
        logger.removeHandler(collector)


def test_metrics_are_loopback_only_and_low_cardinality(client: TestClient):
    before = METRICS.counter_value("requests_total", "health", "2xx")
    assert client.get("/health").status_code == 200
    assert METRICS.counter_value("requests_total", "health", "2xx") == before + 1
    with urlopen("http://127.0.0.1:9100/metrics", timeout=2) as response:
        text = response.read().decode("ascii")
    assert 'requests_total{route="health",status_class="2xx"}' in text
    assert "request_duration_seconds_bucket" in text
    assert "test-key" not in text
    with pytest.raises(ValueError, match="unbounded"):
        METRICS.inc("uploads_rejected_total", "arbitrary-tenant")


def test_upload_log_connects_request_to_worker_job(client: TestClient):
    collector = RecordCollector()
    logger = logging.getLogger("papela.api")
    logger.addHandler(collector)
    supplied = str(uuid.uuid4())
    created_before = METRICS.counter_value("jobs_created_total")
    try:
        response = client.post(
            "/v1/upload",
            headers={**AUTH, "X-Request-ID": supplied},
            files={"file": ("private.pdf", pdf(), "application/pdf")},
        )
    finally:
        logger.removeHandler(collector)
    assert response.status_code == 202
    assert response.headers["x-request-id"] == supplied
    assert METRICS.counter_value("jobs_created_total") == created_before + 1
    accepted = [r for r in collector.records if r.msg == "upload.accepted"]
    assert len(accepted) == 1
    log = json.loads(JsonFormatter("api").format(accepted[0]))
    assert log["request_id"] == supplied
    assert log["job_id"] == response.json()["job_id"]
    assert log["duration_ms"] >= 0
    assert "private.pdf" not in json.dumps(log)


def test_worker_success_and_failure_metrics_are_safe(client: TestClient):
    completed_before = METRICS.counter_value("jobs_completed_total")
    failed_before = METRICS.counter_value("jobs_failed_total")
    collector = RecordCollector()
    logger = logging.getLogger("papela.worker")
    logger.addHandler(collector)
    repo = JobRepository(app.state.db.pool)
    try:
        first = client.post(
            "/v1/upload", headers=AUTH,
            files={"file": ("success.pdf", pdf(), "application/pdf")},
        ).json()["job_id"]
        assert process_one(repo, get_settings())
        assert METRICS.counter_value("jobs_completed_total") == completed_before + 1

        second = client.post(
            "/v1/upload", headers=AUTH,
            files={"file": ("failure.pdf", pdf(), "application/pdf")},
        ).json()["job_id"]
        with psycopg.connect(get_settings().database_url, autocommit=True) as conn:
            conn.execute(
                "UPDATE jobs SET storage_path = %s WHERE id = %s",
                ("/private/SECRET-document.pdf", second),
            )
        settings = get_settings().model_copy(update={"max_attempts": 1})
        assert process_one(repo, settings)
        assert METRICS.counter_value("jobs_failed_total") == failed_before + 1
    finally:
        logger.removeHandler(collector)
    events = [r.msg for r in collector.records]
    assert "job.started" in events and "job.done" in events
    assert "job.failed" in events
    safe_logs = "\n".join(JsonFormatter("worker").format(r) for r in collector.records)
    assert first in safe_logs and second in safe_logs
    assert "SECRET-document" not in safe_logs
    assert "job_processing_duration_seconds" in METRICS.render()


def test_worker_timeout_retry_and_terminal_failure_are_observable(client: TestClient):
    response = client.post(
        "/v1/upload", headers=AUTH,
        files={"file": ("deadline.pdf", pdf(), "application/pdf")},
    )
    assert response.status_code == 202
    repo = JobRepository(app.state.db.pool)
    settings = get_settings().model_copy(
        update={"max_attempts": 2, "processing_timeout_s": 0.000001}
    )
    retries_before = METRICS.counter_value("jobs_retried_total")
    failures_before = METRICS.counter_value("jobs_failed_total")
    collector = RecordCollector()
    logger = logging.getLogger("papela.worker")
    logger.addHandler(collector)
    try:
        assert process_one(repo, settings)
        assert process_one(repo, settings)
    finally:
        logger.removeHandler(collector)
    assert METRICS.counter_value("jobs_retried_total") == retries_before + 1
    assert METRICS.counter_value("jobs_failed_total") == failures_before + 1
    events = [r.msg for r in collector.records]
    assert "job.timeout" in events and "job.retry" in events
    assert "job.failed" in events
    assert all(
        r.error_code == PROCESSING_TIMEOUT
        for r in collector.records
        if r.msg in {"job.timeout", "job.retry", "job.failed"}
    )


def test_error_taxonomy_and_formatter_discard_unapproved_fields():
    assert code_for_exception(TimeoutError("SECRET")) == PROCESSING_TIMEOUT
    assert code_for_exception(ValueError("corrupt PDF SECRET")) == INVALID_PDF
    assert code_for_exception(RuntimeError("SECRET")) == PROCESSING_FAILED
    record = logging.LogRecord(
        "papela.test", logging.ERROR, __file__, 1, "request.failed", (), None
    )
    record.tenant_id = "SECRET-tenant"  # type: ignore[attr-defined]
    record.api_key = "SECRET-key"  # type: ignore[attr-defined]
    record.error_code = AUTH_INVALID  # type: ignore[attr-defined]
    output = JsonFormatter("api").format(record)
    assert json.loads(output)["error_code"] == AUTH_INVALID
    assert "SECRET" not in output and "tenant_id" not in output


def test_worker_heartbeat_metric_does_not_include_identifiers():
    metrics = Metrics()
    metrics.heartbeat()
    output = metrics.render(worker=True)
    assert "worker_heartbeat_age_seconds" in output
    assert "job_id" not in output and "tenant_id" not in output
