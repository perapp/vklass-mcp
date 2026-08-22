"""Application service coordinating authentication, synchronization and cache access."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from vklass_mcp.config import Settings
from vklass_mcp.parsers import (
    normalize_calendar_events,
    normalize_news_items,
    parse_children,
    parse_study_courses,
    parse_weekly_reports,
    plain_text,
    selected_school_id,
    snapshot_record,
)
from vklass_mcp.session_store import SessionStore
from vklass_mcp.storage import Store, utc_now
from vklass_mcp.vklass.client import AuthenticationRequired, VklassClient, VklassResponseError

_LOG = logging.getLogger(__name__)


class VklassService:
    """Long-lived read-only Vklass synchronization service."""

    def __init__(
        self,
        settings: Settings,
        *,
        data_dir: Path | None = None,
        subject: str = "development",
        authentication_required_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.settings = settings
        self.data_dir = data_dir or settings.data_dir
        self.subject = subject
        self.display_name: str | None = None
        self.authentication_required_callback = authentication_required_callback
        self.store = Store(self.data_dir / settings.database_name)
        self.session_store = SessionStore(
            self.data_dir / settings.session_name, settings.resolved_state_key
        )
        self.client = VklassClient(settings.request_timeout_seconds, self._persist_cookie)
        self.auth_state = "starting"
        self.auth_message = "Starting"
        self.last_auth_success: str | None = None
        self.last_auth_error: str | None = None
        self._sync_lock = asyncio.Lock()
        self._background_tasks: list[asyncio.Task[None]] = []
        self._stopping = False

    async def start(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        initial_sync: asyncio.Task[None] | None = None
        try:
            await self.store.open()
            self.display_name = await self.store.get_metadata("display_name")
            await self.client.open()
            cookie = self.session_store.load()
            if cookie:
                self.auth_state = "checking"
                self.auth_message = "Checking saved Vklass session"
                try:
                    resumed = await self.client.restore_cookie(cookie)
                except VklassResponseError:
                    resumed = False
                    self.auth_state = "unavailable"
                    self.auth_message = "Vklass is unavailable; saved session will be retried"
                if resumed:
                    self._mark_authenticated("Saved Vklass session resumed")
                    initial_sync = asyncio.create_task(
                        self._initial_sync(), name="vklass-initial-sync"
                    )
                elif self.auth_state != "unavailable":
                    await self._mark_authentication_required()
            else:
                await self._mark_authentication_required()
            self._background_tasks = [
                asyncio.create_task(self._keepalive_loop(), name="vklass-keepalive"),
                asyncio.create_task(self._sync_loop(), name="vklass-sync"),
            ]
            if initial_sync:
                self._background_tasks.append(initial_sync)
        except Exception:
            await self.client.close()
            await self.store.close()
            raise

    async def stop(self) -> None:
        self._stopping = True
        for task in self._background_tasks:
            task.cancel()
        tasks = list(self._background_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.client.close()
        await self.store.close()

    async def restore_authenticated_session(self, cookie: str, display_name: str) -> None:
        """Replace this user's Vklass session after a successful BankID flow."""

        if not await self.client.restore_cookie(cookie):
            raise AuthenticationRequired("the new Vklass session could not be verified")
        self.display_name = display_name
        self.session_store.save(cookie)
        self._mark_authenticated("Vklass account connected")
        await self.store.set_metadata("display_name", display_name)
        await self.store.set_metadata("auth_state", self.auth_state)
        task = asyncio.create_task(self._initial_sync(), name="vklass-initial-sync")
        self._background_tasks.append(task)

    async def logout(self) -> None:
        await self.client.logout()
        self.session_store.delete()
        self.auth_state = "required"
        self.auth_message = "Logged out; BankID login required"
        await self.store.set_metadata("auth_state", self.auth_state)

    async def sync_all(self) -> dict[str, Any]:
        if not self.client.authenticated:
            raise AuthenticationRequired("BankID login is required before synchronization")
        if self._sync_lock.locked():
            return {"status": "already_running"}
        async with self._sync_lock:
            started = utc_now()
            errors: dict[str, str] = {}
            counts: dict[str, int] = {}

            home_html = await self._capture("home", self.client.home, errors, default="")
            absence_html = await self._capture(
                "absence", self.client.absence_notify, errors, default=""
            )
            children = parse_children(str(absence_html), str(home_html))
            await self.store.upsert_children(children)
            counts["children"] = len(children)

            snapshots = []
            if home_html:
                snapshots.append(snapshot_record("home", "current", str(home_html), "Vklass home"))
            if absence_html:
                snapshots.append(
                    snapshot_record("absence", "current", str(absence_html), "Absence overview")
                )
            await self.store.upsert_records(snapshots)

            meal_records = []
            for child in children:
                meal = child.get("meal") or []
                if meal:
                    meal_records.append(
                        {
                            "kind": "meal",
                            "key": f"{child['id']}:current",
                            "child_id": str(child["id"]),
                            "title": "Dagens mat",
                            "body_text": "\n".join(str(item) for item in meal),
                            "data": {"items": meal},
                        }
                    )
            if home_html and "home" not in errors:
                await self.store.delete_records(kinds=["meal"])
            await self.store.upsert_records(meal_records)
            counts["meals"] = len(meal_records)

            news_records, news_complete = await self._sync_news(errors)
            children_by_name = _unique_child_aliases(children)
            _assign_children(news_records, children_by_name)
            if news_complete:
                await self.store.delete_records(kinds=["news", "weekly_letter"])
            await self.store.upsert_records(news_records)
            counts["news"] = len(news_records)

            reports_html = await self._capture(
                "weekly_reports", self.client.weekly_reports, errors, default=""
            )
            report_records = parse_weekly_reports(str(reports_html))
            _assign_children(report_records, children_by_name)
            if report_records and "weekly_reports" not in errors:
                await self.store.delete_records(kinds=["weekly_report"])
            await self.store.upsert_records(report_records)
            counts["weekly_reports"] = len(report_records)

            scoreboard = await self._capture(
                "notifications", self.client.scoreboard, errors, default=None
            )
            if scoreboard is not None:
                await self.store.upsert_records(
                    [
                        {
                            "kind": "notifications",
                            "key": "current",
                            "title": "Notifications",
                            "body_text": plain_text(scoreboard),
                            "data": (
                                scoreboard
                                if isinstance(scoreboard, dict)
                                else {"value": scoreboard}
                            ),
                        }
                    ]
                )
                counts["notifications"] = 1

            study_html = await self._capture(
                "study_overview", self.client.study_overview, errors, default=""
            )
            if study_html:
                await self.store.upsert_records(
                    [
                        snapshot_record(
                            "study_overview", "current", str(study_html), "Study overview"
                        )
                    ]
                )
                counts["study_overview"] = 1
            course_records = await self._sync_study_courses(children, errors)
            await self.store.upsert_records(course_records)
            counts["study_courses"] = len(course_records)

            calendar_records = await self._sync_calendars(children, errors)
            await self.store.upsert_records(calendar_records)
            counts["calendar"] = sum(1 for item in calendar_records if item["kind"] == "calendar")
            counts["assignments"] = sum(
                1 for item in calendar_records if item["kind"] == "assignment"
            )

            completed = utc_now()
            await self.store.set_metadata("last_sync_started", started)
            await self.store.set_metadata("last_sync_completed", completed)
            await self.store.set_metadata(
                "last_sync_errors", json.dumps(errors, ensure_ascii=False)
            )
            await self.store.set_metadata("auth_state", self.auth_state)
            return {"status": "ok" if not errors else "partial", "counts": counts, "errors": errors}

    async def get_news_article(self, article_id: str) -> dict[str, Any] | None:
        record = await self.store.get_record("news", article_id)
        if record is None:
            record = await self.store.get_record("weekly_letter", article_id)
        if record is None:
            return None
        if not self.client.authenticated or not article_id.isdigit():
            return record
        try:
            html = await self.client.news_article(article_id)
        except AuthenticationRequired:
            await self._mark_authentication_required()
            raise
        except Exception:
            return record
        body = plain_text(html)
        if record:
            data = {**record["data"], "detail_fetched": True}
            updated = {
                **record,
                "body_text": body,
                "body_html": html,
                "data": data,
            }
            await self.store.upsert_records([updated])
            return await self.store.get_record(record["kind"], record["key"])
        return None

    async def status(self) -> dict[str, Any]:
        metadata = await self.store.metadata()
        self.display_name = self.display_name or metadata.get("display_name")
        return {
            "account": {"display_name": self.display_name},
            "authentication": {
                "state": self.auth_state,
                "message": self.auth_message,
                "authenticated": self.client.authenticated,
                "last_success": self.last_auth_success,
                "last_error": self.last_auth_error,
            },
            "sync": {
                "running": self._sync_lock.locked(),
                "last_started": metadata.get("last_sync_started"),
                "last_completed": metadata.get("last_sync_completed"),
                "errors": _parse_json(metadata.get("last_sync_errors"), {}),
                "record_counts": await self.store.record_counts(),
            },
        }

    async def _persist_cookie(self, value: str) -> None:
        self.session_store.save(value)

    def _mark_authenticated(self, message: str) -> None:
        self.auth_state = "authenticated"
        self.auth_message = message
        self.last_auth_success = utc_now()
        self.last_auth_error = None

    async def _sync_news(self, errors: dict[str, str]) -> tuple[list[dict[str, Any]], bool]:
        records: list[dict[str, Any]] = []
        page_token: str | None = None
        complete = False
        try:
            for _ in range(self.settings.max_news_pages):
                payload = await self.client.news_page(page_token)
                page, page_token = normalize_news_items(payload)
                records.extend(page)
                if not page_token:
                    complete = True
                    break
                if len(records) >= self.settings.max_news_items:
                    break
            return records[: self.settings.max_news_items], complete
        except AuthenticationRequired:
            await self._mark_authentication_required()
            raise
        except Exception as error:
            errors["news"] = f"{type(error).__name__}: {error}"
            return records, False

    async def _sync_study_courses(
        self, children: list[dict[str, Any]], errors: dict[str, str]
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for child in children:
            child_id = str(child["id"])
            try:
                student_html = await self.client.study_student(child_id)
                school_id = selected_school_id(student_html)
                if not school_id:
                    continue
                courses_html = await self.client.study_courses(child_id, school_id)
                child_records = parse_study_courses(courses_html, child_id)
                if child_records:
                    await self.store.delete_records(kinds=["study_course"], child_id=child_id)
                    await self.store.upsert_records(child_records)
                records.extend(child_records)
            except AuthenticationRequired:
                await self._mark_authentication_required()
                raise
            except Exception as error:
                errors[f"study_courses:{child_id}"] = f"{type(error).__name__}: {error}"
        return records

    async def _sync_calendars(
        self, children: list[dict[str, Any]], errors: dict[str, str]
    ) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        requested_start = now - timedelta(days=self.settings.calendar_past_days)
        requested_end = now + timedelta(days=self.settings.calendar_future_days)
        month_start = datetime(requested_start.year, requested_start.month, 1, tzinfo=UTC)
        records: list[dict[str, Any]] = []
        for child in children:
            child_id = str(child["id"])
            cursor = month_start
            while cursor <= requested_end:
                next_month = _next_month(cursor)
                try:
                    payload = await self.client.calendar(child_id, cursor, next_month)
                    month_records = normalize_calendar_events(payload, child_id)
                    await self.store.delete_record_window(
                        kinds=["calendar", "assignment"],
                        child_id=child_id,
                        start_at=cursor.isoformat(),
                        end_at=next_month.isoformat(),
                    )
                    await self.store.upsert_records(month_records)
                    records.extend(month_records)
                except AuthenticationRequired:
                    await self._mark_authentication_required()
                    raise
                except Exception as error:
                    month = cursor.strftime("%Y-%m")
                    errors[f"calendar:{child_id}:{month}"] = f"{type(error).__name__}: {error}"
                cursor = next_month
        return records

    async def _capture(
        self,
        key: str,
        getter: Any,
        errors: dict[str, str],
        *,
        default: Any,
    ) -> Any:
        try:
            return await getter()
        except AuthenticationRequired:
            await self._mark_authentication_required()
            raise
        except Exception as error:
            errors[key] = f"{type(error).__name__}: {error}"
            return default

    async def _mark_authentication_required(self) -> None:
        self.auth_state = "required"
        self.auth_message = "Vklass session expired; BankID login required"
        await self.store.set_metadata("auth_state", self.auth_state)
        if self.authentication_required_callback:
            await self.authentication_required_callback(self.subject)

    async def _initial_sync(self) -> None:
        try:
            await self.sync_all()
        except AuthenticationRequired:
            await self._mark_authentication_required()
        except Exception as error:
            _LOG.warning("initial Vklass sync failed: %s", type(error).__name__)

    async def _keepalive_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self.settings.keepalive_interval_seconds)
            if not self.client.authenticated:
                if self.auth_state == "unavailable":
                    try:
                        if await self.client.verify():
                            self._mark_authenticated("Saved Vklass session resumed")
                            await self.sync_all()
                        else:
                            await self._mark_authentication_required()
                    except VklassResponseError:
                        pass
                continue
            try:
                await self.client.keepalive()
            except AuthenticationRequired:
                await self._mark_authentication_required()
            except Exception as error:
                _LOG.warning("Vklass keepalive failed: %s", type(error).__name__)

    async def _sync_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self.settings.sync_interval_seconds)
            if not self.client.authenticated:
                continue
            try:
                await self.sync_all()
            except AuthenticationRequired:
                await self._mark_authentication_required()
            except Exception as error:
                _LOG.warning("scheduled Vklass sync failed: %s", type(error).__name__)


