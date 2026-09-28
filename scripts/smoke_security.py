"""Negative HTTP smoke against the disposable Gate 1 stack only."""
from __future__ import annotations

from uuid import UUID

import httpx


def main() -> None:
    with httpx.Client(base_url="http://127.0.0.1:18077", timeout=10) as client:
        key = {"X-API-Key": "gate1-synthetic-test-key"}
        response = client.get(
            "/health", headers={"X-Request-ID": "unsafe injected value"}
        )
        assert response.status_code == 200
        UUID(response.headers["x-request-id"])
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert client.get("/docs").status_code == 404
        assert (
            client.get("/health", headers={"Host": "attacker.invalid"}).status_code
            == 400
        )
        assert client.post("/v1/upload", content=b"not a pdf").status_code == 401
        assert (
            client.post("/v1/upload", headers={"X-API-Key": "invalid"}).status_code
            == 401
        )
        assert client.get("/v1/jobs/invalid", headers=key).status_code == 422
        response = client.post("/v1/upload", headers=key, files={
            "file": ("../unsafe.pdf", b"GIF89a", "application/pdf"),
        })
        assert response.status_code == 400
        assert "not a PDF" in response.json()["error"]
        response = client.post("/v1/upload", headers=key, files={
            "file": ("unsafe.pdf", b"%PDF-1.4\ntruncated", "application/pdf"),
        })
        assert response.status_code == 400
        assert "truncated" not in response.text
        response = client.post("/v1/upload", headers=key, files={
            "file": ("unsafe.pdf", b"", "application/pdf"),
        })
        assert response.status_code == 400
        response = client.post(
            "/v1/upload", headers=key, content=b"x" * (21 * 1024 * 1024)
        )
        assert response.status_code == 413
    print("Security smoke PASS: auth, headers, host, docs, IDs, invalid/oversize PDF")


if __name__ == "__main__":
    main()
