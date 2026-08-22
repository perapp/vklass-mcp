"""Per-user Vklass service lifecycle management."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

from vklass_mcp.config import Settings
from vklass_mcp.service import VklassService


class UserRegistry:
    """Lazily create one strictly isolated Vklass service per OAuth subject."""

    def __init__(
        self,
        settings: Settings,
        authentication_required_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.settings = settings
        self.authentication_required_callback = authentication_required_callback
        self._services: dict[str, VklassService] = {}
        self._subject_locks: dict[str, asyncio.Lock] = {}
        self._registry_lock = asyncio.Lock()

    async def get(self, subject: str) -> VklassService:
        if not subject:
            raise PermissionError("authenticated OAuth subject is required")
        lock = await self._subject_lock(subject)
        try:
            async with lock:
                return await self._get_or_create(subject)
        except Exception:
            await self._discard_unused_lock(subject, lock)
            raise

    async def adopt_authenticated_session(
        self,
        subject: str,
        display_name: str,
        cookie: str,
    ) -> VklassService:
        lock = await self._subject_lock(subject)
        try:
            async with lock:
                service = await self._get_or_create(subject)
                await service.restore_authenticated_session(cookie, display_name)
                return service
        except Exception:
            await self._discard_unused_lock(subject, lock)
            raise

    async def delete(self, subject: str) -> None:
        """Stop the user's service and erase its cookie and cache."""

        lock = await self._subject_lock(subject)
        async with lock:
            service = self._services.pop(subject, None)
            if service:
                await service.stop()
            directory = self._user_data_dir(subject)
            if directory.exists():
                await asyncio.to_thread(shutil.rmtree, directory)

    async def stop(self) -> None:
        async with self._registry_lock:
            services = list(self._services.values())
            self._services.clear()
        if services:
            await asyncio.gather(*(service.stop() for service in services), return_exceptions=True)

    async def _get_or_create(self, subject: str) -> VklassService:
        existing = self._services.get(subject)
        if existing is not None:
            return existing
        if len(self._services) >= self.settings.max_resident_user_services:
            raise RuntimeError("resident user service capacity reached")
        service = VklassService(
            self.settings,
            data_dir=self._user_data_dir(subject),
            subject=subject,
            authentication_required_callback=self.authentication_required_callback,
        )
        await service.start()
        self._services[subject] = service
        return service

    async def _subject_lock(self, subject: str) -> asyncio.Lock:
        async with self._registry_lock:
            return self._subject_locks.setdefault(subject, asyncio.Lock())

    async def _discard_unused_lock(self, subject: str, lock: asyncio.Lock) -> None:
        async with self._registry_lock:
            if subject not in self._services and self._subject_locks.get(subject) is lock:
                self._subject_locks.pop(subject, None)

    def _user_data_dir(self, subject: str) -> Path:
        digest = hashlib.sha256(subject.encode("utf-8")).hexdigest()
        return self.settings.data_dir / "users" / digest
