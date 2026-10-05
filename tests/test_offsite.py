"""Only synthetic bytes and an isolated local mock; never customer archives."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.backup_inventory import validate_inventory, write_inventory
from app.erasure import ErasureJournal
from ops.offsite import LocalSyntheticStore, OffsiteError


def test_local_mock_verifies_copy_and_inventory_remains_offsite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PAPELA_ENV", "test")
    store = LocalSyntheticStore(tmp_path / "papela-offsite-mock-copies")
    backup_id = "backup-20261004T000000Z-deadbeef"
    payload = b"SYNTHETIC-NOT-A-POSTGRES-ARCHIVE"
    receipt = store.put_verified(backup_id, payload)
    assert receipt.storage_class == "offsite"
    assert receipt.encrypted is False  # Mock is not a production transport.
    assert receipt.checksum == hashlib.sha256(payload).hexdigest()
    assert store.read_verified(receipt) == payload
    with pytest.raises(OffsiteError, match="MOCK_COPY_EXISTS"):
        store.put_verified(backup_id, payload)

    journal = ErasureJournal.initialize(str(tmp_path / "journal"))
    local_root = tmp_path / "local-backups"
    local_root.mkdir(mode=0o700)
    now = datetime.now(UTC)
    coverage = {
        "source_database": "synthetic",
        "schema_version": "synthetic-schema",
        "local_root": str(local_root),
        "local_complete": True,
        "offsite_complete": True,
        "verified_at_utc": now.isoformat(),
    }
    entry = {
        "backup_id": backup_id,
        "created_at_utc": (now - timedelta(minutes=1)).isoformat(),
        "source_database": "synthetic",
        "schema_version": "synthetic-schema",
        "privacy_generation": 0,
        "checksum": receipt.checksum,
        "location": receipt.location,
        "storage_class": "offsite",
        "expires_at_utc": (now + timedelta(days=7)).isoformat(),
        "status": "present",
        "verified_at_utc": receipt.verified_at_utc,
        "legal_hold": False,
        "marker_horizon_generation": 0,
    }
    inventory_path = journal.root / "backup-inventory.json"
    write_inventory(inventory_path, coverage, [entry])
    assert validate_inventory(inventory_path)["backups"][0]["storage_class"] == (
        "offsite"
    )

    (store.root / f"{backup_id}.mock-copy").write_bytes(b"TAMPERED")
    with pytest.raises(OffsiteError, match="MOCK_COPY_CHECKSUM_MISMATCH"):
        store.read_verified(receipt)


def test_mock_refuses_production_and_missing_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "papela-offsite-mock-copies"
    monkeypatch.setenv("PAPELA_ENV", "production")
    with pytest.raises(OffsiteError, match="SYNTHETIC_OFFSITE_ONLY"):
        LocalSyntheticStore(root)
    monkeypatch.setenv("PAPELA_ENV", "test")
    store = LocalSyntheticStore(root)
    receipt = store.put_verified("backup-20261004T000000Z-12345678", b"synthetic")
    (store.root / "backup-20261004T000000Z-12345678.mock-copy").unlink()
    with pytest.raises(OffsiteError, match="MOCK_COPY_MISSING"):
        store.read_verified(receipt)
