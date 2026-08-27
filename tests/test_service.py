from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from vklass_mcp.config import Settings
from vklass_mcp.service import VklassService
from vklass_mcp.vklass.client import AuthenticationRequired, VklassSubmissionOutcomeUnknown


@pytest.mark.asyncio
async def test_expired_authentication_aborts_sync_capture(tmp_path: Path) -> None:
    service = VklassService(
        Settings(data_dir=tmp_path, state_key="state-key-for-tests", _env_file=None)
    )
    await service.store.open()

    async def expired() -> None:
        raise AuthenticationRequired("expired")

    try:
        with pytest.raises(AuthenticationRequired):
            await service._capture("home", expired, {}, default="")
        assert service.auth_state == "required"
        assert await service.store.get_metadata("auth_state") == "required"
    finally:
        await service.store.close()


@pytest.mark.asyncio
async def test_report_absence_updates_cached_snapshot(tmp_path: Path) -> None:
    service = VklassService(
        Settings(data_dir=tmp_path, state_key="state-key-for-tests", _env_file=None)
    )
    service.client.authenticated = True
    submitted: list[str] = []

    async def report_absence_today(child_id: str) -> dict[str, str]:
        submitted.append(child_id)
        return {"mode": "today", "date": "2026-08-27"}

    async def absence_notify() -> str:
        return "<h1>Frånvaro registrerad</h1>"

    service.client.report_absence_today = report_absence_today  # type: ignore[method-assign]
    service.client.absence_notify = absence_notify  # type: ignore[method-assign]
    await service.store.open()
    try:
        result = await service.report_absence_today("12345")

        assert submitted == ["12345"]
        assert result["status"] == "submitted"
        assert result["period"] == {"mode": "today", "date": "2026-08-27"}
        snapshot = await service.store.get_record("absence", "current")
        assert snapshot is not None
        assert snapshot["body_text"] == "Frånvaro registrerad"
    finally:
        await service.store.close()


@pytest.mark.asyncio
async def test_unknown_absence_submission_outcome_is_returned_without_retry(tmp_path: Path) -> None:
    service = VklassService(
        Settings(data_dir=tmp_path, state_key="state-key-for-tests", _env_file=None)
    )
    service.client.authenticated = True
    attempts = 0

    async def uncertain(_: str) -> dict[str, str]:
        nonlocal attempts
        attempts += 1
        raise VklassSubmissionOutcomeUnknown("check overview; do not retry automatically")

    service.client.report_absence_today = uncertain  # type: ignore[method-assign]
    await service.store.open()
    try:
        result = await service.report_absence_today("12345")

        assert attempts == 1
        assert result["status"] == "outcome_unknown"
    finally:
        await service.store.close()


@pytest.mark.asyncio
async def test_successful_absence_report_is_not_failed_by_refresh_error(tmp_path: Path) -> None:
    service = VklassService(
        Settings(data_dir=tmp_path, state_key="state-key-for-tests", _env_file=None)
    )
    service.client.authenticated = True

    async def report_absence_today(_: str) -> dict[str, str]:
        return {"mode": "today", "date": "2026-08-27"}

    async def failed_refresh() -> str:
        raise ConnectionError("temporary")

    service.client.report_absence_today = report_absence_today  # type: ignore[method-assign]
    service.client.absence_notify = failed_refresh  # type: ignore[method-assign]
    await service.store.open()
    try:
        result = await service.report_absence_today("12345")

        assert result["status"] == "submitted"
        assert "refresh_warning" in result
    finally:
        await service.store.close()


@pytest.mark.asyncio
async def test_care_schedule_sync_uses_read_only_date_batches(tmp_path: Path) -> None:
    service = VklassService(
        Settings(
            data_dir=tmp_path,
            state_key="state-key-for-tests",
            calendar_past_days=0,
            calendar_future_days=7,
            _env_file=None,
        )
    )
    requested_dates: list[str] = []

    async def care_schedule(from_date: Any) -> dict[str, Any]:
        requested_dates.append(from_date.isoformat())
        return {
            "fromDate": from_date.isoformat(),
            "untilDate": (from_date + timedelta(days=27)).isoformat(),
            "scheduleData": [
                {
                    "studentId": 123,
                    "schoolId": 456,
                    "date": from_date.isoformat(),
                    "startTime": "08:00",
                    "endTime": "16:00",
                    "isOnLeave": False,
                }
            ],
        }

    service.client.care_schedule = care_schedule  # type: ignore[method-assign]
    await service.store.open()
    try:
        records, complete = await service._sync_care_schedule([{"id": "123"}], {})

        assert complete is True
        assert len(requested_dates) == 1
        assert len(records) == 1
        assert records[0]["kind"] == "care_schedule"
        assert len(await service.store.query_records(kinds=["care_schedule"])) == 1
    finally:
        await service.store.close()


@pytest.mark.asyncio
async def test_invalid_care_response_preserves_cached_schedule(tmp_path: Path) -> None:
    service = VklassService(
        Settings(
            data_dir=tmp_path,
            state_key="state-key-for-tests",
            calendar_past_days=0,
            calendar_future_days=40,
            _env_file=None,
        )
    )
    today = datetime.now(ZoneInfo("Europe/Stockholm")).date()
    requested_dates: list[str] = []

    async def malformed_response(from_date: Any) -> dict[str, Any]:
        requested_dates.append(from_date.isoformat())
        return {
            "fromDate": from_date.isoformat(),
            "untilDate": (from_date + timedelta(days=27)).isoformat(),
            "scheduleData": [
                {
                    "studentId": 123,
                    "schoolId": 456,
                    "date": "not-a-date",
                    "isOnLeave": False,
                }
            ],
        }

    service.client.care_schedule = malformed_response  # type: ignore[method-assign]
    await service.store.open()
    try:
        await service.store.upsert_records(
            [
                {
                    "kind": "care_schedule",
                    "key": "cached",
                    "child_id": "123",
                    "title": "Omsorgsschema",
                    "start_at": f"{today.isoformat()}T08:00:00+00:00",
                }
            ]
        )
        errors: dict[str, str] = {}
        records, complete = await service._sync_care_schedule([{"id": "123"}], errors)

        assert complete is False
        assert records == []
        assert len(requested_dates) == 1
        assert any(key.startswith("care_schedule:") for key in errors)
        assert await service.store.get_record("care_schedule", "cached") is not None
    finally:
        await service.store.close()


@pytest.mark.asyncio
async def test_unknown_ward_care_response_is_not_cached(tmp_path: Path) -> None:
    service = VklassService(
        Settings(
            data_dir=tmp_path,
            state_key="state-key-for-tests",
            calendar_past_days=0,
            calendar_future_days=7,
            _env_file=None,
        )
    )

    async def unknown_ward(from_date: Any) -> dict[str, Any]:
        return {
            "fromDate": from_date.isoformat(),
            "untilDate": (from_date + timedelta(days=27)).isoformat(),
            "scheduleData": [
                {
                    "studentId": 999,
                    "schoolId": 456,
                    "date": from_date.isoformat(),
                    "startTime": None,
                    "endTime": None,
                    "isOnLeave": False,
                }
            ],
        }

    service.client.care_schedule = unknown_ward  # type: ignore[method-assign]
    await service.store.open()
    try:
        errors: dict[str, str] = {}
        records, complete = await service._sync_care_schedule([{"id": "123"}], errors)

        assert complete is False
        assert records == []
        assert "care_schedule:unknown_ward" in errors
        assert await service.store.query_records(kinds=["care_schedule"]) == []
    finally:
        await service.store.close()
