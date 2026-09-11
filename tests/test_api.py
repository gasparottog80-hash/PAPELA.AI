from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def test_agent_success(client: TestClient) -> None:
    r = client.post("/v1/agent", json={"prompt": "hello"})
    assert r.status_code == 200
    body = r.json()
    assert body["reply"].startswith("[stub]")
    assert body["request_id"]
    assert r.headers["x-request-id"] == body["request_id"]


def test_agent_rejects_empty_prompt(client: TestClient) -> None:
    # Pydantic min_length=1 -> 422 validation error (expected failure).
    r = client.post("/v1/agent", json={"prompt": ""})
    assert r.status_code == 422


def test_agent_rejects_oversized_prompt(client: TestClient) -> None:
    # Critical edge case: cost/abuse guard on prompt size -> structured 400.
    r = client.post("/v1/agent", json={"prompt": "x" * 8_001})
    assert r.status_code == 400
    assert "exceeds" in r.json()["error"]
