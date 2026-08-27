from datetime import timedelta

import pytest

from vklass_mcp.mcp_server import _absence_datetime


def test_absence_datetime_uses_stockholm_and_whole_minutes() -> None:
    parsed = _absence_datetime("2026-08-27T09:15", "start")
    assert parsed.utcoffset() == timedelta(hours=2)

    with pytest.raises(ValueError, match="whole minutes"):
        _absence_datetime("2026-08-27T09:15:30+02:00", "start")


def test_absence_datetime_rejects_invalid_or_ambiguous_stockholm_time() -> None:
    with pytest.raises(ValueError, match="not a valid local"):
        _absence_datetime("2026-03-29T02:30", "start")
    with pytest.raises(ValueError, match="ambiguous"):
        _absence_datetime("2026-10-25T02:30", "start")

    explicit = _absence_datetime("2026-10-25T02:30+02:00", "start")
    assert explicit.utcoffset() == timedelta(hours=2)
