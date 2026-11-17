from __future__ import annotations

import json
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings
from app.core.request_context import get_request_id
from app.schemas.responses import error_payload


class _BodyTooLarge(Exception):
    pass


class SecurityMiddleware:
    """入口防护：请求体大小上限与安全响应头（设计 9.8）。"""

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        max_bytes = self.settings.max_request_body_bytes
        header_map = {key.lower(): value for key, value in (scope.get("headers") or [])}
        declared = header_map.get(b"content-length")
        if declared:
            try:
                if int(declared) > max_bytes:
                    await self._send_too_large(send, max_bytes)
                    return
            except ValueError:
                pass

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_bytes:
                    raise _BodyTooLarge
            return message

        response_started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                message = dict(message)
                message["headers"] = self._merge_headers(message.get("headers") or [])
            await send(message)

        try:
            await self.app(scope, limited_receive, send_with_headers)
        except _BodyTooLarge:
            if response_started:
                raise
            await self._send_too_large(send_with_headers, max_bytes)

    def _security_headers(self) -> list[tuple[bytes, bytes]]:
        if not self.settings.security_headers_enabled:
            return []
        headers = [
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"no-referrer"),
            (b"permissions-policy", b"geolocation=(), microphone=(), camera=()"),
            (b"content-security-policy", self.settings.content_security_policy.encode("utf-8")),
            (b"cross-origin-opener-policy", b"same-origin"),
            (b"cross-origin-resource-policy", b"same-site"),
        ]
        if self.settings.hsts_max_age_seconds > 0:
            value = f"max-age={self.settings.hsts_max_age_seconds}; includeSubDomains"
            headers.append((b"strict-transport-security", value.encode("utf-8")))
        return headers

    def _merge_headers(self, existing: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
        present = {key.lower() for key, _ in existing}
        return list(existing) + [
            (key, value) for key, value in self._security_headers() if key not in present
        ]

    async def _send_too_large(self, send: Send, max_bytes: int) -> None:
        payload: dict[str, Any] = error_payload(
            "PAYLOAD_TOO_LARGE", "请求体超过大小限制", {"max_bytes": max_bytes}
        )
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = self._merge_headers(
            [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"x-request-id", str(get_request_id()).encode("ascii")),
            ]
        )
        await send({"type": "http.response.start", "status": 413, "headers": headers})
        await send({"type": "http.response.body", "body": body})
