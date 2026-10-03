"""Isolated Gate 7 A -> B -> A drill; never accepts a public endpoint."""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from ops import release

COMPOSE_KEYS = {
    "PAPELA_PG_ADMIN_PASSWORD_FILE",
    "PAPELA_PG_RUNTIME_PASSWORD_FILE",
    "PAPELA_DATABASE_URL_FILE",
    "PAPELA_TENANT_API_KEYS_FILE",
    "PAPELA_MAX_STORAGE_BYTES",
    "PAPELA_LOG_MAX_SIZE",
    "PAPELA_LOG_MAX_FILES",
}


def write_secret(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


def backup(args: argparse.Namespace, image: str) -> str:
    output = release.compose(
        args, image, "--profile", "ops", "run", "--rm", "backup", "backup"
    )
    matches = re.findall(
        r"^BACKUP_ID=(backup-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8})$", output, re.M
    )
    if len(matches) != 1:
        raise release.ReleaseError("SYNTHETIC_BACKUP_ID_MISSING")
    return matches[0]


def canary(args: argparse.Namespace, key: Path, other: Path) -> str:
    output = release.run(
        sys.executable,
        str(release.ROOT / "scripts" / "smoke_release.py"),
        "--url",
        args.smoke_url,
        "--key-file",
        str(key),
        "--other-key-file",
        str(other),
        "--ca",
        str(args.ca_file),
    )
    return json.loads(output)["job_id"]


def verify_job(args: argparse.Namespace, job_id: str, owner: Path, other: Path) -> None:
    context = ssl.create_default_context(cafile=str(args.ca_file))
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
    )

    def status(key: Path) -> int:
        request = urllib.request.Request(
            f"{args.smoke_url}/v1/jobs/{job_id}",
            headers={"X-API-Key": key.read_text(encoding="utf-8").strip()},
        )
        try:
            with opener.open(request, timeout=10) as response:
                body = json.load(response)
                if body.get("status") != "done":
                    raise release.ReleaseError("PERSISTED_JOB_NOT_DONE")
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    if status(owner) != 200 or status(other) != 404:
        raise release.ReleaseError("PERSISTENCE_OR_TENANT_ISOLATION_FAILED")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-commit", required=True)
    parser.add_argument("--target-commit", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--base-env", type=Path)
    parser.add_argument("--database-url-file", type=Path)
    parser.add_argument(
        "--project", choices=("papela-gate7-local", "papela-gate7-ci"), required=True
    )
    parser.add_argument("--http-port", type=int, default=18082)
    parser.add_argument("--https-port", type=int, default=18445)
    options = parser.parse_args()
    for commit in (options.previous_commit, options.target_commit):
        if not release.SHA.fullmatch(commit):
            raise release.ReleaseError("SYNTHETIC_SHA_INVALID")
    if (
        not 1024 <= options.http_port <= 65535
        or not 1024 <= options.https_port <= 65535
    ):
        raise release.ReleaseError("SYNTHETIC_PORT_INVALID")
    if options.state_dir.exists() and any(options.state_dir.iterdir()):
        raise release.ReleaseError("SYNTHETIC_STATE_NOT_EMPTY")
    options.state_dir.mkdir(parents=True, exist_ok=True)
    state = options.state_dir.resolve()
    values = (
        release.env_file(options.base_env)
        if options.base_env
        else {key: value for key, value in os.environ.items() if key in COMPOSE_KEYS}
    )
    values = {key: value for key, value in values.items() if key in COMPOSE_KEYS}
    if options.database_url_file:
        values["PAPELA_DATABASE_URL_FILE"] = (
            options.database_url_file.resolve().as_posix()
        )
    required = (
        "PAPELA_PG_ADMIN_PASSWORD_FILE",
        "PAPELA_PG_RUNTIME_PASSWORD_FILE",
        "PAPELA_DATABASE_URL_FILE",
        "PAPELA_TENANT_API_KEYS_FILE",
    )
    if any(not Path(values.get(name, "")).is_file() for name in required):
        raise release.ReleaseError("SYNTHETIC_SECRET_PATH_MISSING")
    values.update(
        PAPELA_PROJECT_NAME=options.project,
        PAPELA_DOMAIN="localhost",
        PAPELA_APP_IMAGE=f"papelaai:sha-{options.previous_commit}",
        PAPELA_HTTP_BIND="127.0.0.1",
        PAPELA_HTTPS_BIND="127.0.0.1",
        PAPELA_HTTP_PORT=str(options.http_port),
        PAPELA_HTTPS_PORT=str(options.https_port),
        PAPELA_BACKUP_DIR=(state / "backups").as_posix(),
        PAPELA_MAX_STORAGE_BYTES="536870912",
    )
    env_path = state / ".env.synthetic"
    env_path.write_text(
        "".join(f"{key}={value}\n" for key, value in sorted(values.items())),
        encoding="utf-8",
    )
    backup_dir = state / "backups"
    backup_dir.mkdir(mode=0o700)
    if os.name != "nt":
        subprocess.run(["sudo", "chown", "70:70", str(backup_dir)], check=True)
    # These are public test-only values already used by Gate 4; never read or
    # echo the production tenant-key mapping from its protected file.
    primary = "gate4-synthetic-tenant-key-11111111111111111111111111"
    secondary = "gate4-synthetic-tenant-key-22222222222222222222222222"
    first_key, second_key = state / "tenant-a.key", state / "tenant-b.key"
    write_secret(first_key, primary)
    write_secret(second_key, secondary)
    args = argparse.Namespace(
        state_dir=state,
        env_file=env_path,
        backup_id="",
        smoke_url=f"https://localhost:{options.https_port}",
        smoke_key_file=first_key,
        smoke_other_key_file=second_key,
        ca_file=state / "root.crt",
        initial=True,
        migration_class="backward-compatible",
        migration_review=None,
        synthetic=True,
        simulate_post_deploy_failure=False,
        execute=True,
        commit=options.previous_commit,
    )
    image_a = f"papelaai:sha-{options.previous_commit}"
    release.compose(args, image_a, "config", "--quiet")
    release.compose(args, image_a, "up", "-d", "--wait", "postgres")
    args.backup_id = backup(args, image_a)
    release.record(args)
    args.commit = options.target_commit
    release.record(args)
    args.commit = options.previous_commit
    release.deploy(args)
    first_job = canary(args, first_key, second_key)
    second_job = canary(args, second_key, first_key)
    args.backup_id = backup(args, image_a)
    args.initial = False
    args.migration_class = "none"
    args.commit = options.target_commit
    args.simulate_post_deploy_failure = True
    try:
        release.deploy(args)
    except release.ReleaseError as exc:
        if str(exc) != "SYNTHETIC_FAILURE_INJECTED":
            raise
    else:
        raise release.ReleaseError("FAILURE_INJECTION_NOT_EXERCISED")
    if release.active(state)["commit_sha"] != options.previous_commit:
        raise release.ReleaseError("AUTO_ROLLBACK_FAILED")
    verify_job(args, first_job, first_key, second_key)
    verify_job(args, second_job, second_key, first_key)
    args.simulate_post_deploy_failure = False
    release.deploy(args)
    if release.active(state)["commit_sha"] != options.target_commit:
        raise release.ReleaseError("PROMOTION_FAILED")
    release.rollback(args)
    if release.active(state)["commit_sha"] != options.previous_commit:
        raise release.ReleaseError("MANUAL_ROLLBACK_FAILED")
    verify_job(args, first_job, first_key, second_key)
    verify_job(args, second_job, second_key, first_key)
    fresh_job = canary(args, first_key, second_key)
    verify_job(args, fresh_job, first_key, second_key)
    release.compose(args, image_a, "restart", "postgres", "api", "worker", "caddy")
    release.compose(args, image_a, "up", "-d", "--wait", "api", "worker", "caddy")
    release.readiness(args)
    release.service_images(
        args, release.read_json(release.manifest_path(state, options.previous_commit))
    )
    for job, owner, other in (
        (first_job, first_key, second_key),
        (second_job, second_key, first_key),
        (fresh_job, first_key, second_key),
    ):
        verify_job(args, job, owner, other)
    rows = release.run(
        "docker",
        "exec",
        f"{options.project}-postgres-1",
        "psql",
        "-XAtq",
        "-U",
        "papela_owner",
        "-d",
        "papela",
        "-c",
        "SELECT count(DISTINCT tenant_id), count(*) FROM jobs",
    )
    tenants, jobs = (int(value) for value in rows.split("|"))
    if tenants != 2 or jobs < 3:
        raise release.ReleaseError("DATABASE_CONSISTENCY_FAILED")
    print(
        json.dumps(
            {
                "status": "PASS",
                "flow": "A-B-auto-A-B-manual-A",
                "tenants": tenants,
                "jobs": jobs,
                "active_commit": release.active(state)["commit_sha"],
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (release.ReleaseError, OSError, ValueError, KeyError) as exc:
        code = (
            str(exc)
            if isinstance(exc, release.ReleaseError)
            else "SYNTHETIC_DRILL_FAILED"
        )
        print(
            json.dumps({"event": "gate7_drill_failed", "error_code": code}),
            file=sys.stderr,
        )
        sys.exit(1)
