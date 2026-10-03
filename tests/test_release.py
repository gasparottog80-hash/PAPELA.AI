"""No Docker or external service is required for these release safety tests."""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from pypdf import PdfReader

from ops import release
from scripts.smoke_release import sample_pdf


def test_canary_pdf_is_parseable_and_has_expected_text() -> None:
    reader = PdfReader(io.BytesIO(sample_pdf()))
    assert len(reader.pages) == 1
    assert "Invoice 123" in reader.pages[0].extract_text()


def test_schema_fingerprint_is_stable_and_extractor_is_locked() -> None:
    assert len(release.schema_fingerprint()) == 64
    assert release.extractor_commit() == "ddb485ff76627f2e995b11d2b4d11325fc5628c9"
    head = release.run("git", "rev-parse", "HEAD")
    assert release.schema_fingerprint(head) == release.schema_fingerprint()
    assert release.extractor_commit(head) == release.extractor_commit()


def test_lock_fails_fast_for_parallel_release(tmp_path: Path) -> None:
    with release.exclusive_lock(tmp_path):
        with pytest.raises(release.ReleaseError, match="DEPLOY_LOCK_BUSY"):
            with release.exclusive_lock(tmp_path):
                pass
    with release.exclusive_lock(tmp_path):
        pass


def test_rollback_blocks_incompatible_schema_before_compose(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    current = "a" * 40
    previous = "b" * 40
    current_manifest = {
        "commit_sha": current,
        "previous_release": previous,
        "schema_fingerprint": "new",
        "old_app_compatible": False,
    }
    previous_manifest = {"commit_sha": previous, "schema_fingerprint": "old"}
    release.write_json(release.manifest_path(tmp_path, current), current_manifest)
    release.write_json(release.manifest_path(tmp_path, previous), previous_manifest)
    release.write_json(tmp_path / "active.json", {"commit_sha": current})
    env_path = tmp_path / ".env.synthetic"
    env_path.write_text("PAPELA_PROJECT_NAME=papela-gate7-ci\n", encoding="utf-8")
    compose = Mock()
    monkeypatch.setattr(release, "compose", compose)
    args = argparse.Namespace(
        state_dir=tmp_path,
        env_file=env_path,
        smoke_url="https://localhost:18445",
        execute=True,
        synthetic=True,
    )
    with pytest.raises(
        release.ReleaseError, match="SCHEMA_INCOMPATIBLE_ROLLBACK_BLOCKED"
    ):
        release.rollback(args)
    compose.assert_not_called()


def test_release_manifest_has_no_secret_material(tmp_path: Path) -> None:
    manifest = {
        "commit_sha": "c" * 40,
        "image_id": "sha256:" + "d" * 64,
        "backup_id": "backup-20261003T000000Z-deadbeef",
        "status": "candidate",
    }
    path = release.manifest_path(tmp_path, manifest["commit_sha"])
    release.write_json(path, manifest)
    assert json.loads(path.read_text(encoding="utf-8")) == manifest
    assert "password" not in path.read_text(encoding="utf-8").lower()


def test_preflight_returns_full_previous_release_not_active_pointer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    previous_commit = "a" * 40
    previous = {
        "commit_sha": previous_commit,
        "image_tag": f"papelaai:sha-{previous_commit}",
        "schema_fingerprint": "same-schema",
    }
    release.write_json(release.manifest_path(tmp_path, previous_commit), previous)
    release.write_json(tmp_path / "active.json", {"commit_sha": previous_commit})
    secret = tmp_path / "synthetic.key"
    secret.write_text("synthetic-only", encoding="utf-8")
    secret.chmod(0o600)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    env = tmp_path / ".env.synthetic"
    env.write_text(
        "".join(
            f"{name}={secret}\n"
            for name in (
                "PAPELA_PG_ADMIN_PASSWORD_FILE",
                "PAPELA_PG_RUNTIME_PASSWORD_FILE",
                "PAPELA_DATABASE_URL_FILE",
                "PAPELA_TENANT_API_KEYS_FILE",
            )
        )
        + f"PAPELA_BACKUP_DIR={backup_dir}\n"
        + f"PAPELA_APP_IMAGE={previous['image_tag']}\n"
        + "PAPELA_PROJECT_NAME=papela-gate7-ci\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(release, "validate_manifest", lambda *a, **kw: None)
    monkeypatch.setattr(release, "service_images", lambda *a: None)
    monkeypatch.setattr(release, "readiness", lambda *a: None)
    monkeypatch.setattr(release, "compose", lambda *a: "")
    monkeypatch.setattr(release, "backup_verified", lambda *a: None)
    monkeypatch.setattr(release.shutil, "disk_usage", lambda *a: Mock(free=10**10))
    args = argparse.Namespace(
        state_dir=tmp_path,
        env_file=env,
        smoke_key_file=secret,
        smoke_url="https://localhost:18445",
        initial=False,
        migration_class="none",
        migration_review=None,
        synthetic=True,
    )
    target = {
        "commit_sha": "b" * 40,
        "image_tag": "unused",
        "schema_fingerprint": "same-schema",
    }
    assert release.preflight(args, target) == previous
