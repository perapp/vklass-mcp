"""Application configuration and secret-file handling."""

from __future__ import annotations

from functools import cached_property
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration.

    Secrets should normally be mounted as files by Podman and referenced using the
    corresponding ``*_file`` setting. Direct values are supported for local development.
    """

    model_config = SettingsConfigDict(
        env_prefix="VKLASS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"  # noqa: S104 - container must accept published ports
    port: int = Field(default=8000, ge=1, le=65535)
    public_base_url: str = "http://127.0.0.1:8000"
    allowed_hosts: str = "localhost:*,127.0.0.1:*,vklass-mcp:*"

    data_dir: Path = Path("/data")
    database_name: str = "vklass.db"
    session_name: str = "session.json.fernet"

    organisation_id: int = 190
    sync_interval_seconds: int = Field(default=1800, ge=300)
    keepalive_interval_seconds: int = Field(default=600, ge=300)
    calendar_past_days: int = Field(default=14, ge=0, le=365)
    calendar_future_days: int = Field(default=120, ge=7, le=730)
    max_news_pages: int = Field(default=20, ge=1, le=100)
    max_news_items: int = Field(default=1000, ge=10, le=10000)
    request_timeout_seconds: int = Field(default=30, ge=5, le=120)
    authorization_code_ttl_seconds: int = Field(default=300, ge=60, le=600)
    access_token_ttl_seconds: int = Field(default=3600, ge=300, le=86400)
    refresh_token_ttl_seconds: int = Field(default=2592000, ge=3600, le=31536000)
    max_oauth_clients: int = Field(default=1000, ge=1, le=10000)
    max_pending_authorizations_per_client: int = Field(default=5, ge=1, le=20)
    max_pending_authorizations: int = Field(default=100, ge=1, le=1000)
    max_resident_user_services: int = Field(default=100, ge=1, le=1000)
    max_concurrent_bankid_flows: int = Field(default=4, ge=1, le=20)

    state_key: SecretStr | None = None
    state_key_file: Path | None = None
    log_level: str = "INFO"

    @property
    def database_path(self) -> Path:
        return self.data_dir / self.database_name

    @property
    def session_path(self) -> Path:
        return self.data_dir / self.session_name

    @property
    def legacy_state_paths(self) -> list[Path]:
        paths = [self.database_path, self.session_path]
        paths.extend(Path(f"{self.database_path}{suffix}") for suffix in ("-wal", "-shm"))
        return paths

    @cached_property
    def resolved_state_key(self) -> str | None:
        return self._secret(self.state_key, self.state_key_file)

    @property
    def allowed_host_list(self) -> list[str]:
        return [item.strip() for item in self.allowed_hosts.split(",") if item.strip()]

    @property
    def resource_server_url(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/mcp"

    def validate_security(self) -> None:
        """Require encrypted state and HTTPS outside explicit local development."""

        if not self.resolved_state_key or len(self.resolved_state_key) < 16:
            raise ValueError("VKLASS_STATE_KEY(_FILE) must contain at least 16 characters")
        parsed = urlparse(self.public_base_url)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not local:
            raise ValueError("VKLASS_PUBLIC_BASE_URL must use HTTPS outside local development")

    @staticmethod
    def _secret(value: SecretStr | None, path: Path | None) -> str | None:
        if path:
            return path.read_text(encoding="utf-8").strip()
        return value.get_secret_value() if value else None
