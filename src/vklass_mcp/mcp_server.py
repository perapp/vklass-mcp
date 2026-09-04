"""MCP tool surface for cached Vklass guardian data."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from vklass_mcp.registry import UserRegistry
from vklass_mcp.service import CAPABILITIES, VklassService

_LOG = logging.getLogger(__name__)

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
_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)


def register_tools(mcp: FastMCP[Any], registry: UserRegistry) -> None:
    @mcp.tool(annotations=_READ_ONLY)
    async def vklass_capabilities() -> dict[str, Any]:
        """List Vklass features and the implementation/support level of each."""

        return {
            "read_only": False,
            "write_scope": "vklass.write",
            "features": CAPABILITIES,
        }

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
        """Refresh Vklass caches only; returns status/counts, not records.

        Never use this result alone to answer a question about schedules, events, news,
        assignments, or other Vklass content. After synchronization, always call the
        relevant ``vklass_list_*`` or ``vklass_get_*`` tool to retrieve the records.
        """

        service = await _current_service(registry)
        return await service.sync_all()

    @mcp.tool(annotations=_WRITE)
    async def vklass_report_absence_today(
        child: str,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Report a child absent today in Vklass. Requires vklass.write and confirm=true.

        Vklass applies the school's configured meaning of its Today quick option, which may
        mean the full day or the remainder of the day. This creates a real absence report.
        """

        _require_write_scope()
        if not confirm:
            raise ValueError("confirm must be true to submit a real Vklass absence report")
        service = await _current_service(registry)
        child_id = await _resolve_child(service, child)
        if child_id is None:
            raise ValueError("child is required")
        return await service.report_absence_today(child_id)

    @mcp.tool(annotations=_WRITE)
    async def vklass_report_absence_period(
        child: str,
        start: str,
        end: str,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Report a child absent for an ISO 8601 interval. Requires write scope and confirmation.

        Naive date-times are interpreted in Europe/Stockholm. This creates a real Vklass
        absence report; both start and end must include a time of day.
        """

        _require_write_scope()
        if not confirm:
            raise ValueError("confirm must be true to submit a real Vklass absence report")
        service = await _current_service(registry)
        child_id = await _resolve_child(service, child)
        if child_id is None:
            raise ValueError("child is required")
        start_at = _absence_datetime(start, "start")
        end_at = _absence_datetime(end, "end")
        return await service.report_absence_period(child_id, start_at, end_at)

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
        child: Annotated[
            str | None,
            Field(description="Optional child name/alias; omit to include all own children."),
        ] = None,
        start: Annotated[
            str | None,
            Field(
                description=(
                    "Inclusive start date as YYYY-MM-DD. For an ISO calendar week, pass "
                    "that week's Monday. Defaults to today in Europe/Stockholm."
                )
            ),
        ] = None,
        end: Annotated[
            str | None,
            Field(
                description=(
                    "Inclusive end date as YYYY-MM-DD. For an ISO calendar week, pass "
                    "that week's Sunday. Defaults to 14 days after today."
                )
            ),
        ] = None,
        limit: Annotated[
            int,
            Field(ge=1, le=500, description="Maximum number of schedule-day records."),
        ] = 200,
    ) -> list[dict[str, Any]]:
        """Retrieve omsorgsschema/care/fritids schedule records for a date range.

        Use this tool for planned care hours, drop-off/pick-up, leave, closure, holiday,
        and calendar-week questions. Convert a requested ISO week to its inclusive
        Monday-to-Sunday dates. If freshness is requested, call ``vklass_sync_now`` first,
        then always call this tool; synchronization itself does not return schedule data.
        Do not substitute the school-calendar tool for omsorgsschema.
        """

        started = monotonic()
        try:
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
        except Exception as error:
            _LOG.warning(
                "care schedule query failed error=%s duration_ms=%d",
                type(error).__name__,
                round((monotonic() - started) * 1000),
            )
            raise
        _LOG.info(
            "care schedule query start=%s end=%s child_filtered=%s limit=%d records=%d "
            "duration_ms=%d",
            start_at,
            end_at,
            child_id is not None,
            limit,
            len(records),
            round((monotonic() - started) * 1000),
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


def _require_write_scope() -> None:
    token = get_access_token()
    if token is None or "vklass.write" not in token.scopes:
        raise PermissionError("the OAuth token does not grant the vklass.write scope")


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


def _absence_datetime(value: str, field: str) -> datetime:
    candidate = value.strip()
    pattern = r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?"
    if not re.fullmatch(pattern, candidate):
        raise ValueError(f"{field} must be an ISO 8601 date-time including hours and minutes")
    try:
        parsed = date_parser.isoparse(candidate)
    except (ValueError, OverflowError) as error:
        raise ValueError(f"{field} must be a valid ISO 8601 date-time") from error
    if parsed.second != 0 or parsed.microsecond != 0:
        raise ValueError(f"{field} must use whole minutes")
    if parsed.tzinfo is not None:
        return parsed

    stockholm = ZoneInfo("Europe/Stockholm")
    localized = parsed.replace(tzinfo=stockholm, fold=0)
    round_trip = localized.astimezone(UTC).astimezone(stockholm).replace(tzinfo=None)
    if round_trip != parsed:
        raise ValueError(f"{field} is not a valid local Europe/Stockholm time")
    alternate = parsed.replace(tzinfo=stockholm, fold=1)
    if alternate.utcoffset() != localized.utcoffset():
        raise ValueError(f"{field} is ambiguous; include an explicit UTC offset")
    return localized


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
