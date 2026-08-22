"""SQLite cache for normalized Vklass data."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    """Small async SQLite store with a generic, extensible record model."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None
        self._db_lock = asyncio.Lock()

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS children (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                school_id TEXT,
                school_name TEXT,
                data_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS records (
                kind TEXT NOT NULL,
                key TEXT NOT NULL,
                child_id TEXT,
                title TEXT NOT NULL DEFAULT '',
                audience TEXT NOT NULL DEFAULT '',
                start_at TEXT,
                end_at TEXT,
                body_text TEXT NOT NULL DEFAULT '',
                body_html TEXT NOT NULL DEFAULT '',
                data_json TEXT NOT NULL DEFAULT '{}',
                source_updated_at TEXT,
                cached_at TEXT NOT NULL,
                PRIMARY KEY (kind, key)
            );

            CREATE INDEX IF NOT EXISTS records_kind_start
                ON records(kind, start_at);
            CREATE INDEX IF NOT EXISTS records_child_kind
                ON records(child_id, kind);
            CREATE INDEX IF NOT EXISTS records_title
                ON records(kind, title);
            """
        )
        await self._db.commit()
        with suppress(OSError):
            self.path.chmod(0o600)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("store is not open")
        return self._db

    async def set_metadata(self, key: str, value: str) -> None:
        async with self._write_transaction():
            await self.db.execute(
                """INSERT INTO metadata(key, value, updated_at) VALUES(?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value, updated_at=excluded.updated_at""",
                (key, value, utc_now()),
            )

    async def get_metadata(self, key: str) -> str | None:
        async with (
            self._db_lock,
            self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)) as cursor,
        ):
            row = await cursor.fetchone()
        return str(row["value"]) if row else None

    async def metadata(self) -> dict[str, str]:
        async with self._db_lock, self.db.execute("SELECT key, value FROM metadata") as cursor:
            rows = await cursor.fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    async def upsert_children(self, children: list[dict[str, Any]]) -> None:
        now = utc_now()
        values = [
            (
                str(child["id"]),
                str(child.get("name") or child["id"]),
                _optional_str(child.get("school_id")),
                _optional_str(child.get("school_name")),
                json.dumps(child, ensure_ascii=False, separators=(",", ":")),
                now,
            )
            for child in children
            if child.get("id")
        ]
        if not values:
            return
        child_ids = [value[0] for value in values]
        placeholders = ",".join("?" for _ in child_ids)
        async with self._write_transaction():
            await self.db.execute(
                f"DELETE FROM children WHERE id NOT IN ({placeholders})",  # noqa: S608
                child_ids,
            )
            await self.db.executemany(
                """INSERT INTO children(id, name, school_id, school_name, data_json, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     name=excluded.name,
                     school_id=excluded.school_id,
                     school_name=excluded.school_name,
                     data_json=excluded.data_json,
                     updated_at=excluded.updated_at""",
                values,
            )

    async def list_children(self) -> list[dict[str, Any]]:
        async with (
            self._db_lock,
            self.db.execute(
                """SELECT id, name, school_id, school_name, data_json, updated_at
                   FROM children ORDER BY name"""
            ) as cursor,
        ):
            rows = await cursor.fetchall()
        return [_child_row(row) for row in rows]

    async def upsert_records(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        now = utc_now()
        values = []
        for record in records:
            values.append(
                (
                    str(record["kind"]),
                    str(record["key"]),
                    _optional_str(record.get("child_id")),
                    str(record.get("title") or ""),
                    str(record.get("audience") or ""),
                    _optional_str(record.get("start_at")),
                    _optional_str(record.get("end_at")),
                    str(record.get("body_text") or ""),
                    str(record.get("body_html") or ""),
                    json.dumps(record.get("data") or {}, ensure_ascii=False, separators=(",", ":")),
                    _optional_str(record.get("source_updated_at")),
                    now,
                )
            )
        async with self._write_transaction():
            await self.db.executemany(
                """INSERT INTO records(
                     kind, key, child_id, title, audience, start_at, end_at,
                     body_text, body_html, data_json, source_updated_at, cached_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(kind,key) DO UPDATE SET
                     child_id=excluded.child_id,
                     title=excluded.title,
                     audience=excluded.audience,
                     start_at=excluded.start_at,
                     end_at=excluded.end_at,
                     body_text=excluded.body_text,
                     body_html=excluded.body_html,
                     data_json=excluded.data_json,
                     source_updated_at=excluded.source_updated_at,
                     cached_at=excluded.cached_at""",
                values,
            )

    async def delete_records(self, *, kinds: list[str], child_id: str | None = None) -> None:
        if not kinds:
            return
        placeholders = ",".join("?" for _ in kinds)
        params: list[Any] = list(kinds)
        where = f"kind IN ({placeholders})"
        if child_id is not None:
            where += " AND child_id=?"
            params.append(child_id)
        async with self._write_transaction():
            await self.db.execute(f"DELETE FROM records WHERE {where}", params)  # noqa: S608

    async def delete_record_window(
        self,
        *,
        kinds: list[str],
        child_id: str,
        start_at: str,
        end_at: str,
    ) -> None:
        """Delete a successfully refreshed authoritative time window."""

        if not kinds:
            return
        placeholders = ",".join("?" for _ in kinds)
        sql = f"""DELETE FROM records
                  WHERE kind IN ({placeholders}) AND child_id=?
                    AND start_at>=? AND start_at<?"""  # noqa: S608 - only placeholder count varies
        async with self._write_transaction():
            await self.db.execute(sql, [*kinds, child_id, start_at, end_at])

    async def get_record(self, kind: str, key: str) -> dict[str, Any] | None:
        async with (
            self._db_lock,
            self.db.execute("SELECT * FROM records WHERE kind=? AND key=?", (kind, key)) as cursor,
        ):
            row = await cursor.fetchone()
        return _record_row(row) if row else None

    async def query_records(
        self,
        *,
        kinds: list[str],
        child_id: str | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
        query: str | None = None,
        limit: int = 100,
        newest_first: bool = False,
    ) -> list[dict[str, Any]]:
        if not kinds:
            return []
        limit = max(1, min(limit, 500))
        placeholders = ",".join("?" for _ in kinds)
        where = [f"kind IN ({placeholders})"]
        params: list[Any] = list(kinds)
        if child_id:
            where.append("(child_id=? OR child_id IS NULL)")
            params.append(child_id)
        if start_at:
            where.append("COALESCE(start_at, source_updated_at, cached_at)>=?")
            params.append(start_at)
        if end_at:
            where.append("COALESCE(start_at, source_updated_at, cached_at)<=?")
            params.append(end_at)
        if query:
            where.append("(title LIKE ? OR audience LIKE ? OR body_text LIKE ?)")
            needle = f"%{query}%"
            params.extend((needle, needle, needle))
        direction = "DESC" if newest_first else "ASC"
        params.append(limit)
        sql = f"""SELECT * FROM records WHERE {" AND ".join(where)}
                  ORDER BY COALESCE(start_at, source_updated_at, cached_at) {direction}
                  LIMIT ?"""  # noqa: S608 - placeholders protect values; kinds only set count
        async with self._db_lock, self.db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [_record_row(row) for row in rows]

    async def record_counts(self) -> dict[str, int]:
        async with (
            self._db_lock,
            self.db.execute(
                "SELECT kind, COUNT(*) AS count FROM records GROUP BY kind ORDER BY kind"
            ) as cursor,
        ):
            rows = await cursor.fetchall()
        return {str(row["kind"]): int(row["count"]) for row in rows}

    @asynccontextmanager
    async def _write_transaction(self) -> AsyncIterator[None]:
        async with self._db_lock:
            try:
                yield
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _child_row(row: aiosqlite.Row) -> dict[str, Any]:
    data = json.loads(row["data_json"] or "{}")
    return {
        "id": str(row["id"]),
        "name": str(row["name"]),
        "school_id": row["school_id"],
        "school_name": row["school_name"],
        "updated_at": str(row["updated_at"]),
        "data": data,
    }


def _record_row(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "kind": str(row["kind"]),
        "key": str(row["key"]),
        "child_id": row["child_id"],
        "title": str(row["title"]),
        "audience": str(row["audience"]),
        "start_at": row["start_at"],
        "end_at": row["end_at"],
        "body_text": str(row["body_text"]),
        "body_html": str(row["body_html"]),
        "data": json.loads(row["data_json"] or "{}"),
        "source_updated_at": row["source_updated_at"],
        "cached_at": str(row["cached_at"]),
    }
