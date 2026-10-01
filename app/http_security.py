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
from .security import tenant_for_api_key

logger = logging.getLogger("papela.api")


class SecurityBoundary:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.uploads = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        try:
            request_id = str(UUID(request.headers.get("x-request-id", "")))
        except ValueError:
            request_id = str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        settings = get_settings()
        started = False

        async def safe_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message.setdefault("headers", []).extend(
                    [
                        (b"x-request-id", request_id.encode()),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"cache-control", b"no-store"),
                    ]
                )
            await send(message)

        async def reject(code: int, error: str) -> None:
            response = JSONResponse(
                {"request_id": request_id, "error": error}, status_code=code
            )
            if code == 429:
                response.headers["Retry-After"] = "60"
            await response(scope, receive, safe_send)

        upload = scope["path"] == "/v1/upload" and scope["method"] == "POST"
        admitted = False
        try:
            if scope["path"].startswith("/v1/"):
                keys = request.headers.getlist("x-api-key")
                tenant_id = tenant_for_api_key(keys[0]) if len(keys) == 1 else None
                if tenant_id is None:
                    await reject(401, "invalid api key")
                    return
                scope["state"]["tenant_id"] = tenant_id
                if not scope["app"].state.rate_limiter.allow(tenant_id):
                    await reject(429, "rate limit exceeded")
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
                await reject(400, "invalid content length")
                return
            if sizes and int(sizes[0]) > maximum:
                await reject(413, "request too large")
                return
            if upload:
                if self.uploads >= settings.max_concurrent_uploads:
                    await reject(429, "upload capacity exceeded")
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
                    await reject(413, "request too large")
                    return
                if not message.get("more_body", False):
                    break

            async def replay() -> Message:
                return {"type": "http.request", "body": bytes(body)}

            await self.app(scope, replay, safe_send)
        except TimeoutError:
            if not started:
                await reject(408, "request deadline exceeded")
        except Exception:
            logger.error("request.failed", extra={"request_id": request_id})
            if not started:
                await reject(500, "internal error")
        finally:
            if admitted:
                self.uploads -= 1
