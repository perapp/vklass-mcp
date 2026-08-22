from pathlib import Path

import pytest

from vklass_mcp.config import Settings
from vklass_mcp.service import VklassService
from vklass_mcp.vklass.client import AuthenticationRequired


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
