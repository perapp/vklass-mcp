"""Authenticated, read-only client for the Vklass guardian web application."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp
from yarl import URL

from vklass_mcp.vklass.auth import authenticate as authenticate_goteborg

_LOG = logging.getLogger(__name__)

CUSTODIAN_BASE = "https://custodian.vklass.se"
AUTH_COOKIE_NAME = "se.vklass.authentication"
CookieCallback = Callable[[str], Awaitable[None]]
QRCallback = Callable[[str], Awaitable[None]]


class AuthenticationRequired(PermissionError):
    """The Vklass session is missing or no longer valid."""


class VklassResponseError(ConnectionError):
    """A Vklass endpoint returned an unexpected response."""


class VklassClient:
    """Own the Vklass cookie jar and expose only read/query operations."""

    def __init__(self, timeout_seconds: int, cookie_callback: CookieCallback) -> None:
        self.timeout_seconds = timeout_seconds
        self.cookie_callback = cookie_callback
        self.session: aiohttp.ClientSession | None = None
        self.authenticated = False
        self._last_cookie: str | None = None
        self._operation_lock = asyncio.Lock()

    async def open(self) -> None:
        if self.session and not self.session.closed:
            return
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        self.session = aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(quote_cookie=False),
            requote_redirect_url=False,
            timeout=timeout,
            raise_for_status=False,
            headers={
                "Accept": "*/*",
                "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.7",
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) VklassMCP/0.3",
            },
        )

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None
        self.authenticated = False

    async def restore_cookie(self, value: str) -> bool:
        async with self._operation_lock:
            session = self._session
            session.cookie_jar.clear()
            cookie = SimpleCookie()
            cookie[AUTH_COOKIE_NAME] = value.strip()
            cookie[AUTH_COOKIE_NAME]["domain"] = ".vklass.se"
            cookie[AUTH_COOKIE_NAME]["path"] = "/"
            session.cookie_jar.update_cookies(cookie, response_url=URL(CUSTODIAN_BASE))
            self._last_cookie = value.strip()
            return await self._verify_unlocked()

    async def authenticate_bankid(self, organisation_id: int, qr_callback: QRCallback) -> None:
        async with self._operation_lock:
            session = self._session
            self.authenticated = False
            self._last_cookie = None
            session.cookie_jar.clear()
            await authenticate_goteborg(session, qr_callback, organisation_id)
            if not await self._verify_unlocked():
                raise AuthenticationRequired(
                    "BankID completed but Vklass did not accept the session"
                )
            await self._capture_cookie(force=True)

    async def verify(self) -> bool:
        async with self._operation_lock:
            return await self._verify_unlocked()

    async def _verify_unlocked(self) -> bool:
        try:
            async with self._session.get(
                f"{CUSTODIAN_BASE}/Home/Welcome", allow_redirects=False
            ) as response:
                await response.read()
                valid = (
                    response.status == 200
                    and urlparse(str(response.url)).hostname == "custodian.vklass.se"
                )
        except (TimeoutError, aiohttp.ClientError) as error:
            self.authenticated = False
            raise VklassResponseError("Vklass verification is temporarily unavailable") from error
        if response.status not in (200, 301, 302, 303, 307, 308, 401, 403):
            self.authenticated = False
            raise VklassResponseError(f"Vklass verification returned HTTP {response.status}")
        self.authenticated = valid
        if valid:
            await self._capture_cookie()
        return valid

    async def logout(self) -> None:
        async with self._operation_lock:
            self._session.cookie_jar.clear()
            self._last_cookie = None
            self.authenticated = False

    async def keepalive(self) -> None:
        await self.get_text("/")

    async def account_page(self) -> str:
        return await self.get_text("/")

    async def home(self) -> str:
        return await self.get_text("/Home/Welcome")

    async def absence_notify(self) -> str:
        return await self.get_text("/Absence/Notify")

    async def weekly_reports(self) -> str:
        return await self.get_text(
            "/WeeklyReports/Archive/",
            headers={"X-Requested-With": "Fetch", "vk-client-has-tracking-detail": "True"},
        )

    async def scoreboard(self) -> Any:
        return await self.get_json("/Account/Scoreboard")

    async def care_schedule(self, from_date: date) -> Any:
        return await self.get_json(
            "/CareSchedule/LoadDays",
            params={"fromDate": from_date.isoformat()},
            headers={"vk-json-load": "true"},
        )

    async def study_overview(self) -> str:
        return await self.get_text("/StudyOverview/Student")

    async def study_student(self, child_id: str) -> str:
        if not child_id.isdigit():
            raise ValueError("child IDs must be numeric")
        return await self.get_text(f"/StudyOverview/Student/{child_id}")

    async def study_courses(self, child_id: str, school_id: str) -> str:
        if not child_id.isdigit() or not school_id.isdigit():
            raise ValueError("child and school IDs must be numeric")
        return await self.get_text(
            f"/StudyOverview/Courses/{child_id}", params={"school": school_id}
        )

    async def news_page(self, page_token: str | None = None) -> Any:
        params = {"pageToken": page_token} if page_token else None
        return await self.get_json("/Home/NewsArticles", params=params)

    async def news_article(self, article_id: str) -> str:
        if not article_id.isdigit():
            raise ValueError("news article IDs must be numeric")
        return await self.get_text(f"/Home/News/{article_id}")

    async def calendar(
        self,
        child_id: str,
        start: datetime,
        end: datetime,
    ) -> Any:
        if not child_id.isdigit():
            raise ValueError("child IDs must be numeric")
        return await self.post_json(
            "/Events/FullCalendar",
            data={"students": child_id, "start": start.isoformat(), "end": end.isoformat()},
        )

    async def get_text(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        response, body = await self._request("GET", path, params=params, headers=headers)
        content_type = response.headers.get("Content-Type", "").lower()
        if "text/" not in content_type and "html" not in content_type:
            raise VklassResponseError(f"expected text from {path}, got {content_type or 'unknown'}")
        return body.decode(response.charset or "utf-8", errors="replace")

    async def get_json(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        _, body = await self._request("GET", path, params=params, headers=headers)
        try:
            return json.loads(body)
        except json.JSONDecodeError as error:
            raise VklassResponseError(f"expected JSON from {path}") from error

    async def post_json(self, path: str, *, data: dict[str, str]) -> Any:
        _, body = await self._request("POST", path, data=data)
        try:
            return json.loads(body)
        except json.JSONDecodeError as error:
            raise VklassResponseError(f"expected JSON from {path}") from error

    async def download(
        self,
        url: str,
        *,
        maximum_bytes: int = 10_000_000,
    ) -> tuple[bytes, str]:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(".vklass.se")
        ):
            raise ValueError("downloads are restricted to HTTPS Vklass hosts")
        async with self._operation_lock, self._session.get(url, allow_redirects=False) as response:
            if response.status in (301, 302, 303, 307, 308, 401, 403):
                self.authenticated = False
                await response.read()
                raise AuthenticationRequired("Vklass session expired while downloading")
            if response.status != 200:
                await response.read()
                raise VklassResponseError(f"download returned HTTP {response.status}")
            if int(response.headers.get("Content-Length", "0") or 0) > maximum_bytes:
                raise ValueError("attachment exceeds configured size limit")
            body = await response.content.read(maximum_bytes + 1)
            if len(body) > maximum_bytes:
                raise ValueError("attachment exceeds configured size limit")
            await self._capture_cookie()
            return body, response.headers.get("Content-Type", "application/octet-stream")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[aiohttp.ClientResponse, bytes]:
        async with self._operation_lock:
            if not self.authenticated:
                raise AuthenticationRequired("Vklass login is required")
            url = urljoin(f"{CUSTODIAN_BASE}/", path.lstrip("/"))
            if urlparse(url).hostname != "custodian.vklass.se":
                raise ValueError("Vklass request escaped the custodian host")
            async with self._session.request(
                method,
                url,
                params=params,
                data=data,
                headers=headers,
                allow_redirects=False,
            ) as response:
                body = await response.read()
                if response.status in (301, 302, 303, 307, 308, 401, 403):
                    self.authenticated = False
                    raise AuthenticationRequired("Vklass session expired")
                if response.status != 200:
                    raise VklassResponseError(f"{path} returned HTTP {response.status}")
                await self._capture_cookie()
                return response, body

    async def _capture_cookie(self, *, force: bool = False) -> None:
        morsel = self._session.cookie_jar.filter_cookies(URL(CUSTODIAN_BASE)).get(AUTH_COOKIE_NAME)
        if not morsel:
            return
        value = morsel.value
        if force or value != self._last_cookie:
            self._last_cookie = value
            await self.cookie_callback(value)

    @property
    def _session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            raise RuntimeError("Vklass client is not open")
        return self.session
