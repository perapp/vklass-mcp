"""Small in-process safety limits for public OAuth and MCP endpoints."""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from starlette.types import ASGIApp, Receive, Scope, Send


@dataclass(frozen=True)
class Limit:
    requests: int
    seconds: int


class RateLimitMiddleware:
    """Apply conservative per-peer limits without trusting forwarded headers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        limit_name, limit = _limit_for(scope)
        if limit is None:
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        peer = str(client[0]) if client else "unknown"
        key = (peer, limit_name)
        now = time.monotonic()
        async with self._lock:
            events = self._events[key]
            cutoff = now - limit.seconds
            while events and events[0] <= cutoff:
                events.popleft()
            allowed = len(events) < limit.requests
            if allowed:
                events.append(now)
        if not allowed:
            await _send_rate_limited(send, limit.seconds)
            return
        await self.app(scope, receive, send)


def _limit_for(scope: Scope) -> tuple[str, Limit | None]:
    if scope.get("type") != "http":
        return "", None
    path = str(scope.get("path", ""))
    if path == "/register":
        return "register", Limit(30, 3600)
    if path == "/authorize":
        return "authorize", Limit(300, 60)
    if path == "/token":
        return "token", Limit(600, 60)
    if path.startswith("/auth/vklass/") and path.endswith("/start"):
        return "bankid", Limit(20, 60)
    if path.rstrip("/") == "/mcp":
        return "mcp", Limit(1200, 60)
    return "", None


async def _send_rate_limited(send: Send, retry_after: int) -> None:
    body = json.dumps(
        {"error": "temporarily_unavailable", "error_description": "rate limit exceeded"}
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
                (b"retry-after", str(retry_after).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
