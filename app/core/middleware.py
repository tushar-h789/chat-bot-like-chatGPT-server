import logging
import time

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import error_body

logger = logging.getLogger(__name__)


class SecurityHeadersMiddleware:
    """Add browser-hardening headers without buffering a streamed body."""

    def __init__(self, app: ASGIApp, *, environment: str) -> None:
        self.app = app
        self._environment = environment

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "no-referrer"
                headers["Permissions-Policy"] = (
                    "camera=(), microphone=(), geolocation=()"
                )
                headers["Content-Security-Policy"] = (
                    "default-src 'none'; frame-ancestors 'none'"
                )
                if self._environment == "production":
                    headers["Strict-Transport-Security"] = (
                        "max-age=31536000; includeSubDomains"
                    )
            await send(message)

        await self.app(scope, receive, send_with_headers)


class AccessLogMiddleware:
    """Log the request line after the response finishes. Bodies are not logged."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 0

        async def send_logged(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            if message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                duration_ms = int((time.perf_counter() - started) * 1000)
                logger.info(
                    "request method=%s path=%s status=%s duration_ms=%s",
                    scope.get("method", ""),
                    scope.get("path", ""),
                    status,
                    duration_ms,
                )
            await send(message)

        await self.app(scope, receive, send_logged)


class BodyLimitMiddleware:
    """Reject a declared body larger than the configured cap before reading it."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        raw_length = Headers(scope=scope).get("content-length")
        if (
            raw_length is not None
            and raw_length.isdigit()
            and int(raw_length) > self._max_bytes
        ):
            response = JSONResponse(
                status_code=413,
                content=error_body(
                    "payload_too_large", "The request body is too large."
                ),
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
