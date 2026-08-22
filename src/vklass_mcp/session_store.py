"""Encrypted persistence for the Vklass bearer session cookie."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SessionStore:
    def __init__(self, path: Path, configured_key: str | None) -> None:
        self.path = path
        self.key_path = path.parent / ".state-key"
        self._fernet = Fernet(self._resolve_key(configured_key))

    def save(self, cookie: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = json.dumps(
            {"cookie": cookie, "saved_at": datetime.now(UTC).isoformat()},
            separators=(",", ":"),
        ).encode()
        encrypted = self._fernet.encrypt(payload)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encrypted)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self.path.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)

    def load(self) -> str | None:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self._fernet.decrypt(self.path.read_bytes()))
            value = payload.get("cookie")
            return str(value) if value else None
        except (InvalidToken, json.JSONDecodeError, OSError, TypeError):
            return None

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)

    def _resolve_key(self, configured_key: str | None) -> bytes:
        if configured_key:
            return base64.urlsafe_b64encode(hashlib.sha256(configured_key.encode()).digest())
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.key_path.exists():
            return self.key_path.read_bytes().strip()
        key = Fernet.generate_key()
        fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(key + b"\n")
        return key
