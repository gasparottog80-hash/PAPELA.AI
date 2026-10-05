"""Conservative, independently stored backup inventory for journal compaction.

The checksum detects accidental damage, not a malicious privileged operator.
Completeness is an explicit operator attestation; absent external verification,
offsite copies are never accepted as destroyed by this implementation.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .erasure import (
    ErasureJournal,
    JournalError,
    _atomic_json,
    _check_mode,
    _digest,
    _read_json,
)

SHA256 = re.compile(r"[0-9a-f]{64}\Z")
BACKUP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def _utc(value: Any) -> datetime:
    if not isinstance(value, str):
        raise JournalError("inventory timestamp invalid")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise JournalError("inventory timestamp invalid") from exc
    if result.tzinfo != UTC:
        raise JournalError("inventory timestamp must be UTC")
    return result


def validate_inventory(path: Path) -> dict[str, Any]:
    """Fail closed on missing, incomplete or unverifiable inventory state."""
    inventory = _read_json(path)
    if set(inventory) != {"version", "coverage", "backups", "sha256"}:
        raise JournalError("inventory schema invalid")
    body = {key: inventory[key] for key in ("version", "coverage", "backups")}
    if type(inventory["version"]) is not int or inventory["version"] != 1 \
            or inventory["sha256"] != _digest(body):
        raise JournalError("inventory integrity invalid")
    coverage = inventory["coverage"]
    if not isinstance(coverage, dict) or set(coverage) != {
        "source_database", "schema_version", "local_root",
        "local_complete", "offsite_complete",
        "verified_at_utc",
    }:
        raise JournalError("inventory coverage invalid")
    if (
        not isinstance(coverage["source_database"], str)
        or not coverage["source_database"]
        or not isinstance(coverage["schema_version"], str)
        or not coverage["schema_version"]
        or coverage["local_complete"] is not True
        or coverage["offsite_complete"] is not True
    ):
        raise JournalError("inventory incomplete")
    coverage_verified = _utc(coverage["verified_at_utc"])
    if coverage_verified > datetime.now(UTC) + timedelta(minutes=5):
        raise JournalError("inventory coverage verification in future")
    if not isinstance(coverage["local_root"], str):
        raise JournalError("inventory local root invalid")
    local_root = Path(coverage["local_root"])
    if not local_root.is_absolute() or local_root.is_symlink():
        raise JournalError("inventory local root invalid")
    _check_mode(local_root, directory=True)
    if not isinstance(inventory["backups"], list):
        raise JournalError("inventory backups invalid")
    seen: set[str] = set()
    listed_present: set[Path] = set()
    for backup in inventory["backups"]:
        if not isinstance(backup, dict) or set(backup) != {
            "backup_id", "created_at_utc", "source_database", "schema_version",
            "privacy_generation", "checksum", "location", "storage_class",
            "expires_at_utc", "status", "verified_at_utc", "legal_hold",
            "marker_horizon_generation",
        }:
            raise JournalError("inventory backup schema invalid")
        backup_id = backup["backup_id"]
        if (
            not isinstance(backup_id, str)
            or not BACKUP_ID.fullmatch(backup_id)
            or backup_id in seen
        ):
            raise JournalError("inventory backup ID invalid or duplicate")
        seen.add(backup_id)
        if (
            backup["source_database"] != coverage["source_database"]
            or backup["schema_version"] != coverage["schema_version"]
            or type(backup["privacy_generation"]) is not int
            or backup["privacy_generation"] < 0
            or type(backup["marker_horizon_generation"]) is not int
            or backup["marker_horizon_generation"] != backup["privacy_generation"]
            or not isinstance(backup["checksum"], str)
            or not SHA256.fullmatch(backup["checksum"])
            or not isinstance(backup["location"], str)
            or not backup["location"]
            or not isinstance(backup["storage_class"], str)
            or backup["storage_class"] not in {"local", "offsite"}
            or not isinstance(backup["status"], str)
            or backup["status"] not in {
                "present", "missing", "expired", "deleted", "invalid"
            }
            or type(backup["legal_hold"]) is not bool
        ):
            raise JournalError("inventory backup state invalid")
        created = _utc(backup["created_at_utc"])
        expiry = _utc(backup["expires_at_utc"])
        verified = _utc(backup["verified_at_utc"])
        if (
            expiry < created
            or verified < created
            or verified > coverage_verified
            or created > datetime.now(UTC) + timedelta(minutes=5)
        ):
            raise JournalError("inventory backup chronology invalid")
        if backup["status"] == "invalid":
            raise JournalError("inventory backup state unverified")
        if backup["status"] == "missing":
            raise JournalError("inventory backup missing")
        if backup["status"] == "expired" and datetime.now(UTC) < expiry:
            raise JournalError("inventory backup not yet expired")
        if backup["storage_class"] == "local":
            location = Path(backup["location"])
            if (
                not location.is_absolute()
                or location.is_symlink()
                or not location.is_relative_to(local_root)
                or not location.resolve().is_relative_to(local_root.resolve())
            ):
                raise JournalError("inventory local location invalid")
            if backup["status"] == "present":
                _check_mode(location, directory=False)
                listed_present.add(location)
                with location.open("rb") as source:
                    digest = hashlib.file_digest(source, "sha256").hexdigest()
                if digest != backup["checksum"]:
                    raise JournalError("inventory backup checksum invalid")
            elif backup["status"] in {"expired", "deleted"} and location.exists():
                raise JournalError("inventory supposedly removed backup still exists")
        # No offsite provider/receipt verification exists yet. Every offsite
        # entry therefore blocks pruning, including a claimed deleted copy.
    actual = set(local_root.rglob("*.dump"))
    if any(path.is_symlink() or not path.is_file() for path in actual):
        raise JournalError("inventory local backup path invalid")
    if actual != listed_present:
        raise JournalError("inventory local backup listing incomplete")
    return inventory


def write_inventory(
    path: Path, coverage: dict[str, Any], backups: list[dict[str, Any]]
) -> None:
    """Atomically publish an operator-prepared inventory, then re-verify it."""
    _check_mode(path.parent, directory=True)
    body: dict[str, Any] = {"version": 1, "coverage": coverage, "backups": backups}
    with ErasureJournal(str(path.parent)).locked():
        _atomic_json(path, {**body, "sha256": _digest(body)})
        validate_inventory(path)
