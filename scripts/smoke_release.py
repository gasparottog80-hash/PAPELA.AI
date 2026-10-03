"""Small HTTPS release canary; reads a dedicated key file, never prints it."""

from __future__ import annotations

import argparse
import json
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path


def sample_pdf() -> bytes:
    stream = b"BT /F1 12 Tf 25 100 Td (Invoice 123) Tj ET"
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
    )
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    start = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(
        (
            f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
            f"startxref\n{start}\n%%EOF\n"
        ).encode()
    )
    return bytes(data)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--other-key-file", type=Path)
    parser.add_argument("--ca", type=Path)
    args = parser.parse_args()
    if not args.url.startswith("https://"):
        raise RuntimeError("HTTPS_REQUIRED")
    primary = args.key_file.read_text(encoding="utf-8").strip()
    other = (
        args.other_key_file.read_text(encoding="utf-8").strip()
        if args.other_key_file
        else None
    )
    if len(primary) < 24 or (other is not None and len(other) < 24):
        raise RuntimeError("SMOKE_KEY_INVALID")
    context = ssl.create_default_context(cafile=str(args.ca) if args.ca else None)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
    )
    base = args.url.rstrip("/")

    def request(
        path: str,
        key: str | None = None,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> tuple[int, dict]:
        headers = {"X-API-Key": key} if key else {}
        if content_type:
            headers["Content-Type"] = content_type
        try:
            with opener.open(
                urllib.request.Request(base + path, data=body, headers=headers),
                timeout=10,
            ) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, {}

    if request("/health")[0] != 200 or request("/readiness") != (
        200,
        {"status": "ready"},
    ):
        raise RuntimeError("HEALTH_OR_READINESS_FAILED")
    boundary = b"papela-release-canary"
    body = (
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="file"; '
        b'filename="release-canary.pdf"\r\n'
        b"Content-Type: application/pdf\r\n\r\n"
        + sample_pdf()
        + b"\r\n--"
        + boundary
        + b"--\r\n"
    )
    status, accepted = request(
        "/v1/upload",
        primary,
        body,
        "multipart/form-data; boundary=" + boundary.decode(),
    )
    if status != 202:
        raise RuntimeError("UPLOAD_CANARY_FAILED")
    job_id = accepted["job_id"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        status, result = request(f"/v1/jobs/{job_id}", primary)
        if status != 200 or result.get("status") == "failed":
            raise RuntimeError("JOB_CANARY_FAILED")
        if result.get("status") == "done":
            if "Invoice 123" not in result["result"]["pages"][0]["text"]:
                raise RuntimeError("EXTRACTION_CANARY_FAILED")
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("JOB_CANARY_TIMEOUT")
    if other and request(f"/v1/jobs/{job_id}", other)[0] != 404:
        raise RuntimeError("TENANT_ISOLATION_FAILED")
    print(json.dumps({"status": "PASS", "check": "release_smoke", "job_id": job_id}))


if __name__ == "__main__":
    main()
