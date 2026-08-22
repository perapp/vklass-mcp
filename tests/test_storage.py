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
