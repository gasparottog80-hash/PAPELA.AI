"""Durable, independently stored erasure journal.

SHA-256 detects accidental corruption/truncation; it is not an authentication
MAC. Protect this directory with host permissions and an independent backup.
Each structured event is immutable in normal operation. The index is atomically
replaced only to append a reference to a newly durable event.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EVENT_TYPES = frozenset(
    {
        "job_deleted",
        "result_expired",
        "tenant_disabled",
        "tenant_deleted",
        "api_key_rotated",
        "api_key_revoked",
    }
)
EVENT_FILE = re.compile(r"[0-9]{8}-[0-9a-f-]{36}\.json\Z")


class JournalError(RuntimeError):
    pass


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _uuid(value: str) -> str:
    try:
        parsed = str(uuid.UUID(value))
    except (ValueError, TypeError) as exc:
        raise JournalError("invalid identifier") from exc
    if parsed != value:
        raise JournalError("non-canonical identifier")
    return parsed


def _check_mode(path: Path, *, directory: bool) -> None:
    if path.is_symlink() or not path.exists():
        raise JournalError("journal path unavailable")
    if directory != path.is_dir():
        raise JournalError("journal path type invalid")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise JournalError("journal permissions too broad")


def _fsync_dir(path: Path) -> None:
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(_canonical(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        _fsync_dir(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    _check_mode(path, directory=False)
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise JournalError("journal JSON corrupt") from exc
    if not isinstance(value, dict):
        raise JournalError("journal schema invalid")
    return value


class ErasureJournal:
    def __init__(self, directory: str) -> None:
        self.root = Path(directory)

    @classmethod
    def initialize(cls, directory: str) -> ErasureJournal:
        root = Path(directory)
        if root.is_symlink():
            raise JournalError("journal symlink forbidden")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _check_mode(root, directory=True)
        events = root / "events"
        events.mkdir(mode=0o700, exist_ok=True)
        _check_mode(events, directory=True)
        lock = root / ".lock"
        if not lock.exists():
            if (root / "index.json").exists():
                raise JournalError("journal lock missing")
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        _check_mode(lock, directory=False)
        index = root / "index.json"
        if not index.exists():
            body: dict[str, Any] = {"version": 1, "generation": 0, "events": []}
            _atomic_json(index, {**body, "sha256": _digest(body)})
        journal = cls(directory)
        journal.validate()
        return journal

    @contextmanager
    def locked(self) -> Iterator[None]:
        _check_mode(self.root, directory=True)
        lock_path = self.root / ".lock"
        _check_mode(lock_path, directory=False)
        with lock_path.open("r+b") as lock:
            if os.name == "nt":
                import msvcrt

                lock.write(b"0")
                lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
                try:
                    yield
                finally:
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def _validate_unlocked(self) -> dict[str, Any]:
        _check_mode(self.root / "events", directory=True)
        index = _read_json(self.root / "index.json")
        fields: tuple[str, ...]
        floor: Any
        garbage: Any
        if type(index.get("version")) is not int:
            raise JournalError("journal index version invalid")
        if index["version"] == 1:
            fields = ("version", "generation", "events")
            floor = 0
            garbage = []
        elif index.get("version") == 2:
            fields = ("version", "generation", "floor_generation", "events", "garbage")
            floor = index.get("floor_generation")
            garbage = index.get("garbage")
        else:
            raise JournalError("journal index version invalid")
        if set(index) != {*fields, "sha256"}:
            raise JournalError("journal index schema invalid")
        body = {key: index[key] for key in fields}
        if (
            type(index["generation"]) is not int
            or index["generation"] < 0
            or type(floor) is not int
            or floor < 0
            or floor > index["generation"]
            or not isinstance(index["events"], list)
            or (index["version"] == 1 and len(index["events"]) != index["generation"])
            or not isinstance(garbage, list)
            or index["sha256"] != _digest(body)
        ):
            raise JournalError("journal index integrity invalid")
        seen: set[str] = set()
        sequences: set[int] = set()
        logical_jobs: set[tuple[str, str]] = set()
        for item in index["events"]:
            if not isinstance(item, dict) or set(item) != {"file", "sha256"}:
                raise JournalError("journal entry schema invalid")
            name = item["file"]
            if not isinstance(name, str) or not EVENT_FILE.fullmatch(name):
                raise JournalError("journal event name invalid")
            sequence = int(name[:8])
            if (
                name in seen
                or sequence in sequences
                or not 1 <= sequence <= index["generation"]
            ):
                raise JournalError("journal sequence invalid")
            seen.add(name)
            sequences.add(sequence)
            event = _read_json(self.root / "events" / name)
            if set(event) != {"version", "type", "tenant_id", "job_id", "at_utc"}:
                raise JournalError("journal event schema invalid")
            if (
                type(event["version"]) is not int
                or event["version"] != 1
                or not isinstance(event["type"], str)
                or event["type"] not in EVENT_TYPES
                or not isinstance(event["tenant_id"], str)
                or _uuid(event["tenant_id"]) != event["tenant_id"]
            ):
                raise JournalError("journal event invalid")
            job_id = event["job_id"]
            if event["type"] in {"job_deleted", "result_expired"}:
                if not isinstance(job_id, str) or _uuid(job_id) != job_id:
                    raise JournalError("journal job invalid")
                logical_job = (event["tenant_id"], job_id)
                if logical_job in logical_jobs:
                    raise JournalError("duplicate logical erasure marker")
                logical_jobs.add(logical_job)
            elif job_id is not None:
                raise JournalError("journal job unexpected")
            try:
                stamp = datetime.fromisoformat(event["at_utc"])
            except (TypeError, ValueError) as exc:
                raise JournalError("journal timestamp invalid") from exc
            if stamp.tzinfo != UTC or item["sha256"] != _digest(event):
                raise JournalError("journal event checksum invalid")
        garbage_names: set[str] = set()
        for item in garbage:
            if not isinstance(item, dict) or set(item) != {"file", "sha256"}:
                raise JournalError("journal garbage schema invalid")
            name = item["file"]
            if (
                not isinstance(name, str)
                or not EVENT_FILE.fullmatch(name)
                or name in seen
                or name in garbage_names
                or int(name[:8]) in sequences
                or int(name[:8]) > floor
            ):
                raise JournalError("journal garbage invalid")
            garbage_names.add(name)
            path = self.root / "events" / name
            if path.exists() and _digest(_read_json(path)) != item["sha256"]:
                raise JournalError("journal garbage checksum invalid")
        actual = {p.name for p in (self.root / "events").iterdir()}
        if not seen.issubset(actual) or not actual.issubset(seen | garbage_names):
            raise JournalError("journal event set mismatch")
        if index["version"] == 2:
            if any(sequence not in sequences and sequence > floor
                   for sequence in range(1, index["generation"] + 1)):
                raise JournalError("journal floor invalid")
            if floor:
                from .backup_inventory import validate_inventory

                inventory = validate_inventory(self.root / "backup-inventory.json")
                for backup in inventory["backups"]:
                    if backup["privacy_generation"] < floor and (
                        backup["status"] == "present"
                        or backup["storage_class"] == "offsite"
                        or backup["legal_hold"]
                    ):
                        raise JournalError("pre-compaction backup reappeared")
        elif sequences != set(range(1, index["generation"] + 1)):
            raise JournalError("journal sequence coverage invalid")
        inventory_path = self.root / "backup-inventory.json"
        if inventory_path.exists() and not floor:
            from .backup_inventory import validate_inventory

            validate_inventory(inventory_path)
        return index

    def validate(self) -> dict[str, Any]:
        with self.locked():
            return self._validate_unlocked()

    def _events_unlocked(self, index: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            _read_json(self.root / "events" / item["file"]) for item in index["events"]
        ]

    def state(self) -> tuple[int, set[str], set[tuple[str, str]]]:
        """Validate every referenced file before returning denylist state."""
        with self.locked():
            index = self._validate_unlocked()
            tenants: set[str] = set()
            jobs: set[tuple[str, str]] = set()
            for event in self._events_unlocked(index):
                if event["type"] in {
                    "tenant_disabled",
                    "tenant_deleted",
                    "api_key_revoked",
                }:
                    tenants.add(event["tenant_id"])
                elif event["type"] in {"job_deleted", "result_expired"}:
                    jobs.add((event["tenant_id"], event["job_id"]))
            return index["generation"], tenants, jobs

    def record_locked(
        self,
        index: dict[str, Any],
        event_type: str,
        tenant_id: str,
        job_id: str | None = None,
    ) -> int:
        """Caller holds lock; fsync marker before any active-data deletion."""
        if event_type not in EVENT_TYPES:
            raise JournalError("journal event type invalid")
        tenant_id = _uuid(tenant_id)
        if event_type in {"job_deleted", "result_expired"}:
            if job_id is None:
                raise JournalError("job required")
            job_id = _uuid(job_id)
        elif job_id is not None:
            raise JournalError("job forbidden")
        for prior in self._events_unlocked(index):
            if (prior["type"], prior["tenant_id"], prior["job_id"]) == (
                event_type,
                tenant_id,
                job_id,
            ) and event_type not in {"api_key_rotated"}:
                return int(index["generation"])
        event: dict[str, Any] = {
            "version": 1,
            "type": event_type,
            "tenant_id": tenant_id,
            "job_id": job_id,
            "at_utc": datetime.now(UTC).isoformat(),
        }
        sequence = index["generation"] + 1
        name = f"{sequence:08d}-{uuid.uuid4()}.json"
        _atomic_json(self.root / "events" / name, event)
        if index["version"] == 1:
            body = {
                "version": 1,
                "generation": sequence,
                "events": [*index["events"], {"file": name, "sha256": _digest(event)}],
            }
        else:
            body = {
                "version": 2,
                "generation": sequence,
                "floor_generation": index["floor_generation"],
                "events": [*index["events"], {"file": name, "sha256": _digest(event)}],
                "garbage": index["garbage"],
            }
        _atomic_json(self.root / "index.json", {**body, "sha256": _digest(body)})
        return int(sequence)
