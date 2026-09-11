from __future__ import annotations

import threading
import time

from fastapi import Header, HTTPException, Request, status

from .config import get_settings


async def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> str:
    """Fail-closed API-key auth. No configured keys => every request is 401
    (except /health, which is unauthenticated by design for probes)."""
    settings = get_settings()
    keys = settings.api_key_set
    if not x_api_key or x_api_key not in keys:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key"
        )
    request.state.api_key = x_api_key
    return x_api_key


class RateLimiter:
    """Per-key fixed-window limiter, in-process, thread-safe.

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
