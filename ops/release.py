"""Fail-closed single-host release controller. No registry or remote deploy here.

The state directory is external to Git and backed up separately. Commands that
change services require --execute. See docs/gate7-deploy-rollback.md.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_FILES = (
    "ops/0000_lock.sql",
    "app/migrations/0001_initial.sql",
    "app/migrations/0002_tenant_isolation.sql",
    "ops/010_runtime_role.sql",
)
SHA = re.compile(r"[0-9a-f]{40}\Z")
BACKUP_ID = re.compile(r"backup-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}\Z")
SCANNER = (
    "aquasec/trivy@sha256:"
    "62b1e65e8869bc4b4c6aa4fa2b21595256c7c2f6018a9d9ad61caf87187c1969"
)


class ReleaseError(RuntimeError):
    pass


def utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def event(name: str, release_id: str, **fields: str | int) -> None:
    allowed = {"backup_id", "error_code", "image_id", "previous_release"}
    payload: dict[str, str | int] = {
        "ts": utc(),
        "service": "release",
        "event": name,
        "release_id": release_id,
    }
    payload.update({key: value for key, value in fields.items() if key in allowed})
    print(json.dumps(payload, separators=(",", ":")), flush=True)


def run(*argv: str, env: dict[str, str] | None = None, capture: bool = True) -> str:
    result = subprocess.run(
        argv,
        cwd=ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        # Child stderr may contain a secret or private URL. Never reflect it.
        raise ReleaseError(
            f"COMMAND_FAILED_{Path(argv[0]).name.upper().replace('-', '_')}"
        )
    return result.stdout.strip() if capture else ""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def schema_fingerprint() -> str:
    digest = hashlib.sha256()
    for name in SCHEMA_FILES:
        digest.update(name.encode("ascii") + b"\0")
        digest.update((ROOT / name).read_bytes())
    return digest.hexdigest()


def extractor_commit() -> str:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    package = next(p for p in lock["package"] if p["name"] == "papela-fiscal-extractor")
    commit = package["source"]["git"].rsplit("#", 1)[-1]
    if not SHA.fullmatch(commit):
        raise ReleaseError("EXTRACTOR_LOCK_INVALID")
    return commit


def image_details(tag: str) -> tuple[str, str]:
    payload = json.loads(run("docker", "image", "inspect", tag))
    if len(payload) != 1:
        raise ReleaseError("IMAGE_MISSING")
    image = payload[0]
    return image["Id"], image["Config"]["Labels"].get(
        "org.opencontainers.image.revision", ""
    )


def scan_image(tag: str) -> dict[str, int]:
    # Scan the exact local image bytes. Reports and tar never leave the host.
    with tempfile.TemporaryDirectory(prefix="papela-release-scan-") as tmp:
        archive = Path(tmp) / "image.tar"
        report = Path(tmp) / "scan.json"
        run("docker", "save", "--output", str(archive), tag, capture=False)
        run(
            "docker",
            "run",
            "--rm",
            "--mount",
            f"type=bind,source={tmp},target=/scan",
            SCANNER,
            "image",
            "--input",
            "/scan/image.tar",
            "--cache-dir",
            "/scan/cache",
            "--scanners",
            "vuln,secret",
            "--severity",
            "CRITICAL,HIGH",
            "--format",
            "json",
            "--output",
            "/scan/scan.json",
            "--timeout",
            "5m",
            "--quiet",
            capture=False,
        )
        data = json.loads(report.read_text(encoding="utf-8"))
        targets = data.get("Results") or []
        if not targets:
            raise ReleaseError("SCAN_EMPTY")
        severe = sum(
            vulnerability.get("Severity") in {"CRITICAL", "HIGH"}
            for target in targets
            for vulnerability in target.get("Vulnerabilities") or []
        )
        secrets = sum(len(target.get("Secrets") or []) for target in targets)
        if severe or secrets:
            raise ReleaseError("SCAN_BLOCKED")
        return {"critical_high": severe, "secrets": secrets}


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=".release-",
        delete=False,
    ) as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temp = Path(handle.name)
    try:
        if os.name != "nt":
            temp.chmod(0o600)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ReleaseError("MANIFEST_MISSING")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ReleaseError("MANIFEST_INVALID")
    return value


@contextlib.contextmanager
def exclusive_lock(directory: Path) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / "deploy.lock"
    with lock.open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                if handle.read(1) == b"":
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ReleaseError("DEPLOY_LOCK_BUSY") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def manifest_path(state: Path, commit: str) -> Path:
    if not SHA.fullmatch(commit):
        raise ReleaseError("RELEASE_SHA_INVALID")
    return state / "releases" / f"{commit}.json"


def active(state: Path) -> dict[str, Any] | None:
    path = state / "active.json"
    return read_json(path) if path.is_file() else None


def env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, marker, value = line.partition("=")
        if marker and re.fullmatch(r"PAPELA_[A-Z0-9_]+", key):
            values[key] = value.strip().strip('"')
    return values


def set_env_image(path: Path, image: str) -> None:
    """Keep an operator's later plain Compose invocation on the active image."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    matches = [
        index
        for index, line in enumerate(lines)
        if line.startswith("PAPELA_APP_IMAGE=")
    ]
    if len(matches) != 1:
        raise ReleaseError("APP_IMAGE_ENV_INVALID")
    lines[matches[0]] = f"PAPELA_APP_IMAGE={image}\n"
    mode = path.stat().st_mode & 0o777
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=".papela-env-",
        delete=False,
    ) as handle:
        handle.writelines(lines)
        temp = Path(handle.name)
    try:
        if os.name != "nt":
            temp.chmod(mode)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def compose_env(args: argparse.Namespace, image: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(env_file(args.env_file))
    env["PAPELA_APP_IMAGE"] = image
    return env


def compose(args: argparse.Namespace, image: str, *command: str) -> str:
    return run(
        "docker",
        "compose",
        "--env-file",
        str(args.env_file),
        "-f",
        "docker-compose.prod.yml",
        *command,
        env=compose_env(args, image),
    )


def ci_green(commit: str) -> None:
    url = (
        "https://api.github.com/repos/gasparottog80-hash/PAPELA.AI/actions/runs"
        f"?head_sha={commit}&event=push&per_page=30"
    )
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "papela-release/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.load(response)
    except Exception as exc:
        raise ReleaseError("CI_STATUS_UNAVAILABLE") from exc
    if not any(
        item.get("head_sha") == commit
        and item.get("head_branch") == "master"
        and item.get("name") == "Reproducible baseline"
        and item.get("conclusion") == "success"
        for item in data.get("workflow_runs", [])
    ):
        raise ReleaseError("CI_NOT_GREEN")


def validate_manifest(manifest: dict[str, Any], *, target: bool = True) -> None:
    commit = manifest.get("commit_sha")
    if not isinstance(commit, str) or not SHA.fullmatch(commit):
        raise ReleaseError("RELEASE_SHA_INVALID")
    tag = f"papelaai:sha-{commit}"
    if manifest.get("image_tag") != tag:
        raise ReleaseError("RELEASE_TAG_INVALID")
    image_id, label = image_details(tag)
    if image_id != manifest.get("image_id") or label != commit:
        raise ReleaseError("IMAGE_PROVENANCE_MISMATCH")
    if target:
        if manifest.get("extractor_commit") != extractor_commit():
            raise ReleaseError("EXTRACTOR_PROVENANCE_MISMATCH")
        if manifest.get("schema_fingerprint") != schema_fingerprint():
            raise ReleaseError("SCHEMA_FINGERPRINT_MISMATCH")
    if manifest.get("scan") != {"critical_high": 0, "secrets": 0}:
        raise ReleaseError("SCAN_ATTESTATION_INVALID")


def backup_verified(args: argparse.Namespace, image: str) -> None:
    if not BACKUP_ID.fullmatch(args.backup_id):
        raise ReleaseError("BACKUP_ID_INVALID")
    # The host operator cannot read a UID-70, mode-0700 archive directory.
    # The fixed-format ID is UTC; the isolated backup container verifies
    # archive SHA-256, TOC and metadata with its own read permission.
    created = datetime.strptime(args.backup_id[7:23], "%Y%m%dT%H%M%SZ").replace(
        tzinfo=UTC
    )
    age = (datetime.now(UTC) - created).total_seconds()
    if not 0 <= age <= 26 * 3600:
        raise ReleaseError("BACKUP_STALE")
    compose(
        args,
        image,
        "--profile",
        "ops",
        "run",
        "--rm",
        "backup",
        "verify",
        args.backup_id,
    )


def preflight(
    args: argparse.Namespace, manifest: dict[str, Any]
) -> dict[str, Any] | None:
    validate_manifest(manifest)
    values = env_file(args.env_file)
    for name in (
        "PAPELA_PG_ADMIN_PASSWORD_FILE",
        "PAPELA_PG_RUNTIME_PASSWORD_FILE",
        "PAPELA_DATABASE_URL_FILE",
        "PAPELA_TENANT_API_KEYS_FILE",
    ):
        secret_path = Path(values.get(name, ""))
        if not secret_path.is_file():
            raise ReleaseError("SECRET_FILE_MISSING")
        if os.name != "nt" and secret_path.stat().st_mode & 0o077:
            raise ReleaseError("SECRET_FILE_PERMISSIONS")
    if not args.smoke_key_file.is_file():
        raise ReleaseError("SMOKE_KEY_MISSING")
    if shutil.disk_usage(Path(values["PAPELA_BACKUP_DIR"])).free < 512 * 1024 * 1024:
        raise ReleaseError("DISK_SPACE_LOW")
    current = active(args.state_dir)
    if current is None and not args.initial:
        raise ReleaseError("CURRENT_RELEASE_UNKNOWN")
    if current is None and args.initial:
        for service in ("api", "worker"):
            if compose(args, manifest["image_tag"], "ps", "-a", "-q", service):
                raise ReleaseError("INITIAL_DEPLOY_FOUND_EXISTING_SERVICE")
    if current is not None:
        previous = read_json(manifest_path(args.state_dir, current["commit_sha"]))
        validate_manifest(previous, target=False)
        if values.get("PAPELA_APP_IMAGE") != previous["image_tag"]:
            raise ReleaseError("ACTIVE_ENV_IMAGE_DRIFT")
        if previous["schema_fingerprint"] != manifest["schema_fingerprint"]:
            if not args.migration_review:
                raise ReleaseError("SCHEMA_CHANGE_REQUIRES_MANUAL_PLAN")
            review = read_json(args.migration_review)
            if not (
                review.get("from_schema") == previous["schema_fingerprint"]
                and review.get("to_schema") == manifest["schema_fingerprint"]
                and review.get("classification") == "backward-compatible"
                and review.get("old_app_compatible") is True
                and isinstance(review.get("reviewed_by"), str)
                and len(review["reviewed_by"]) >= 3
            ):
                raise ReleaseError("MIGRATION_REVIEW_INVALID")
        service_images(args, previous)
        readiness(args)
    elif args.initial and args.migration_class != "backward-compatible":
        raise ReleaseError("INITIAL_MIGRATION_REVIEW_REQUIRED")
    if args.migration_class == "destructive":
        raise ReleaseError("DESTRUCTIVE_MIGRATION_FORBIDDEN")
    if not args.synthetic:
        ci_green(manifest["commit_sha"])
    elif not (
        (
            values.get("PAPELA_PROJECT_NAME", "").startswith("papela-gate7-")
            or values.get("PAPELA_PROJECT_NAME") == "papela-gate4-ci"
        )
        and args.smoke_url.startswith("https://localhost:")
    ):
        raise ReleaseError("SYNTHETIC_SCOPE_INVALID")
    compose(args, manifest["image_tag"], "config", "--quiet")
    backup_verified(args, manifest["image_tag"])
    return current


def readiness(args: argparse.Namespace) -> None:
    request = urllib.request.Request(args.smoke_url.rstrip("/") + "/readiness")
    context = ssl.create_default_context(
        cafile=str(args.ca_file) if args.ca_file else None
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
    )
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            with opener.open(request, timeout=4) as response:
                if response.status == 200 and json.load(response) == {
                    "status": "ready"
                }:
                    return
        except Exception:
            pass
        time.sleep(2)
    raise ReleaseError("READINESS_TIMEOUT")


def service_images(args: argparse.Namespace, manifest: dict[str, Any]) -> None:
    for service in ("api", "worker"):
        container = compose(args, manifest["image_tag"], "ps", "-q", service)
        if not container:
            raise ReleaseError("SERVICE_NOT_RUNNING")
        image = run("docker", "inspect", "--format", "{{.Image}}", container)
        if image != manifest["image_id"]:
            raise ReleaseError("ACTIVE_IMAGE_MISMATCH")
        health = run(
            "docker", "inspect", "--format", "{{.State.Health.Status}}", container
        )
        if health != "healthy":
            raise ReleaseError("SERVICE_UNHEALTHY")


def smoke(args: argparse.Namespace) -> None:
    run(
        sys.executable,
        str(ROOT / "scripts" / "smoke_release.py"),
        "--url",
        args.smoke_url,
        "--key-file",
        str(args.smoke_key_file),
        *(["--ca", str(args.ca_file)] if args.ca_file else []),
        *(
            ["--other-key-file", str(args.smoke_other_key_file)]
            if args.smoke_other_key_file
            else []
        ),
        capture=False,
    )


def synthetic_ca(args: argparse.Namespace, image: str) -> None:
    if not args.synthetic or not args.ca_file or args.ca_file.is_file():
        return
    container = compose(args, image, "ps", "-q", "caddy")
    if not container:
        raise ReleaseError("SYNTHETIC_CADDY_MISSING")
    run(
        "docker",
        "cp",
        f"{container}:/data/caddy/pki/authorities/local/root.crt",
        str(args.ca_file),
        capture=False,
    )


def record(args: argparse.Namespace) -> None:
    commit = args.commit
    if not SHA.fullmatch(commit):
        raise ReleaseError("RELEASE_SHA_INVALID")
    run("git", "cat-file", "-e", f"{commit}^{{commit}}")
    tag = f"papelaai:sha-{commit}"
    with exclusive_lock(args.state_dir):
        path = manifest_path(args.state_dir, commit)
        if path.exists():
            raise ReleaseError("RELEASE_ALREADY_RECORDED")
        image_id, label = image_details(tag)
        if label != commit:
            raise ReleaseError("IMAGE_PROVENANCE_MISMATCH")
        provenance = json.loads(
            run(
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                tag,
                "python",
                "scripts/verify_extractor.py",
            )
        )
        if provenance.get("installed_commit") != extractor_commit():
            raise ReleaseError("EXTRACTOR_PROVENANCE_MISMATCH")
        scan = scan_image(tag)
        manifest = {
            "commit_sha": commit,
            "image_tag": tag,
            "image_id": image_id,
            "registry_digest": None,
            "created_at_utc": utc(),
            "extractor_commit": extractor_commit(),
            "schema_fingerprint": schema_fingerprint(),
            "scan": scan,
            "migration_applied": False,
            "backup_id": None,
            "previous_release": None,
            "status": "candidate",
        }
        write_json(path, manifest)
        event("release_recorded", commit, image_id=image_id)


def deploy(args: argparse.Namespace) -> None:
    if not args.execute:
        raise ReleaseError("EXECUTE_FLAG_REQUIRED")
    with exclusive_lock(args.state_dir):
        manifest = read_json(manifest_path(args.state_dir, args.commit))
        event("deploy_started", args.commit)
        previous: dict[str, Any] | None = None
        changed = False
        try:
            previous = preflight(args, manifest)
            if previous and previous["commit_sha"] == args.commit:
                raise ReleaseError("ALREADY_ACTIVE")
            migration_needed = (
                previous is None
                or previous["schema_fingerprint"] != manifest["schema_fingerprint"]
            )
            if migration_needed:
                event("migration_started", args.commit)
                try:
                    compose(
                        args, manifest["image_tag"], "up", "-d", "--wait", "postgres"
                    )
                    compose(
                        args,
                        manifest["image_tag"],
                        "--profile",
                        "ops",
                        "run",
                        "--rm",
                        "migrate",
                    )
                except ReleaseError:
                    event(
                        "migration_failed", args.commit, error_code="MIGRATION_FAILED"
                    )
                    raise
                event("migration_succeeded", args.commit)
                manifest["migration_applied"] = True
                if previous is not None:
                    manifest["migration_review_sha256"] = sha256(args.migration_review)
                    manifest["old_app_compatible"] = True
            changed = True
            compose(
                args,
                manifest["image_tag"],
                "up",
                "-d",
                "--force-recreate",
                "--wait",
                "api",
                "worker",
                "caddy",
            )
            synthetic_ca(args, manifest["image_tag"])
            readiness(args)
            service_images(args, manifest)
            smoke(args)
            if args.simulate_post_deploy_failure:
                raise ReleaseError("SYNTHETIC_FAILURE_INJECTED")
            set_env_image(args.env_file, manifest["image_tag"])
            manifest.update(
                status="active",
                backup_id=args.backup_id,
                previous_release=previous["commit_sha"] if previous else None,
                promoted_at_utc=utc(),
            )
            write_json(manifest_path(args.state_dir, args.commit), manifest)
            write_json(
                args.state_dir / "active.json",
                {"commit_sha": args.commit, "image_id": manifest["image_id"]},
            )
            event("deploy_succeeded", args.commit, backup_id=args.backup_id)
        except (ReleaseError, OSError, ValueError, KeyError) as exc:
            code = str(exc) if isinstance(exc, ReleaseError) else "DEPLOY_RUNTIME_ERROR"
            event("deploy_failed", args.commit, error_code=code)
            if (
                changed
                and previous
                and (
                    previous["schema_fingerprint"] == manifest["schema_fingerprint"]
                    or manifest.get("old_app_compatible") is True
                )
            ):
                rollback_to(args, previous)
            raise


def rollback_to(args: argparse.Namespace, previous: dict[str, Any]) -> None:
    commit = previous["commit_sha"]
    event("rollback_started", commit)
    try:
        validate_manifest(previous, target=False)
        compose(
            args,
            previous["image_tag"],
            "up",
            "-d",
            "--force-recreate",
            "--no-deps",
            "--wait",
            "api",
            "worker",
        )
        readiness(args)
        service_images(args, previous)
        smoke(args)
        set_env_image(args.env_file, previous["image_tag"])
        write_json(
            args.state_dir / "active.json",
            {"commit_sha": commit, "image_id": previous["image_id"]},
        )
        previous["status"] = "active"
        previous["promoted_at_utc"] = utc()
        write_json(manifest_path(args.state_dir, commit), previous)
        event("rollback_succeeded", commit)
    except (ReleaseError, OSError, ValueError, KeyError):
        event("rollback_failed", commit, error_code="ROLLBACK_VALIDATION_FAILED")
        raise


def rollback(args: argparse.Namespace) -> None:
    if not args.execute:
        raise ReleaseError("EXECUTE_FLAG_REQUIRED")
    with exclusive_lock(args.state_dir):
        values = env_file(args.env_file)
        if args.synthetic and not (
            (
                values.get("PAPELA_PROJECT_NAME", "").startswith("papela-gate7-")
                or values.get("PAPELA_PROJECT_NAME") == "papela-gate4-ci"
            )
            and args.smoke_url.startswith("https://localhost:")
        ):
            raise ReleaseError("SYNTHETIC_SCOPE_INVALID")
        current = active(args.state_dir)
        if not current:
            raise ReleaseError("CURRENT_RELEASE_UNKNOWN")
        deployed = read_json(manifest_path(args.state_dir, current["commit_sha"]))
        previous_sha = deployed.get("previous_release")
        if not previous_sha:
            raise ReleaseError("PREVIOUS_RELEASE_UNKNOWN")
        previous = read_json(manifest_path(args.state_dir, previous_sha))
        if (
            previous["schema_fingerprint"] != deployed["schema_fingerprint"]
            and deployed.get("old_app_compatible") is not True
        ):
            raise ReleaseError("SCHEMA_INCOMPATIBLE_ROLLBACK_BLOCKED")
        if not args.synthetic:
            ci_green(previous_sha)
        rollback_to(args, previous)
        deployed["status"] = "rolled_back"
        write_json(manifest_path(args.state_dir, deployed["commit_sha"]), deployed)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("record", "preflight", "deploy", "rollback", "status")
    )
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--commit", default="")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--backup-id", default="")
    parser.add_argument("--smoke-url", default="")
    parser.add_argument("--smoke-key-file", type=Path)
    parser.add_argument("--smoke-other-key-file", type=Path)
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--initial", action="store_true")
    parser.add_argument(
        "--migration-class",
        choices=("none", "backward-compatible", "destructive"),
        default="none",
    )
    parser.add_argument("--migration-review", type=Path)
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--simulate-post-deploy-failure", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.command == "status":
        print(json.dumps(active(args.state_dir) or {"status": "unknown"}))
        return
    if args.command == "record":
        record(args)
        return
    if (
        not args.env_file
        or not args.smoke_key_file
        or not args.smoke_url
        or not args.backup_id
    ):
        raise ReleaseError("PREFLIGHT_ARGUMENT_MISSING")
    if args.simulate_post_deploy_failure and not args.synthetic:
        raise ReleaseError("FAILURE_INJECTION_SYNTHETIC_ONLY")
    if args.command == "rollback":
        rollback(args)
    elif args.command == "deploy":
        deploy(args)
    else:
        with exclusive_lock(args.state_dir):
            manifest = read_json(manifest_path(args.state_dir, args.commit))
            preflight(args, manifest)
            event("preflight_succeeded", args.commit, backup_id=args.backup_id)


if __name__ == "__main__":
    try:
        main()
    except (ReleaseError, KeyError, ValueError, OSError) as exc:
        code = str(exc) if isinstance(exc, ReleaseError) else "INVALID_RELEASE_INPUT"
        print(
            json.dumps(
                {"service": "release", "event": "command_failed", "error_code": code}
            ),
            file=sys.stderr,
        )
        sys.exit(1)
