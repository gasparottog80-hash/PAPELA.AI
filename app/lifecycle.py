"""Tenant-scoped lifecycle operations; CLI is operator-only, never HTTP.

All destructive operations write and fsync the independent journal first.
If later cleanup fails, the generation mismatch blocks readiness and API
access until an operator completes reconciliation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from .config import Settings, get_settings
from .db import Database
from .erasure import ErasureJournal, JournalError, _uuid
from .repository import JobRepository
from .storage import delete_pdf

logger = logging.getLogger("papela.privacy")


def _subject(tenant_id: str) -> str:
    # Operational pseudonym, not a claim of anonymization.
    return hashlib.sha256(tenant_id.encode("ascii")).hexdigest()[:16]


def ready(repo: JobRepository, journal: ErasureJournal) -> bool:
    try:
        generation, _, _ = journal.state()
        return repo.privacy_generation() == generation
    except (JournalError, OSError, RuntimeError):
        return False


def create_job_if_enabled(
    repo: JobRepository,
    journal: ErasureJournal,
    *,
    job_id: str,
    tenant_id: str,
    filename: str,
    storage_path: str,
    size_bytes: int,
    pages: int,
) -> bool:
    """Serialize admission with offboarding so no late upload can resurrect data."""
    with journal.locked():
        index = journal._validate_unlocked()
        _require_synced(repo, index)
        if any(
            event["tenant_id"] == tenant_id
            and event["type"]
            in {"tenant_disabled", "tenant_deleted", "api_key_revoked"}
            for event in journal._events_unlocked(index)
        ):
            return False
        repo.create(
            job_id=job_id,
            tenant_id=tenant_id,
            filename=filename,
            storage_path=storage_path,
            size_bytes=size_bytes,
            pages=pages,
        )
        return True


def visible_job(
    repo: JobRepository,
    journal: ErasureJournal,
    tenant_id: str,
    job_id: str,
) -> dict[str, Any] | None:
    """Serialize reads with erasure, then apply the tenant/job denylist."""
    with journal.locked():
        index = journal._validate_unlocked()
        _require_synced(repo, index)
        for event in journal._events_unlocked(index):
            if event["tenant_id"] != tenant_id:
                continue
            if event["type"] in {
                "tenant_disabled",
                "tenant_deleted",
                "api_key_revoked",
            }:
                return None
            if event["job_id"] == job_id and event["type"] in {
                "job_deleted",
                "result_expired",
            }:
                return None
        return repo.get_for_tenant(job_id, tenant_id)


def _require_synced(repo: JobRepository, index: dict[str, Any]) -> None:
    if repo.privacy_generation() != index["generation"]:
        raise JournalError("reconciliation required")


def delete_job(
    repo: JobRepository,
    journal: ErasureJournal,
    settings: Settings,
    tenant_id: str,
    job_id: str,
    *,
    event_type: str = "job_deleted",
) -> bool:
    """Owner-only terminal deletion; repeat returns true, unknown returns false."""
    tenant_id, job_id = _uuid(tenant_id), _uuid(job_id)
    with journal.locked():
        index = journal._validate_unlocked()
        _require_synced(repo, index)
        existing = journal._events_unlocked(index)
        if any(
            e["type"] in {"job_deleted", "result_expired"}
            and e["tenant_id"] == tenant_id
            and e["job_id"] == job_id
            for e in existing
        ):
            return True
        if any(
            e["type"] in {"tenant_disabled", "tenant_deleted", "api_key_revoked"}
            and e["tenant_id"] == tenant_id
            for e in existing
        ):
            raise JournalError("tenant disabled")
        row = repo.get_for_tenant(job_id, tenant_id)
        if row is None:
            return False
        if row["status"] not in {"done", "failed"}:
            raise ValueError("job is not terminal")
        generation = journal.record_locked(index, event_type, tenant_id, job_id)
        delete_pdf(
            job_id,
            settings.storage_dir,
            reason=(
                "tenant-delete" if event_type == "job_deleted" else "result-retention"
            ),
        )
        repo.delete_terminal_and_advance(job_id, tenant_id, generation)
    logger.info(
        "data_deleted", extra={"subject_ref": _subject(tenant_id), "job_id": job_id}
    )
    return True


def purge_expired_results(
    repo: JobRepository,
    journal: ErasureJournal,
    settings: Settings,
) -> int:
    removed = 0
    for job_id, tenant_id in repo.expired_terminal_jobs(
        retention_days=settings.job_retention_days
    ):
        removed += int(
            delete_job(
                repo, journal, settings, tenant_id, job_id, event_type="result_expired"
            )
        )
    if removed:
        logger.info("retention.results_purged", extra={"count": removed})
    return removed


def offboard_tenant(
    repo: JobRepository,
    journal: ErasureJournal,
    settings: Settings,
    tenant_id: str,
) -> int:
    tenant_id = _uuid(tenant_id)
    with journal.locked():
        index = journal._validate_unlocked()
        _require_synced(repo, index)
        events = journal._events_unlocked(index)
        if any(
            e["type"] == "tenant_deleted" and e["tenant_id"] == tenant_id
            for e in events
        ):
            return 0
        generation = journal.record_locked(index, "tenant_deleted", tenant_id)
        # No worker can INSERT a job, and the marker denies API access before
        # any data removal. An in-flight worker UPDATE affects zero deleted rows.
        rows = repo.tenant_jobs(tenant_id)
        for row in rows:
            delete_pdf(row["id"], settings.storage_dir, reason="tenant-offboard")
        removed = repo.delete_tenant_and_advance(tenant_id, generation)
    logger.info(
        "tenant_deleted", extra={"subject_ref": _subject(tenant_id), "count": removed}
    )
    return removed


def revoke_tenant(repo: JobRepository, journal: ErasureJournal, tenant_id: str) -> None:
    tenant_id = _uuid(tenant_id)
    with journal.locked():
        index = journal._validate_unlocked()
        _require_synced(repo, index)
        generation = journal.record_locked(index, "api_key_revoked", tenant_id)
        if generation != index["generation"]:
            repo.advance_privacy_generation(generation)
    logger.info("api_key_revoked", extra={"subject_ref": _subject(tenant_id)})


def record_rotation(
    repo: JobRepository, journal: ErasureJournal, tenant_id: str
) -> None:
    tenant_id = _uuid(tenant_id)
    with journal.locked():
        index = journal._validate_unlocked()
        _require_synced(repo, index)
        generation = journal.record_locked(index, "api_key_rotated", tenant_id)
        repo.advance_privacy_generation(generation)
    logger.info("api_key_rotated", extra={"subject_ref": _subject(tenant_id)})


def reconcile(repo: JobRepository, journal: ErasureJournal, settings: Settings) -> int:
    """Replay all validated markers; caller must keep API/worker stopped."""
    with journal.locked():
        index = journal._validate_unlocked()
        if repo.privacy_generation_raw() > index["generation"]:
            raise JournalError("journal older than database; recovery unsafe")
        if repo.privacy_generation_raw() < index.get("floor_generation", 0):
            raise JournalError("restored backup predates compacted journal floor")
        tenants: set[str] = set()
        jobs: set[tuple[str, str]] = set()
        for event in journal._events_unlocked(index):
            if event["type"] in {
                "tenant_disabled",
                "tenant_deleted",
                "api_key_revoked",
            }:
                tenants.add(event["tenant_id"])
            elif event["type"] in {"job_deleted", "result_expired"}:
                jobs.add((event["tenant_id"], event["job_id"]))
        for tenant_id in sorted(tenants):
            for row in repo.tenant_jobs(tenant_id):
                delete_pdf(row["id"], settings.storage_dir, reason="restore-erasure")
        for tenant_id, job_id in sorted(jobs):
            if tenant_id not in tenants and repo.get_for_tenant(job_id, tenant_id):
                delete_pdf(job_id, settings.storage_dir, reason="restore-erasure")
        removed = repo.reconcile_erasures(tenants, jobs, index["generation"])
    logger.info("erasure.reconciled", extra={"count": removed})
    return removed


def export_tenant(
    repo: JobRepository,
    journal: ErasureJournal,
    tenant_id: str,
    output: Path,
) -> int:
    tenant_id = _uuid(tenant_id)
    if not output.is_absolute() or output.is_symlink():
        raise ValueError("export path must be absolute and new")
    parent = output.parent
    if not parent.is_dir() or parent.is_symlink() or parent.resolve() != parent:
        raise ValueError("export directory unavailable")
    if os.name != "nt" and parent.stat().st_mode & 0o077:
        raise ValueError("export directory permissions too broad")
    with journal.locked():
        index = journal._validate_unlocked()
        _require_synced(repo, index)
        if any(
            e["type"] in {"tenant_disabled", "tenant_deleted", "api_key_revoked"}
            and e["tenant_id"] == tenant_id
            for e in journal._events_unlocked(index)
        ):
            raise JournalError("tenant disabled")
        rows = repo.tenant_jobs(tenant_id)
        document = {
            "format": "papela-tenant-export-v1",
            "tenant_id": tenant_id,
            "jobs": rows,
        }
        payload = json.dumps(
            document,
            default=lambda v: v.isoformat(),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(output, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            output.unlink(missing_ok=True)
            raise
    logger.info(
        "data_exported", extra={"subject_ref": _subject(tenant_id), "count": len(rows)}
    )
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="PAPELA.AI privacy operator command")
    parser.add_argument(
        "action",
        choices=(
            "init",
            "export",
            "offboard",
            "reconcile",
            "revoke-key",
            "record-key-rotation",
            "compact",
        ),
    )
    parser.add_argument("--tenant")
    parser.add_argument("--confirm-tenant")
    parser.add_argument("--database")
    parser.add_argument("--confirm-database")
    parser.add_argument("--confirm-journal")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.action != "compact" and (
        not args.execute or os.environ.get("PAPELA_ALLOW_PRIVACY_OPS") != "1"
    ):
        parser.error("explicit --execute and PAPELA_ALLOW_PRIVACY_OPS=1 required")
    settings = get_settings()
    from .logging_config import configure_logging

    configure_logging(settings.log_level, "api")
    if args.action == "init":
        ErasureJournal.initialize(settings.erasure_dir)
        return
    if args.action not in {"reconcile", "compact"}:
        if not args.tenant or args.confirm_tenant != args.tenant:
            parser.error("exact --tenant and --confirm-tenant required")
        _uuid(args.tenant)
    db = Database(settings)
    db.open()
    try:
        repo = JobRepository(db.pool)
        journal = ErasureJournal(settings.erasure_dir)
        if args.action == "compact":
            from .journal_compaction import compact_journal

            if args.execute and (
                settings.env != "test"
                or os.environ.get("PAPELA_ALLOW_PRIVACY_OPS") != "1"
                or args.database != repo.database_name()
                or args.confirm_database != args.database
                or args.confirm_journal != str(Path(settings.erasure_dir).resolve())
            ):
                parser.error(
                    "synthetic test environment and exact confirmations required"
                )
            print(json.dumps(compact_journal(repo, journal, execute=args.execute)))
        elif args.action == "export":
            if args.output is None:
                parser.error("--output required")
            export_tenant(repo, journal, args.tenant, args.output)
        elif args.action == "offboard":
            offboard_tenant(repo, journal, settings, args.tenant)
        elif args.action == "reconcile":
            name = repo.database_name()
            if (args.database != name or args.confirm_database != name
                    or (settings.env == "production"
                        and not name.startswith("papela_restore_"))):
                parser.error("exact restored database confirmation required")
            reconcile(repo, journal, settings)
        elif args.action == "revoke-key":
            revoke_tenant(repo, journal, args.tenant)
        else:
            record_rotation(repo, journal, args.tenant)
    finally:
        db.close()


if __name__ == "__main__":
    main()
