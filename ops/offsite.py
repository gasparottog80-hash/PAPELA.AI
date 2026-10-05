"""Provider boundary and synthetic local mock for future encrypted offsite copy.

No cloud backend, credentials, customer-data upload or production command is
implemented here. A real backend must provide encryption, independent remote
checksum/listing/deletion receipts and a separately tested restore path.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

BACKUP_ID = re.compile(r"backup-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}\Z")


class OffsiteError(RuntimeError):
    pass


@dataclass(frozen=True)
class CopyReceipt:
    backup_id: str
    storage_class: str
    location: str
    checksum: str
    verified_at_utc: str
    encrypted: bool
    backend: str

    def public_metadata(self) -> dict[str, str | bool]:
        """No credential, archive bytes, tenant identifier or document text."""
        return asdict(self)


class OffsiteTransport(Protocol):
    def put_verified(self, backup_id: str, payload: bytes) -> CopyReceipt: ...

    def read_verified(self, receipt: CopyReceipt) -> bytes: ...


class LocalSyntheticStore:
    """Unencrypted test double. It MUST NOT be used for real backup data."""

    def __init__(self, root: Path) -> None:
        if os.environ.get("PAPELA_ENV") != "test" or not root.name.startswith(
            "papela-offsite-mock-"
        ):
            raise OffsiteError("SYNTHETIC_OFFSITE_ONLY")
        if root.is_symlink():
            raise OffsiteError("MOCK_ROOT_SYMLINK")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt" and root.stat().st_mode & 0o077:
            raise OffsiteError("MOCK_ROOT_PERMISSIONS")
        self.root = root.resolve()

    def _path(self, backup_id: str) -> Path:
        if not BACKUP_ID.fullmatch(backup_id):
            raise OffsiteError("BACKUP_ID_INVALID")
        return self.root / f"{backup_id}.mock-copy"

    def put_verified(self, backup_id: str, payload: bytes) -> CopyReceipt:
        if not isinstance(payload, bytes) or not payload:
            raise OffsiteError("MOCK_COPY_EMPTY")
        path = self._path(backup_id)
        # Exclusive creation prevents a mock test from replacing an earlier
        # copy and pretending that the old checksum/receipt still applies.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError as exc:
            raise OffsiteError("MOCK_COPY_EXISTS") from exc
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        expected = hashlib.sha256(payload).hexdigest()
        receipt = CopyReceipt(
            backup_id=backup_id,
            storage_class="offsite",
            location=f"mock://{backup_id}",
            checksum=expected,
            verified_at_utc=datetime.now(UTC).isoformat(),
            encrypted=False,
            backend="local-synthetic-only",
        )
        self.read_verified(receipt)
        return receipt

    def read_verified(self, receipt: CopyReceipt) -> bytes:
        if receipt.backend != "local-synthetic-only" or receipt.encrypted:
            raise OffsiteError("MOCK_RECEIPT_INVALID")
        path = self._path(receipt.backup_id)
        if receipt.location != f"mock://{receipt.backup_id}":
            raise OffsiteError("MOCK_RECEIPT_LOCATION_INVALID")
        if path.is_symlink() or not path.is_file():
            raise OffsiteError("MOCK_COPY_MISSING")
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != receipt.checksum:
            raise OffsiteError("MOCK_COPY_CHECKSUM_MISMATCH")
        return payload
