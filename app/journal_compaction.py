"""Fail-closed journal compaction, limited to terminal job erasure markers."""

from __future__ import annotations

from typing import Any, Protocol

from .backup_inventory import _utc, validate_inventory
from .erasure import ErasureJournal, JournalError, _atomic_json, _digest, _fsync_dir


class PrivacyRepository(Protocol):
    def privacy_generation(self) -> int: ...
    def database_name(self) -> str: ...


def compact_journal(
    repo: PrivacyRepository, journal: ErasureJournal, *, execute: bool = False
) -> list[dict[str, Any]]:
    """Explain every marker; execute only after a complete verified inventory.

    The index swap is atomic. Pruned event files become indexed garbage until
    physically removed; interruption at any point remains readable and safe.
    """
    with journal.locked():
        index = journal._validate_unlocked()
        if repo.privacy_generation() != index["generation"]:
            raise JournalError("reconciliation required before compaction")
        inventory = validate_inventory(journal.root / "backup-inventory.json")
        coverage = inventory["coverage"]
        if coverage["source_database"] != repo.database_name():
            raise JournalError("inventory database mismatch")
        if any(backup["legal_hold"] for backup in inventory["backups"]):
            raise JournalError("legal hold blocks journal compaction")
        events = journal._events_unlocked(index)
        seen_jobs: set[tuple[str, str]] = set()
        report: list[dict[str, Any]] = []
        eligible: set[str] = set()
        for item, event in zip(index["events"], events, strict=True):
            sequence = int(item["file"][:8])
            reason = "eligible"
            if event["type"] not in {"job_deleted", "result_expired"}:
                reason = "tenant/credential protection retained"
            else:
                identity = (event["tenant_id"], event["job_id"])
                if identity in seen_jobs:
                    raise JournalError("duplicate logical erasure marker")
                seen_jobs.add(identity)
                if _utc(coverage["verified_at_utc"]) < _utc(event["at_utc"]):
                    reason = "inventory coverage predates marker"
                else:
                    for backup in inventory["backups"]:
                        if backup["privacy_generation"] >= sequence:
                            continue
                        if backup["storage_class"] == "offsite":
                            reason = "offsite destruction not independently verified"
                            break
                        if backup["status"] == "present":
                            reason = "restorable pre-erasure backup present"
                            break
                        if backup["status"] not in {"expired", "deleted"}:
                            reason = "pre-erasure backup state unverified"
                            break
            if reason == "eligible":
                eligible.add(item["file"])
            report.append({
                "sequence": sequence,
                "event_type": event["type"],
                "eligible": reason == "eligible",
                "reason": reason,
            })
        if not execute:
            return report
        old_garbage = index.get("garbage", [])
        if not eligible and not old_garbage:
            return report
        removed = [item for item in index["events"] if item["file"] in eligible]
        retained = [item for item in index["events"] if item["file"] not in eligible]
        floor = max(
            [index.get("floor_generation", 0)]
            + [int(item["file"][:8]) for item in removed]
        )
        body = {
            "version": 2,
            "generation": index["generation"],
            "floor_generation": floor,
            "events": retained,
            "garbage": [*old_garbage, *removed],
        }
        _atomic_json(journal.root / "index.json", {**body, "sha256": _digest(body)})
        # If interrupted here, validation permits only these exact hashed files.
        for item in body["garbage"]:
            (journal.root / "events" / item["file"]).unlink(missing_ok=True)
        _fsync_dir(journal.root / "events")
        body["garbage"] = []
        _atomic_json(journal.root / "index.json", {**body, "sha256": _digest(body)})
        journal._validate_unlocked()
        return report
