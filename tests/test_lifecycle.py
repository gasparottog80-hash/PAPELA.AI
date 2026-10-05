"""Gate 8 synthetic-data lifecycle and restore-safety regressions."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("PAPELA_ENV", "test")
os.environ.setdefault(
    "PAPELA_DATABASE_URL", "postgresql://papela:papela@localhost:5433/papela"
)
os.environ.setdefault("PAPELA_API_KEYS", "test-key")
os.environ.setdefault(
    "PAPELA_TENANT_API_KEYS",
    '{"22222222-2222-4222-8222-222222222222":"gate1-synthetic-b-key"}',
)
os.environ.setdefault("PAPELA_OCR_ENGINE", "fake")

from app.backup_inventory import write_inventory  # noqa: E402
from app.config import DEVELOPMENT_LEGACY_TENANT_ID, get_settings  # noqa: E402
from app.erasure import ErasureJournal, JournalError  # noqa: E402
from app.journal_compaction import compact_journal  # noqa: E402
from app.lifecycle import (  # noqa: E402
    create_job_if_enabled,
    export_tenant,
    offboard_tenant,
    purge_expired_results,
    ready,
    reconcile,
    record_rotation,
    revoke_tenant,
)
from app.main import app  # noqa: E402
from app.repository import JobRepository  # noqa: E402
from app.storage import get_pdf_path  # noqa: E402
from app.worker import process_one  # noqa: E402

A = DEVELOPMENT_LEGACY_TENANT_ID
B = "22222222-2222-4222-8222-222222222222"
AUTH_A = {"X-API-Key": "test-key"}
AUTH_B = {"X-API-Key": "gate1-synthetic-b-key"}


@pytest.fixture()
def context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "erasure_dir", str(tmp_path / "erasures"))
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "pdfs"))
    Path(settings.storage_dir).mkdir(mode=0o700)
    with TestClient(app) as client:
        with psycopg.connect(settings.database_url, autocommit=True) as conn:
            conn.execute("TRUNCATE jobs")
            conn.execute(
                "UPDATE privacy_state SET generation = 0, restore_ready = TRUE"
            )
        # TestClient startup initializes a fresh empty journal in tmp_path.
        yield client, JobRepository(app.state.db.pool), app.state.journal, settings


def seed(repo: JobRepository, settings, tenant: str, *, status: str = "done") -> str:
    job_id = str(uuid.uuid4())
    path = get_pdf_path(job_id, settings.storage_dir)
    Path(path).write_bytes(b"%PDF- synthetic-only")
    repo.create(
        job_id=job_id,
        tenant_id=tenant,
        filename="synthetic.pdf",
        storage_path=path,
        size_bytes=20,
        pages=1,
    )
    if status == "done":
        repo.mark_done(job_id, {"fields": {"cpf": "SYNTHETIC-A"}})
    return job_id


def test_owner_delete_idempotent_and_cross_tenant_hidden(context):
    client, repo, journal, settings = context
    own = seed(repo, settings, A)
    other = seed(repo, settings, B)
    assert client.delete(f"/v1/jobs/{own}", headers=AUTH_B).status_code == 404
    assert client.delete(f"/v1/jobs/{own}", headers=AUTH_A).status_code == 204
    assert client.delete(f"/v1/jobs/{own}", headers=AUTH_A).status_code == 204
    assert client.get(f"/v1/jobs/{own}", headers=AUTH_A).status_code == 404
    assert client.get(f"/v1/jobs/{other}", headers=AUTH_B).status_code == 200
    assert not Path(get_pdf_path(own, settings.storage_dir)).exists()
    assert journal.state()[0] == repo.privacy_generation() == 1


def test_retention_is_tenant_safe_and_idempotent(context):
    client, repo, journal, settings = context
    old = seed(repo, settings, A)
    fresh = seed(repo, settings, B)
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(
            "UPDATE jobs SET created_at = now() - interval '40 days' WHERE id = %s",
            (old,),
        )
    settings = settings.model_copy(update={"job_retention_days": 30})
    assert purge_expired_results(repo, journal, settings) == 1
    assert purge_expired_results(repo, journal, settings) == 0
    assert client.get(f"/v1/jobs/{old}", headers=AUTH_A).status_code == 404
    assert client.get(f"/v1/jobs/{fresh}", headers=AUTH_B).status_code == 200


def test_export_only_target_tenant_no_internal_secrets(context, tmp_path: Path):
    _, repo, journal, settings = context
    seed(repo, settings, A)
    seed(repo, settings, B)
    export_dir = tmp_path / "exports"
    export_dir.mkdir(mode=0o700)
    output = export_dir / "tenant-a.json"
    assert export_tenant(repo, journal, A, output) == 1
    value = json.loads(output.read_text(encoding="utf-8"))
    assert value["format"] == "papela-tenant-export-v1"
    assert value["tenant_id"] == A
    assert len(value["jobs"]) == 1
    payload = output.read_text(encoding="utf-8")
    assert B not in payload and "storage_path" not in payload
    assert "test-key" not in payload and "gate1-synthetic-b-key" not in payload
    if os.name != "nt":
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        export_tenant(repo, journal, A, output)


def test_offboard_blocks_in_flight_worker_and_preserves_other_tenant(
    context, monkeypatch: pytest.MonkeyPatch
):
    client, repo, journal, settings = context
    in_flight = seed(repo, settings, A, status="pending")
    other = seed(repo, settings, B)
    parsing = threading.Event()
    resume = threading.Event()

    def paused(*_args):
        parsing.set()
        assert resume.wait(5)
        return {"fields": {"cpf": "SYNTHETIC-IN-FLIGHT"}}

    monkeypatch.setattr("app.worker.bounded_process", paused)
    thread = threading.Thread(target=process_one, args=(repo, settings))
    thread.start()
    assert parsing.wait(5)
    try:
        assert offboard_tenant(repo, journal, settings, A) == 1
    finally:
        resume.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert repo.get_for_tenant(in_flight, A) is None
    assert client.get(f"/v1/jobs/{in_flight}", headers=AUTH_A).status_code == 401
    assert client.get(f"/v1/jobs/{other}", headers=AUTH_B).status_code == 200
    assert offboard_tenant(repo, journal, settings, A) == 0


def test_offboard_blocks_late_upload_insert(context):
    _, repo, journal, settings = context
    assert offboard_tenant(repo, journal, settings, A) == 0
    job = str(uuid.uuid4())
    assert not create_job_if_enabled(
        repo,
        journal,
        job_id=job,
        tenant_id=A,
        filename="synthetic.pdf",
        storage_path=get_pdf_path(job, settings.storage_dir),
        size_bytes=20,
        pages=1,
    )
    assert repo.get_for_tenant(job, A) is None


def test_restore_reconciliation_job_tenant_multiple_and_duplicate(context):
    client, repo, journal, settings = context
    job_a = seed(repo, settings, A)
    job_b = seed(repo, settings, B)
    # Synthetic T0 snapshot: only the row values needed to simulate an older
    # pg_dump. The CI smoke performs a real pg_dump/pg_restore separately.
    snapshot = repo.get_for_tenant(job_a, A)
    assert snapshot is not None
    assert client.delete(f"/v1/jobs/{job_a}", headers=AUTH_A).status_code == 204
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute("UPDATE privacy_state SET generation = 0, restore_ready = FALSE")
        conn.execute(
            "INSERT INTO jobs (id,tenant_id,status,filename,storage_path,"
            "size_bytes,pages,result) "
            "VALUES (%s,%s,'done','synthetic.pdf',%s,20,1,%s)",
            (
                job_a,
                A,
                get_pdf_path(job_a, settings.storage_dir),
                psycopg.types.json.Jsonb(snapshot["result"]),
            ),
        )
    assert client.get("/readiness").status_code == 503
    assert client.get(f"/v1/jobs/{job_a}", headers=AUTH_A).status_code == 503
    assert reconcile(repo, journal, settings) == 1
    assert reconcile(repo, journal, settings) == 0
    assert client.get("/readiness").status_code == 200
    assert client.get(f"/v1/jobs/{job_a}", headers=AUTH_A).status_code == 404
    assert client.get(f"/v1/jobs/{job_b}", headers=AUTH_B).status_code == 200


def test_restore_reconciliation_of_offboarded_tenant(context):
    client, repo, journal, settings = context
    job_a = seed(repo, settings, A)
    job_b = seed(repo, settings, B)
    snapshot = repo.get_for_tenant(job_a, A)
    assert snapshot is not None
    assert offboard_tenant(repo, journal, settings, A) == 1
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute("UPDATE privacy_state SET generation = 0, restore_ready = FALSE")
        conn.execute(
            "INSERT INTO jobs (id,tenant_id,status,filename,storage_path,"
            "size_bytes,pages,result) "
            "VALUES (%s,%s,'done','synthetic.pdf',%s,20,1,%s)",
            (job_a, A, get_pdf_path(job_a, settings.storage_dir),
             psycopg.types.json.Jsonb(snapshot["result"])),
        )
    assert client.get("/readiness").status_code == 503
    assert reconcile(repo, journal, settings) == 1
    assert client.get("/readiness").status_code == 200
    assert repo.get_for_tenant(job_a, A) is None
    assert client.get(f"/v1/jobs/{job_a}", headers=AUTH_A).status_code == 401
    assert client.get(f"/v1/jobs/{job_b}", headers=AUTH_B).status_code == 200


def test_unknown_restored_database_identity_fails_closed(context):
    client, repo, journal, settings = context
    seed(repo, settings, A)
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(
            "UPDATE privacy_state SET database_name = 'synthetic_old_database'"
        )
    assert client.get("/readiness").status_code == 503
    blocked = client.get(
        "/v1/jobs/00000000-0000-4000-8000-000000000001", headers=AUTH_A
    )
    assert blocked.status_code == 503
    assert reconcile(repo, journal, settings) == 0
    assert client.get("/readiness").status_code == 200


def test_reconciliation_rejects_an_older_but_valid_journal(context, tmp_path):
    client, repo, _journal, settings = context
    job = seed(repo, settings, A)
    assert client.delete(f"/v1/jobs/{job}", headers=AUTH_A).status_code == 204
    older = ErasureJournal.initialize(str(tmp_path / "older-erasures"))
    assert not ready(repo, older)
    with pytest.raises(JournalError, match="journal older"):
        reconcile(repo, older, settings)
    assert repo.privacy_generation_raw() == 1


def test_journal_write_failure_preserves_active_job_and_pdf(
    context, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    client, repo, _journal, settings = context
    job = seed(repo, settings, A)

    def fail(*_args):
        raise OSError("SYNTHETIC-CANARY secret 12345678901")

    monkeypatch.setattr("app.erasure._atomic_json", fail)
    response = client.delete(f"/v1/jobs/{job}", headers=AUTH_A)
    assert response.status_code == 500
    assert "SYNTHETIC-CANARY" not in response.text
    assert "SYNTHETIC-CANARY" not in caplog.text
    assert repo.get_for_tenant(job, A) is not None
    assert Path(get_pdf_path(job, settings.storage_dir)).exists()


@pytest.mark.parametrize("damage", ["truncate", "remove"])
def test_journal_corruption_or_missing_marker_fails_closed(context, damage: str):
    client, repo, journal, settings = context
    job = seed(repo, settings, A)
    assert client.delete(f"/v1/jobs/{job}", headers=AUTH_A).status_code == 204
    marker = next((Path(settings.erasure_dir) / "events").iterdir())
    if damage == "truncate":
        marker.write_bytes(b"{")
    else:
        marker.unlink()
    with pytest.raises(JournalError):
        journal.validate()
    assert not ready(repo, journal)
    assert client.get("/readiness").status_code == 503
    assert client.get(f"/v1/jobs/{job}", headers=AUTH_A).status_code == 503


def test_credential_rotation_and_revocation_do_not_log_key(context):
    client, repo, journal, settings = context
    other = seed(repo, settings, B)
    record_rotation(repo, journal, B)
    assert client.get(f"/v1/jobs/{other}", headers=AUTH_B).status_code == 200
    revoke_tenant(repo, journal, B)
    assert client.get(f"/v1/jobs/{other}", headers=AUTH_B).status_code == 401
    assert ready(repo, journal)


def inventory_record(repo, journal, tmp_path, *, status="present", offsite=False,
                     hold=False, bad_checksum=False):
    archive = tmp_path / "synthetic-backup.dump"
    if status == "present" and not offsite:
        archive.write_bytes(b"SYNTHETIC-BACKUP-ONLY")
        archive.chmod(0o600)
    else:
        archive.unlink(missing_ok=True)
    checksum = hashlib.sha256(b"SYNTHETIC-BACKUP-ONLY").hexdigest()
    if bad_checksum:
        checksum = "0" * 64
    now = datetime.now(UTC)
    entry = {
        "backup_id": "synthetic-backup-1",
        "created_at_utc": (now - timedelta(days=2)).isoformat(),
        "source_database": repo.database_name(),
        "schema_version": "jobs-tenant-v3",
        "privacy_generation": 0,
        "checksum": checksum,
        "location": "offsite://synthetic/backup" if offsite else str(archive),
        "storage_class": "offsite" if offsite else "local",
        "expires_at_utc": (now - timedelta(days=1)).isoformat(),
        "status": status,
        "verified_at_utc": now.isoformat(),
        "legal_hold": hold,
        "marker_horizon_generation": 0,
    }
    coverage = {
        "source_database": repo.database_name(),
        "schema_version": "jobs-tenant-v3",
        "local_root": str(tmp_path),
        "local_complete": True,
        "offsite_complete": True,
        "verified_at_utc": now.isoformat(),
    }
    path = journal.root / "backup-inventory.json"
    write_inventory(path, coverage, [entry])
    return path, coverage, entry, archive


def test_compaction_requires_backup_destruction_and_restore_floor(context, tmp_path):
    client, repo, journal, settings = context
    job = seed(repo, settings, A)
    assert client.delete(f"/v1/jobs/{job}", headers=AUTH_A).status_code == 204
    path, coverage, entry, archive = inventory_record(repo, journal, tmp_path)
    report = compact_journal(repo, journal)
    assert report[0]["eligible"] is False
    assert "present" in report[0]["reason"]
    assert compact_journal(repo, journal, execute=True) == report
    assert len(journal.validate()["events"]) == 1
    archive.unlink()
    entry["status"] = "deleted"
    entry["verified_at_utc"] = datetime.now(UTC).isoformat()
    coverage["verified_at_utc"] = datetime.now(UTC).isoformat()
    write_inventory(path, coverage, [entry])
    assert compact_journal(repo, journal)[0]["eligible"]
    compact_journal(repo, journal, execute=True)
    index = journal.validate()
    assert index["floor_generation"] == 1
    assert index["events"] == []
    assert list((journal.root / "events").iterdir()) == []
    assert ready(repo, journal)
    assert compact_journal(repo, journal, execute=True) == []
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute("UPDATE privacy_state SET generation = 0, restore_ready = FALSE")
    assert client.get("/readiness").status_code == 503
    with pytest.raises(JournalError, match="predates compacted"):
        reconcile(repo, journal, settings)
    # A surviving post-erasure snapshot at the floor is reconcilable.
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute("UPDATE privacy_state SET generation = 1, restore_ready = FALSE")
    assert reconcile(repo, journal, settings) == 0
    assert ready(repo, journal)
    assert repo.get_for_tenant(job, A) is None
    path.unlink()
    assert client.get("/readiness").status_code == 503


def test_compaction_accepts_only_physically_expired_local_archive(context, tmp_path):
    client, repo, journal, settings = context
    job = seed(repo, settings, A)
    assert client.delete(f"/v1/jobs/{job}", headers=AUTH_A).status_code == 204
    inventory_record(repo, journal, tmp_path, status="expired")
    assert compact_journal(repo, journal)[0]["eligible"]
    compact_journal(repo, journal, execute=True)
    assert journal.validate()["floor_generation"] == 1


def test_unlisted_local_archive_blocks_compaction_and_readiness(context, tmp_path):
    client, repo, journal, settings = context
    job = seed(repo, settings, A)
    assert client.delete(f"/v1/jobs/{job}", headers=AUTH_A).status_code == 204
    inventory_record(repo, journal, tmp_path, status="deleted")
    extra = tmp_path / "unknown-backup.dump"
    extra.write_bytes(b"SYNTHETIC-UNREGISTERED")
    extra.chmod(0o600)
    with pytest.raises(JournalError, match="listing incomplete"):
        compact_journal(repo, journal)
    assert client.get("/readiness").status_code == 503


@pytest.mark.parametrize(
    "scenario",
    [
        "missing", "missing_copy", "corrupt", "checksum", "offsite", "hold",
        "invalid", "malformed",
    ],
)
def test_compaction_fail_closed_inventory_scenarios(context, tmp_path, scenario):
    client, repo, journal, settings = context
    job = seed(repo, settings, A)
    assert client.delete(f"/v1/jobs/{job}", headers=AUTH_A).status_code == 204
    if scenario == "missing":
        with pytest.raises(JournalError):
            compact_journal(repo, journal)
        return
    path, _, _, _ = inventory_record(
        repo, journal, tmp_path,
        offsite=scenario == "offsite", hold=scenario == "hold"
    )
    if scenario == "corrupt":
        path.write_bytes(b"{")
        with pytest.raises(JournalError):
            compact_journal(repo, journal)
        assert client.get("/readiness").status_code == 503
    elif scenario == "checksum":
        archive = tmp_path / "synthetic-backup.dump"
        archive.write_bytes(b"TAMPERED")
        with pytest.raises(JournalError, match="checksum"):
            compact_journal(repo, journal)
        assert client.get("/readiness").status_code == 503
    elif scenario == "offsite":
        report = compact_journal(repo, journal)
        assert not report[0]["eligible"] and "offsite" in report[0]["reason"]
    elif scenario in {"invalid", "malformed", "missing_copy"}:
        inventory = json.loads(path.read_text(encoding="utf-8"))
        if scenario == "invalid":
            inventory["backups"][0]["status"] = "invalid"
        elif scenario == "missing_copy":
            inventory["backups"][0]["status"] = "missing"
        else:
            inventory["backups"][0]["status"] = []
        from app.erasure import _digest

        body = {key: inventory[key] for key in ("version", "coverage", "backups")}
        inventory["sha256"] = _digest(body)
        path.write_text(json.dumps(inventory), encoding="utf-8")
        with pytest.raises(JournalError):
            compact_journal(repo, journal)
        assert client.get("/readiness").status_code == 503
    else:
        with pytest.raises(JournalError, match="legal hold"):
            compact_journal(repo, journal)


def test_compaction_duplicate_and_incomplete_inventory(context, tmp_path):
    client, repo, journal, settings = context
    job = seed(repo, settings, A)
    assert client.delete(f"/v1/jobs/{job}", headers=AUTH_A).status_code == 204
    path, coverage, entry, archive = inventory_record(repo, journal, tmp_path)
    coverage["offsite_complete"] = False
    with pytest.raises(JournalError, match="incomplete"):
        write_inventory(path, coverage, [entry])
    assert not ready(repo, journal)
    coverage["offsite_complete"] = True
    with pytest.raises(JournalError, match="duplicate"):
        write_inventory(path, coverage, [entry, entry.copy()])
    archive.unlink()
    entry["status"] = "deleted"
    write_inventory(path, coverage, [entry])
    # A second logical deletion with a different event type must not prune.
    with journal.locked():
        index = journal._validate_unlocked()
        journal.record_locked(index, "result_expired", A, job)
    repo.advance_privacy_generation(2)
    with pytest.raises(JournalError, match="duplicate logical"):
        compact_journal(repo, journal)
    assert not ready(repo, journal)
    assert client.get("/readiness").status_code == 503


def test_compaction_interruption_keeps_valid_index_and_retries(
    context, tmp_path, monkeypatch
):
    client, repo, journal, settings = context
    job = seed(repo, settings, A)
    assert client.delete(f"/v1/jobs/{job}", headers=AUTH_A).status_code == 204
    path, coverage, entry, archive = inventory_record(repo, journal, tmp_path)
    archive.unlink()
    entry["status"] = "deleted"
    write_inventory(path, coverage, [entry])
    original_unlink = Path.unlink
    interrupted = False

    def fail_once(self, *args, **kwargs):
        nonlocal interrupted
        if self.parent == journal.root / "events" and not interrupted:
            interrupted = True
            raise OSError("synthetic interruption")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_once)
    with pytest.raises(OSError, match="synthetic interruption"):
        compact_journal(repo, journal, execute=True)
    index = journal.validate()
    assert index["floor_generation"] == 1 and len(index["garbage"]) == 1
    assert ready(repo, journal)
    compact_journal(repo, journal, execute=True)
    assert journal.validate()["garbage"] == []
    assert list((journal.root / "events").iterdir()) == []
