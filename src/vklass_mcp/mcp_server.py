"""MCP tool surface for cached Vklass guardian data."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from vklass_mcp.registry import UserRegistry
from vklass_mcp.service import CAPABILITIES, VklassService

_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_LIVE_READ = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)


def register_tools(mcp: FastMCP[Any], registry: UserRegistry) -> None:
    @mcp.tool(annotations=_READ_ONLY)
    async def vklass_capabilities() -> dict[str, Any]:
        """List Vklass features and the implementation/support level of each."""

        return {"read_only": True, "features": CAPABILITIES}

    @mcp.tool(annotations=_READ_ONLY)
    async def vklass_status() -> dict[str, Any]:
        """Show authentication, synchronization and cache status without exposing secrets."""

        service = await _current_service(registry)
        return await service.status()

    @mcp.tool(annotations=_READ_ONLY)
    async def vklass_list_children() -> list[dict[str, Any]]:
        """List children available to the authenticated guardian account."""

        service = await _current_service(registry)
        children = await service.store.list_children()
        return [_public_child(child) for child in children]

    @mcp.tool(annotations=_LIVE_READ)
    async def vklass_sync_now() -> dict[str, Any]:
        """Read current data from Vklass and refresh the local cache."""

        service = await _current_service(registry)
        return await service.sync_all()

    @mcp.tool(annotations=_READ_ONLY)
    async def vklass_list_weekly_letters(
        child: str | None = None,
        query: str | None = None,
        since: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List teacher-published news articles identified as weekly letters (veckobrev)."""

        service = await _current_service(registry)
        child_id = await _resolve_child(service, child)
        records = await service.store.query_records(
            kinds=["weekly_letter"],
            child_id=child_id,
            start_at=_normalize_date(since),
            query=query,
            limit=limit,
            newest_first=True,
        )
        return [_public_record(record, preview=True) for record in records]

    @mcp.tool(annotations=_LIVE_READ)
    async def vklass_get_weekly_letter(article_id: str) -> dict[str, Any] | None:
        """Get the full plain text of one teacher weekly letter by its article ID."""

        service = await _current_service(registry)
        record = await service.store.get_record("weekly_letter", article_id)
        if record is None or not record["body_text"]:
            record = await service.get_news_article(article_id)
        return _public_record(record, preview=False) if record else None

    @mcp.tool(annotations=_READ_ONLY)
    async def vklass_list_news(
        child: str | None = None,
        query: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List Vklass news, including weekly letters, optionally filtered by child or text."""

        service = await _current_service(registry)
        child_id = await _resolve_child(service, child)
        records = await service.store.query_records(
            kinds=["news", "weekly_letter"],
            child_id=child_id,
            start_at=_normalize_date(since),
            query=query,
            limit=limit,
            newest_first=True,
        )
        return [_public_record(record, preview=True) for record in records]

    @mcp.tool(annotations=_LIVE_READ)
    async def vklass_get_news_article(article_id: str) -> dict[str, Any] | None:
        """Get the full plain text and attachment metadata for one Vklass news article."""

        service = await _current_service(registry)
        record = await service.get_news_article(article_id)
        return _public_record(record, preview=False) if record else None

    @mcp.tool(annotations=_READ_ONLY)
    async def vklass_list_calendar(
        child: str | None = None,
        start: str | None = None,
        end: str | None = None,
        query: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """List cached lessons, events, homework and tests for a date range."""

        service = await _current_service(registry)
        child_id = await _resolve_child(service, child)
        start_at = _normalize_date(start) or datetime.now(UTC).isoformat()
        end_at = (
            _normalize_date(end, end_of_day=True)
            or (datetime.now(UTC) + timedelta(days=14)).isoformat()
        )
        records = await service.store.query_records(
            kinds=["calendar", "assignment"],
            child_id=child_id,
            start_at=start_at,
            end_at=end_at,
            query=query,
            limit=limit,
        )
        return [_public_record(record, preview=False) for record in records]

    @mcp.tool(annotations=_READ_ONLY)
    async def vklass_list_assignments(
        child: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List cached homework, tests, submissions and other assignment-type events."""

        service = await _current_service(registry)
        child_id = await _resolve_child(service, child)
        start_at = _normalize_date(start) or datetime.now(UTC).isoformat()
        end_at = (
            _normalize_date(end, end_of_day=True)
            or (datetime.now(UTC) + timedelta(days=31)).isoformat()
        )
        records = await service.store.query_records(
            kinds=["assignment"],
            child_id=child_id,
            start_at=start_at,
            end_at=end_at,
            limit=limit,
        )
        return [_public_record(record, preview=False) for record in records]

    @mcp.tool(annotations=_READ_ONLY)
    async def vklass_list_care_schedule(
        child: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """List cached omsorgsschema with planned care and actual drop-off/pick-up times."""

        service = await _current_service(registry)
        child_id = await _resolve_child(service, child)
        local_today = datetime.now(ZoneInfo("Europe/Stockholm")).date()
        start_at = _normalize_date(start or local_today.isoformat())
        end_at = _normalize_date(
            end or (local_today + timedelta(days=14)).isoformat(), end_of_day=True
        )
        records = await service.store.query_records(
            kinds=["care_schedule"],
            child_id=child_id,
            start_at=start_at,
            end_at=end_at,
            limit=limit,
        )
        return [_public_record(record, preview=False) for record in records]

    @mcp.tool(annotations=_READ_ONLY)
    async def vklass_list_automatic_weekly_reports(
        child: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List Vklass automatic weekly reports; these differ from teacher weekly letters."""

        service = await _current_service(registry)
        child_id = await _resolve_child(service, child)
        records = await service.store.query_records(
            kinds=["weekly_report"], child_id=child_id, limit=limit, newest_first=True
        )
        return [_public_record(record, preview=False) for record in records]

    @mcp.tool(annotations=_READ_ONLY)
    async def vklass_get_meals(child: str | None = None) -> list[dict[str, Any]]:
        """Get the currently cached meal information from each child's home card."""

        service = await _current_service(registry)
        child_id = await _resolve_child(service, child)
        records = await service.store.query_records(kinds=["meal"], child_id=child_id, limit=20)
        return [_public_record(record, preview=False) for record in records]

    @mcp.tool(annotations=_READ_ONLY)
    async def vklass_get_notifications() -> dict[str, Any] | None:
        """Get the current Vklass notification scoreboard/count."""

        service = await _current_service(registry)
        record = await service.store.get_record("notifications", "current")
        return _public_record(record, preview=False) if record else None

    @mcp.tool(annotations=_READ_ONLY)
    async def vklass_list_study_courses(
        child: str | None = None,
        query: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List cached subjects/courses with published judgements and grades."""

        service = await _current_service(registry)
        child_id = await _resolve_child(service, child)
        records = await service.store.query_records(
            kinds=["study_course"],
            child_id=child_id,
            query=query,
            limit=limit,
            newest_first=True,
        )
        return [_public_record(record, preview=False) for record in records]

    @mcp.tool(annotations=_READ_ONLY)
    async def vklass_get_feature_snapshot(feature: str) -> dict[str, Any] | None:
        """Get a plain-text snapshot of home, absence or the study overview."""

        service = await _current_service(registry)
        allowed = {"home", "absence", "study_overview"}
        if feature not in allowed:
            raise ValueError(f"feature must be one of: {', '.join(sorted(allowed))}")
        record = await service.store.get_record(feature, "current")
        return _public_record(record, preview=False) if record else None

    @mcp.tool(annotations=_READ_ONLY)
    async def vklass_search(
        query: str,
        child: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Search all normalized cached Vklass content for text."""

        if len(query.strip()) < 2:
            raise ValueError("query must contain at least two characters")
        service = await _current_service(registry)
        child_id = await _resolve_child(service, child)
        kinds = [
            "weekly_letter",
            "news",
            "assignment",
            "calendar",
            "weekly_report",
            "care_schedule",
            "meal",
            "home",
            "absence",
            "study_overview",
            "study_course",
        ]
        records = await service.store.query_records(
            kinds=kinds,
            child_id=child_id,
            query=query,
            limit=limit,
            newest_first=True,
        )
        return [_public_record(record, preview=True) for record in records]


async def _current_service(registry: UserRegistry) -> VklassService:
    token = get_access_token()
    if token is None or not token.subject:
        raise PermissionError("OAuth user identity is required")
    return await registry.get(token.subject)


async def _resolve_child(service: VklassService, value: str | None) -> str | None:
    if not value:
        return None
    children = await service.store.list_children()
    exact_id = [child for child in children if child["id"] == value]
    if exact_id:
        return str(exact_id[0]["id"])
    matches = [child for child in children if child["name"].casefold() == value.casefold()]
    if len(matches) == 1:
        return str(matches[0]["id"])
    partial = [child for child in children if value.casefold() in child["name"].casefold()]
    if len(partial) == 1:
        return str(partial[0]["id"])
    raise ValueError("child must uniquely match a child ID or name")


def _normalize_date(value: str | None, *, end_of_day: bool = False) -> str | None:
    if not value:
        return None
    parsed = date_parser.parse(value)
    date_only = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Europe/Stockholm"))
    if end_of_day and date_only:
        parsed = parsed + timedelta(days=1, microseconds=-1)
    return str(parsed.astimezone(UTC).isoformat())


def _public_child(child: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": child["id"],
        "name": child["name"],
        "school_id": child.get("school_id"),
        "school_name": child.get("school_name"),
        "updated_at": child["updated_at"],
    }


def _public_record(record: dict[str, Any], *, preview: bool) -> dict[str, Any]:
    text = str(record.get("body_text") or "")
    if preview and len(text) > 500:
        text = text[:500] + "…"
    return {
        "type": record["kind"],
        "id": record["key"],
        "child_id": record.get("child_id"),
        "title": record.get("title", ""),
        "audience": record.get("audience", ""),
        "start": record.get("start_at"),
        "end": record.get("end_at"),
        "text": text,
        "details": record.get("data") or {},
        "source_updated_at": record.get("source_updated_at"),
        "cached_at": record.get("cached_at"),
    }
