import asyncio
from pathlib import Path

import pytest

from vklass_mcp.storage import Store


@pytest.mark.asyncio
async def test_store_upsert_and_query(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db")
    await store.open()
    try:
        await store.upsert_children([{"id": "1", "name": "Alex"}])
        await store.upsert_records(
            [
                {
                    "kind": "weekly_letter",
                    "key": "42",
                    "child_id": "1",
                    "title": "Veckobrev",
                    "body_text": "Läxa till måndag",
                    "data": {"safe": True},
                }
            ]
        )
        records = await store.query_records(kinds=["weekly_letter"], child_id="1", query="Läxa")
        assert records[0]["key"] == "42"
        assert records[0]["data"] == {"safe": True}
        assert (await store.list_children())[0]["name"] == "Alex"

        await store.upsert_records(
            [
                {
                    "kind": "calendar",
                    "key": "1:event",
                    "child_id": "1",
                    "title": "Old event",
                    "start_at": "2026-08-10T08:00:00+00:00",
                }
            ]
        )
        await store.delete_record_window(
            kinds=["calendar", "assignment"],
            child_id="1",
            start_at="2026-08-01T00:00:00+00:00",
            end_at="2026-09-01T00:00:00+00:00",
        )
        assert await store.get_record("calendar", "1:event") is None

        await store.upsert_records(
            [
                {
                    "kind": "care_schedule",
                    "key": "old",
                    "child_id": "1",
                    "start_at": "2026-08-24T06:00:00+00:00",
                }
            ]
        )
        await store.replace_record_window(
            kinds=["care_schedule"],
            child_ids=["1"],
            start_at="2026-08-24T00:00:00+00:00",
            end_at="2026-08-25T00:00:00+00:00",
            records=[
                {
                    "kind": "care_schedule",
                    "key": "new",
                    "child_id": "1",
                    "start_at": "2026-08-24T07:00:00+00:00",
                },
                {
                    "kind": "care_schedule",
                    "key": "unrelated",
                    "child_id": "999",
                    "start_at": "2026-08-24T07:00:00+00:00",
                },
            ],
        )
        assert await store.get_record("care_schedule", "old") is None
        assert await store.get_record("care_schedule", "new") is not None
        assert await store.get_record("care_schedule", "unrelated") is None

        await store.upsert_children([{"id": "1", "name": "Alex"}, {"id": "2", "name": "Robin"}])
        await store.upsert_records(
            [
                {
                    "kind": "care_schedule",
                    "key": "removed-ward",
                    "child_id": "2",
                    "start_at": "2026-08-24T07:00:00+00:00",
                }
            ]
        )
        await store.upsert_children([{"id": "1", "name": "Alex"}])
        assert await store.get_record("care_schedule", "removed-ward") is None

        await store.upsert_children([], authoritative=True)
        assert await store.list_children() == []
        assert await store.query_records(kinds=["care_schedule"]) == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_cancelled_window_replacement_rolls_back(tmp_path: Path) -> None:
    store = Store(tmp_path / "cancelled.db")
    await store.open()
    try:
        await store.upsert_records(
            [
                {
                    "kind": "care_schedule",
                    "key": "old",
                    "child_id": "1",
                    "start_at": "2026-08-24T06:00:00+00:00",
                }
            ]
        )
        original_upsert = store._upsert_records_unlocked

        async def cancel_after_delete(records: list[dict[str, object]]) -> None:
            raise asyncio.CancelledError

        store._upsert_records_unlocked = cancel_after_delete  # type: ignore[method-assign]
        with pytest.raises(asyncio.CancelledError):
            await store.replace_record_window(
                kinds=["care_schedule"],
                child_ids=["1"],
                start_at="2026-08-24T00:00:00+00:00",
                end_at="2026-08-25T00:00:00+00:00",
                records=[],
            )
        store._upsert_records_unlocked = original_upsert  # type: ignore[method-assign]
        await store.set_metadata("after-cancellation", "committed")
        assert await store.get_record("care_schedule", "old") is not None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_concurrent_authoritative_child_updates_are_atomic(tmp_path: Path) -> None:
    store = Store(tmp_path / "concurrent.db")
    await store.open()
    try:
        first = [{"id": "a", "name": "First"}]
        second = [{"id": "b", "name": "Second"}]
        for _ in range(20):
            await asyncio.gather(store.upsert_children(first), store.upsert_children(second))
            ids = {child["id"] for child in await store.list_children()}
            assert ids in ({"a"}, {"b"})
    finally:
        await store.close()
