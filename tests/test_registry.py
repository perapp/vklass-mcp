from pathlib import Path

import pytest

from vklass_mcp.config import Settings
from vklass_mcp.registry import UserRegistry


@pytest.mark.asyncio
async def test_user_caches_are_strictly_isolated(tmp_path: Path) -> None:
    registry = UserRegistry(
        Settings(
            data_dir=tmp_path,
            state_key="state-encryption-key-for-tests",
            _env_file=None,
        )
    )
    try:
        alice = await registry.get("alice-vklass-id")
        bob = await registry.get("bob-vklass-id")
        await alice.store.upsert_children([{"id": "child-a", "name": "Alice child"}])
        await bob.store.upsert_children([{"id": "child-b", "name": "Bob child"}])

        assert [item["id"] for item in await alice.store.list_children()] == ["child-a"]
        assert [item["id"] for item in await bob.store.list_children()] == ["child-b"]
        assert alice.data_dir != bob.data_dir

        alice_dir = alice.data_dir
        await registry.delete("alice-vklass-id")
        assert not alice_dir.exists()
        recreated = await registry.get("alice-vklass-id")
        assert await recreated.store.list_children() == []
    finally:
        await registry.stop()
