from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from vklass_mcp.parsers import parse_absence_form
from vklass_mcp.vklass.client import VklassClient


def _absence_html() -> str:
    payload = (
        '{"studentOptions":[{"value":"12345","additionalValues":'
        '{"AbsenceBoundaryMinDate":"2026-08-27 06:00",'
        '"AbsenceBoundaryMaxDate":"2026-08-28 20:00"}}],'
        '"quickOptionDate":"2026-08-27"}'
    )
    return f"""
    <form action="/Absence/Notify">
      <input name="__RequestVerificationToken" value="csrf-secret">
    </form>
    <script>
      enhanceServerHtml('absence-notify', 'common/views/absence/absence-notify', '{payload}')
    </script>
    """


async def _absence_form() -> dict[str, Any]:
    return parse_absence_form(_absence_html())


@pytest.mark.asyncio
async def test_report_absence_today_submits_quick_option() -> None:
    async def ignore_cookie(_: str) -> None:
        pass

    client = VklassClient(30, ignore_cookie)
    submitted: list[dict[str, str]] = []

    async def submit(data: dict[str, str]) -> None:
        submitted.append(data)

    client._absence_form_unlocked = _absence_form  # type: ignore[method-assign]
    client._submit_absence_form_unlocked = submit  # type: ignore[method-assign]

    period = await client.report_absence_today("12345")

    assert period == {"mode": "today", "date": "2026-08-27"}
    assert submitted[0]["Notify.SelectedStudentIds"] == "12345"
    assert submitted[0]["Notify.QuickOptionDate"] == "2026-08-27"
    assert submitted[0]["Notify.QuickOptionDateIsNull"] == "False"
    assert submitted[0]["__RequestVerificationToken"] == "csrf-secret"


@pytest.mark.asyncio
async def test_report_absence_period_submits_local_dates_and_times() -> None:
    async def ignore_cookie(_: str) -> None:
        pass

    client = VklassClient(30, ignore_cookie)
    submitted: list[dict[str, str]] = []

    async def submit(data: dict[str, str]) -> None:
        submitted.append(data)

    client._absence_form_unlocked = _absence_form  # type: ignore[method-assign]
    client._submit_absence_form_unlocked = submit  # type: ignore[method-assign]
    stockholm = ZoneInfo("Europe/Stockholm")

    period = await client.report_absence_period(
        "12345",
        datetime(2026, 8, 27, 9, 15, tzinfo=stockholm),
        datetime(2026, 8, 27, 11, 45, tzinfo=stockholm),
    )

    assert period["mode"] == "period"
    assert submitted[0]["Notify.QuickOptionDateIsNull"] == "True"
    assert submitted[0]["Notify.StartDate"] == "2026-08-27"
    assert submitted[0]["Notify.StartTime"] == "09:15"
    assert submitted[0]["Notify.EndDate"] == "2026-08-27"
    assert submitted[0]["Notify.EndTime"] == "11:45"


@pytest.mark.asyncio
async def test_report_absence_rejects_unknown_child() -> None:
    async def ignore_cookie(_: str) -> None:
        pass

    client = VklassClient(30, ignore_cookie)

    async def unexpected_submit(_: dict[str, str]) -> Any:
        raise AssertionError("must not submit")

    client._absence_form_unlocked = _absence_form  # type: ignore[method-assign]
    client._submit_absence_form_unlocked = unexpected_submit  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="not available"):
        await client.report_absence_today("99999")