CAPABILITIES: list[dict[str, Any]] = [
    {"feature": "children", "support": "implemented", "source": "/Absence/Notify"},
    {
        "feature": "news_and_teacher_weekly_letters",
        "support": "implemented",
        "source": "/Home/NewsArticles",
    },
    {
        "feature": "calendar_lessons_homework_tests",
        "support": "implemented",
        "source": "/Events/FullCalendar",
    },
    {
        "feature": "automatic_weekly_reports",
        "support": "implemented",
        "source": "/WeeklyReports/Archive/",
    },
    {"feature": "meals", "support": "implemented", "source": "/Home/Welcome"},
    {
        "feature": "notification_count",
        "support": "implemented",
        "source": "/Account/Scoreboard",
    },
    {
        "feature": "study_courses_and_judgements",
        "support": "implemented",
        "source": "/StudyOverview/Courses/{student}",
    },
    {
        "feature": "study_overview",
        "support": "snapshot",
        "source": "/StudyOverview/Student",
    },
    {"feature": "absence_overview", "support": "snapshot", "source": "/Absence/Notify"},
    {"feature": "class_list", "support": "disabled_privacy_other_children"},
    {"feature": "news_attachments", "support": "metadata_only", "source": "/Home/NewsArticles"},
    {"feature": "messages", "support": "not_yet_mapped"},
    {"feature": "documents", "support": "not_yet_mapped"},
    {"feature": "development_talks", "support": "not_yet_mapped"},
    {"feature": "care_schedule", "support": "not_yet_mapped"},
    {"feature": "leave_and_absence_writes", "support": "disabled_read_only"},
]


def _unique_child_aliases(children: list[dict[str, Any]]) -> dict[str, str]:
    candidates: dict[str, set[str]] = {}
    for child in children:
        child_id = str(child["id"])
        for value in (child.get("name"), child.get("home_name")):
            alias = str(value or "").strip().casefold()
            if alias:
                candidates.setdefault(alias, set()).add(child_id)
    return {
        alias: next(iter(child_ids))
        for alias, child_ids in candidates.items()
        if len(child_ids) == 1
    }


def _assign_children(records: list[dict[str, Any]], children_by_name: dict[str, str]) -> None:
    for record in records:
        searchable = f"{record.get('audience', '')}\n{record.get('title', '')}".casefold()
        for name, child_id in children_by_name.items():
            if name and name in searchable:
                record["child_id"] = child_id
                break


def _next_month(value: datetime) -> datetime:
    if value.month == 12:
        return value.replace(year=value.year + 1, month=1)
    return value.replace(month=value.month + 1)


def _parse_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default
