"""Synthetic two-tenant seed and post-restore API verification.

Only localhost with a trusted local Caddy CA is permitted. The state file
contains generated job UUIDs, never credentials or document contents.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from uuid import UUID

import httpx
from smoke_production_local import TENANT_A, TENANT_B, digital_pdf

TENANT_C = {"X-API-Key": "gate8-synthetic-tenant-key-33333333333333333333333333"}


def _job_done(client: httpx.Client, job_id: str, auth: dict[str, str]) -> dict:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        response = client.get(f"/v1/jobs/{job_id}", headers=auth)
        assert response.status_code == 200, response.status_code
        body = response.json()
        assert body["status"] != "failed", "synthetic job failed"
        if body["status"] == "done" and body["purged_at"] is not None:
            assert "Invoice 123" in body["result"]["pages"][0]["text"]
            return body
        time.sleep(0.5)
    raise AssertionError("synthetic job was not completed and purged")


def _create(client: httpx.Client, auth: dict[str, str]) -> str:
    response = client.post(
        "/v1/upload",
        headers=auth,
        files={"file": ("synthetic.pdf", digital_pdf(), "application/pdf")},
    )
    assert response.status_code == 202, response.status_code
    job_id = response.json()["job_id"]
    assert str(UUID(job_id)) == job_id
    _job_done(client, job_id, auth)
    return job_id


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("seed", "erase", "verify"))
    parser.add_argument("--state", required=True)
    parser.add_argument("--ca", required=True)
    parser.add_argument("--port", type=int, default=18443)
    parser.add_argument("--reconciled-job-id")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not Path(args.ca).is_file():
        raise ValueError("localhost TLS and a trusted local CA are required")
    state_path = Path(args.state)
    with httpx.Client(
        base_url=f"https://localhost:{args.port}",
        verify=args.ca,
        trust_env=False,
        timeout=10,
    ) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/readiness").json() == {"status": "ready"}
        if args.phase == "seed":
            job_a = _create(client, TENANT_A)
            job_b = _create(client, TENANT_B)
            erasure_job = _create(client, TENANT_A)
            tenant_c_job = _create(client, TENANT_C)
            state_path.write_text(
                json.dumps({"tenant_a_job": job_a, "tenant_b_job": job_b,
                            "erasure_job": erasure_job,
                            "tenant_c_job": tenant_c_job}),
                encoding="utf-8",
            )
        elif args.phase == "erase":
            state = json.loads(state_path.read_text(encoding="utf-8"))
            erased = str(UUID(state["erasure_job"]))
            assert (
                client.delete(f"/v1/jobs/{erased}", headers=TENANT_B).status_code == 404
            )
            assert (
                client.delete(f"/v1/jobs/{erased}", headers=TENANT_A).status_code == 204
            )
            assert (
                client.delete(f"/v1/jobs/{erased}", headers=TENANT_A).status_code == 204
            )
            assert client.get(f"/v1/jobs/{erased}", headers=TENANT_A).status_code == 404
        else:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            job_a = str(UUID(state["tenant_a_job"]))
            job_b = str(UUID(state["tenant_b_job"]))
            _job_done(client, job_a, TENANT_A)
            _job_done(client, job_b, TENANT_B)
            erased = str(UUID(state["erasure_job"]))
            assert client.get(f"/v1/jobs/{erased}", headers=TENANT_A).status_code == 404
            tenant_c_job = str(UUID(state["tenant_c_job"]))
            assert (
                client.get(f"/v1/jobs/{tenant_c_job}", headers=TENANT_C).status_code
                == 401
            )
            if args.reconciled_job_id:
                pending_id = str(UUID(args.reconciled_job_id))
                failed = client.get(f"/v1/jobs/{pending_id}", headers=TENANT_A)
                assert failed.status_code == 200
                assert failed.json()["status"] == "failed"
                assert failed.json()["error"] == (
                    "processing_error: RestoreRequiresResubmission"
                )
                assert client.get(
                    f"/v1/jobs/{pending_id}", headers=TENANT_B
                ).status_code == 404
            fresh_job = _create(client, TENANT_A)
            assert fresh_job not in {job_a, job_b}
        assert client.get(f"/v1/jobs/{job_a}", headers=TENANT_B).status_code == 404
        assert client.get(f"/v1/jobs/{job_b}", headers=TENANT_A).status_code == 404
    print(f"BACKUP_RESTORE_{args.phase.upper()}_PASS")


if __name__ == "__main__":
    main()
