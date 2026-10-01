"""Synthetic Gate 4 smoke, hard-wired to localhost TLS in Caddy's network.

This script must never be pointed at a real customer endpoint. It accepts
only localhost and a temporary local Caddy CA for TLS verification.
"""

from __future__ import annotations

import argparse
import io
import time
from pathlib import Path

import httpx
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

TENANT_A = {"X-API-Key": "gate4-synthetic-tenant-key-11111111111111111111111111"}
TENANT_B = {"X-API-Key": "gate4-synthetic-tenant-key-22222222222222222222222222"}


def digital_pdf() -> bytes:
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


def wait_done(client: httpx.Client, job_id: str) -> dict:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        response = client.get(f"/v1/jobs/{job_id}", headers=TENANT_A)
        assert response.status_code == 200, response.status_code
        body = response.json()
        assert body["status"] != "failed", "worker failed"
        if body["status"] == "done":
            assert body["result"]["engine"] == "text"
            assert "Invoice 123" in body["result"]["pages"][0]["text"]
            assert body["result"]["fields"]["extractor_version"] == 1
            return body
        time.sleep(0.5)
    raise AssertionError("worker did not finish within 60 seconds")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("before", "after"))
    parser.add_argument("--job-id")
    parser.add_argument("--port", type=int, default=443)
    parser.add_argument("--ca", default="/certs/root.crt")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        raise ValueError("invalid local TLS port")
    ca = Path(args.ca)
    if not ca.is_file():
        raise RuntimeError("local Caddy CA is required; never disable TLS verification")
    with httpx.Client(
        base_url=f"https://localhost:{args.port}",
        verify=str(ca),
        timeout=10,
        trust_env=False,
    ) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.headers["strict-transport-security"] == "max-age=31536000"
        assert health.headers["x-content-type-options"] == "nosniff"
        assert health.headers["cache-control"] == "no-store"
        assert client.get("/readiness").json() == {"status": "ready"}
        assert client.post("/v1/upload").status_code == 401
        if args.phase == "before":
            response = client.post(
                "/v1/upload",
                headers=TENANT_A,
                files={"file": ("digital.pdf", digital_pdf(), "application/pdf")},
            )
            assert response.status_code == 202, response.status_code
            job_id = response.json()["job_id"]
            wait_done(client, job_id)
            assert client.get(f"/v1/jobs/{job_id}", headers=TENANT_B).status_code == 404
            assert (
                client.delete(f"/v1/jobs/{job_id}", headers=TENANT_B).status_code
                == 404
            )
            print(f"PRE_RESTART_JOB_ID={job_id}")
        else:
            if not args.job_id:
                raise ValueError("after phase requires --job-id")
            wait_done(client, args.job_id)
            assert client.get(
                f"/v1/jobs/{args.job_id}/audit", headers=TENANT_B
            ).status_code == 404
            assert (
                client.delete(f"/v1/jobs/{args.job_id}", headers=TENANT_A).status_code
                == 204
            )
            response = client.post(
                "/v1/upload",
                headers=TENANT_A,
                files={"file": ("after-restart.pdf", digital_pdf(), "application/pdf")},
            )
            assert response.status_code == 202
            second = response.json()["job_id"]
            wait_done(client, second)
            assert (
                client.delete(f"/v1/jobs/{second}", headers=TENANT_A).status_code
                == 204
            )
            print("Production-local smoke PASS: TLS, readiness, isolation, persistence")


if __name__ == "__main__":
    main()
