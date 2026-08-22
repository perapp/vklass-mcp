from pathlib import Path

import pytest

from vklass_mcp.config import Settings


def test_security_requires_state_encryption_key(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, _env_file=None)
    with pytest.raises(ValueError, match="STATE_KEY"):
        settings.validate_security()


def test_security_requires_https_for_non_local_server(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        public_base_url="http://192.0.2.10:8000",
        state_key="a separate state encryption key",
        _env_file=None,
    )
    with pytest.raises(ValueError, match="HTTPS"):
        settings.validate_security()


def test_state_key_file_and_https(tmp_path: Path) -> None:
    key = tmp_path / "state-key"
    key.write_text("a separate state encryption key")
    settings = Settings(
        data_dir=tmp_path,
        public_base_url="https://vklass.example.test",
        state_key_file=key,
        _env_file=None,
    )
    settings.validate_security()
    assert settings.resolved_state_key == "a separate state encryption key"
    assert settings.resource_server_url == "https://vklass.example.test/mcp"
