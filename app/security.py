from __future__ import annotations

import threading
import time
from hmac import compare_digest

from fastapi import HTTPException, Request, status

from .config import get_settings


def tenant_for_api_key(value: str | None) -> str | None:
    if not value or len(value) > 512 or not value.isascii():
        return None
    # Evaluate all keys with no early return. Never persist/log the supplied key.
    owner: str | None = None
    matches = 0
    candidate = value.encode("ascii")
    for tenant_id, key in get_settings().tenant_key_pairs:
        equal = compare_digest(candidate, key.encode("ascii"))
        matches += int(equal)
        if equal:
            owner = tenant_id
    return owner if matches == 1 else None


def valid_api_key(value: str | None) -> bool:
    return tenant_for_api_key(value) is not None


async def require_api_key(
    request: Request,
) -> str:
    """Return the authenticated tenant ID, never the raw credential."""
    keys = request.headers.getlist("x-api-key")
    tenant_id = tenant_for_api_key(keys[0]) if len(keys) == 1 else None
    if tenant_id is None or request.state.tenant_id != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key"
        )
    return tenant_id


class RateLimiter:
    """Per-tenant fixed-window limiter, in-process, thread-safe.

    Honest limitation: in-process only, so it does NOT coordinate across
    multiple API replicas. For a single on-prem node this is correct; scale
    out => move the counter to Postgres or Redis. Documented, not hidden.
    """

    def __init__(self, per_min: int) -> None:
        self._limit = per_min
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[int, int]] = {}  # key -> (window, count)

    def allow(self, key: str) -> bool:
        window = int(time.time() // 60)
        with self._lock:
            w, count = self._buckets.get(key, (window, 0))
            if w != window:
                w, count = window, 0
            if count >= self._limit:
                return False
            self._buckets[key] = (w, count + 1)
            return True
