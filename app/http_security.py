"""HTTP admission checks run BEFORE FastAPI parses multipart bodies."""

from __future__ import annotations

import asyncio
import logging
import time
from uuid import UUID, uuid4

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import get_settings
from .error_codes import (
    AUTH_INVALID,
    DB_UNAVAILABLE,
    INTERNAL_ERROR,
    INVALID_REQUEST,
    PROCESSING_TIMEOUT,
    RATE_LIMITED,
    UPLOAD_TOO_LARGE,
    code_for_exception,
)
from .lifecycle import ready as privacy_ready
from .metrics import METRICS
from .security import tenant_for_api_key

logger = logging.getLogger("papela.api")


def _route(path: str) -> str:
    if path == "/health":
        return "health"
    if path == "/readiness":
        return "readiness"
    if path == "/v1/upload":
        return "upload"
    if path.startswith("/v1/jobs/"):
        return "job_audit" if path.endswith("/audit") else "job"
    return "other"


def _request_id(request: Request) -> str:
    values = request.headers.getlist("x-request-id")
    if len(values) == 1 and len(values[0]) == 36:
        try:
            parsed = UUID(values[0])
            if parsed.version == 4 and str(parsed) == values[0]:
                return str(parsed)
        except ValueError:
            pass
    return str(uuid4())


class SecurityBoundary:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.uploads = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        request_id = _request_id(request)
        scope.setdefault("state", {})["request_id"] = request_id
        settings = get_settings()
        started = False
        started_at = time.monotonic()
        status_code = 500
        route = _route(scope["path"])

        async def safe_send(message: Message) -> None:
            nonlocal started, status_code
            if message["type"] == "http.response.start":
                started = True
                status_code = int(message["status"])
                message.setdefault("headers", []).extend(
                    [
                        (b"x-request-id", request_id.encode()),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"cache-control", b"no-store"),
                    ]
                )
            await send(message)

        async def reject(code: int, error: str, error_code: str) -> None:
            response = JSONResponse(
                {"request_id": request_id, "code": error_code, "error": error},
                status_code=code,
            )
            if code == 429:
                response.headers["Retry-After"] = "60"
            if upload:
                reason = {
                    AUTH_INVALID: "auth",
                    RATE_LIMITED: "rate_limit",
                    UPLOAD_TOO_LARGE: "too_large",
                    PROCESSING_TIMEOUT: "timeout",
                }.get(error_code)
                if reason is not None:
                    METRICS.inc("uploads_rejected_total", reason)
            await response(scope, receive, safe_send)

        upload = scope["path"] == "/v1/upload" and scope["method"] == "POST"
        admitted = False
        try:
            if scope["path"].startswith("/v1/"):
                keys = request.headers.getlist("x-api-key")
                tenant_id = tenant_for_api_key(keys[0]) if len(keys) == 1 else None
                if tenant_id is None:
                    await reject(401, "invalid api key", AUTH_INVALID)
                    return
                if not privacy_ready(
                    scope["app"].state.repo, scope["app"].state.journal
                ):
                    await reject(503, "service unavailable", DB_UNAVAILABLE)
                    return
                _, disabled, _ = scope["app"].state.journal.state()
                if tenant_id in disabled:
                    await reject(401, "invalid api key", AUTH_INVALID)
                    return
                scope["state"]["tenant_id"] = tenant_id
                if not scope["app"].state.rate_limiter.allow(tenant_id):
                    await reject(429, "rate limit exceeded", RATE_LIMITED)
                    return
            # Also bound bodies on methods/routes that have no upload handler.
            maximum = settings.max_upload_bytes + 64 * 1024 if upload else 0
            sizes = request.headers.getlist("content-length")
            if sizes and (
                len(sizes) != 1
                or len(sizes[0]) > 12
                or not sizes[0].isascii()
                or not sizes[0].isdecimal()
            ):
                await reject(400, "invalid content length", INVALID_REQUEST)
                return
            if sizes and int(sizes[0]) > maximum:
                await reject(413, "request too large", UPLOAD_TOO_LARGE)
                return
            if upload:
                if self.uploads >= settings.max_concurrent_uploads:
                    await reject(429, "upload capacity exceeded", RATE_LIMITED)
                    return
                self.uploads += 1
                admitted = True
            body = bytearray()
            deadline = time.monotonic() + settings.upload_timeout_s
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                message = await asyncio.wait_for(receive(), timeout=remaining)
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > maximum:
                    await reject(413, "request too large", UPLOAD_TOO_LARGE)
                    return
                if not message.get("more_body", False):
                    break

            async def replay() -> Message:
                return {"type": "http.request", "body": bytes(body)}

            await self.app(scope, replay, safe_send)
        except TimeoutError:
            if not started:
                await reject(408, "request deadline exceeded", PROCESSING_TIMEOUT)
        except Exception as exc:
            classified = code_for_exception(exc)
            error_code = (
                DB_UNAVAILABLE if classified == DB_UNAVAILABLE else INTERNAL_ERROR
            )
            logger.error(
                "request.failed",
                extra={"request_id": request_id, "error_code": error_code},
            )
            if error_code == DB_UNAVAILABLE:
                METRICS.inc("db_errors_total", "request")
            if not started:
                await reject(
                    503 if error_code == DB_UNAVAILABLE else 500,
                    "service unavailable"
                    if error_code == DB_UNAVAILABLE
                    else "internal error",
                    error_code,
                )
        finally:
            if admitted:
                self.uploads -= 1
            duration = time.monotonic() - started_at
            status_class = f"{status_code // 100}xx"
            METRICS.inc(
                "requests_total",
                route,
                status_class if status_class in {"2xx", "3xx", "4xx", "5xx"} else "5xx",
            )
            METRICS.observe("request_duration_seconds", duration, route)
            if route not in {"health", "readiness"} or status_code >= 500:
                logger.info(
                    "http.request",
                    extra={
                        "request_id": request_id,
                        "route": route,
                        "status_code": status_code,
                        "duration_ms": duration * 1000,
                    },
                )
