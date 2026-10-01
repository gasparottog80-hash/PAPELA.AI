"""Smoke the isolated synthetic CI stack, never a production endpoint."""

from __future__ import annotations

import io
import os
import time

import httpx
from pypdf import PdfWriter


def main() -> None:
    target = os.environ.get("PAPELA_SMOKE_BASE_URL", "http://127.0.0.1:18077")
    if target not in {"http://127.0.0.1:18077", "http://api:8000"}:
        raise RuntimeError("Smoke target must be the isolated test stack")
    host = {"Host": "localhost"} if target == "http://api:8000" else None
    pdf = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(pdf)
    with httpx.Client(
        base_url=target, timeout=5, headers=host, trust_env=False
    ) as client:
        assert client.get("/health").status_code == 200, "Health check failed"
        assert client.post("/v1/upload").status_code == 401
        headers = {"X-API-Key": "gate1-synthetic-test-key"}
        response = client.post(
            "/v1/upload",
            headers=headers,
            files={"file": ("synthetic.pdf", pdf.getvalue(), "application/pdf")},
        )
        assert response.status_code == 202, "Synthetic upload failed"
        assert response.headers.get("X-Request-ID"), "Missing correlation ID"
        job_id = response.json()["job_id"]
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            response = client.get(f"/v1/jobs/{job_id}", headers=headers)
            assert response.status_code == 200, "Job lookup failed"
            result = response.json()
            assert result["status"] != "failed", "Worker failed"
            if result["status"] == "done":
                break
            time.sleep(0.5)
        else:
            raise AssertionError("Worker timed out")
        assert result["result"]["engine"] == "fake"
        assert result["result"]["page_count"] == 1
        fields = result["result"]["fields"]
        assert fields["extractor_version"] == 1
        assert len(fields["itens"]) == 2
        assert fields["itens"][0]["valor_total"] == "100.00"
        assert fields["itens"][1]["valor_total"] == "250.50"
        audit = client.get(f"/v1/jobs/{job_id}/audit", headers=headers)
        assert audit.status_code == 200
        assert audit.json()["file_exists"] is False
        assert audit.json()["purged_at"] is not None
        other = {"X-API-Key": "gate1-synthetic-b-key"}
        assert client.get(f"/v1/jobs/{job_id}", headers=other).status_code == 404
        assert client.get(f"/v1/jobs/{job_id}/audit", headers=other).status_code == 404
        assert client.delete(f"/v1/jobs/{job_id}", headers=other).status_code == 404
        assert client.delete(f"/v1/jobs/{job_id}", headers=headers).status_code == 204
        assert client.get(f"/v1/jobs/{job_id}", headers=headers).status_code == 404
    print("Smoke PASS: E2E, cross-tenant denial, owner delete and PDF purge")


if __name__ == "__main__":
    main()
