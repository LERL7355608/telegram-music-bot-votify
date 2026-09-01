from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite


ISO_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def encode_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).strftime(ISO_FORMAT)


def decode_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, ISO_FORMAT)


class DownloadRepository:
    def __init__(self, database_path: Path):
        self.database_path = database_path

    async def init(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    track_id TEXT,
                    title TEXT,
                    artist TEXT,
                    quality TEXT NOT NULL,
                    file_path TEXT,
                    token TEXT UNIQUE,
                    status TEXT NOT NULL,
                    error_message TEXT,
                    metadata TEXT,
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL,
                    expires_at TIMESTAMP
                )
                """
            )
            await db.execute("CREATE INDEX IF NOT EXISTS idx_token ON downloads(token)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_expires ON downloads(expires_at)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_user_created ON downloads(user_id, created_at)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_status ON downloads(status)")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    started_at TIMESTAMP NOT NULL,
                    last_seen_at TIMESTAMP NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS playlist_downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL DEFAULT 0,
                    playlist_id TEXT NOT NULL,
                    playlist_title TEXT,
                    track_id TEXT NOT NULL,
                    quality TEXT NOT NULL,
                    download_id INTEGER,
                    status TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL,
                    UNIQUE(user_id, playlist_id, track_id, quality)
                )
                """
            )
            await self._migrate_playlist_downloads(db)
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_playlist_downloads_lookup
                ON playlist_downloads(user_id, playlist_id, quality, status)
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_playlist_downloads_download
                ON playlist_downloads(download_id)
                """
            )
            await db.commit()

    async def register_user(self, user: Any) -> None:
        now = encode_dt(utc_now())
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                """
                INSERT INTO users (
                    user_id, username, first_name, last_name, started_at, last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_name = excluded.last_name,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    user.id,
                    getattr(user, "username", None),
                    getattr(user, "first_name", None),
                    getattr(user, "last_name", None),
                    now,
                    now,
                ),
            )
            await db.commit()

    async def is_registered_user(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.database_path) as db:
            cursor = await db.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
            return await cursor.fetchone() is not None

    async def _migrate_playlist_downloads(self, db: aiosqlite.Connection) -> None:
        cursor = await db.execute("PRAGMA table_info(playlist_downloads)")
        columns = [row[1] for row in await cursor.fetchall()]
        cursor = await db.execute("PRAGMA index_list(playlist_downloads)")
        indexes = await cursor.fetchall()
        has_user_unique = False
        for index in indexes:
            index_name = index[1]
            if not index_name.startswith("sqlite_autoindex"):
                continue
            index_cursor = await db.execute(f"PRAGMA index_info({index_name})")
            index_columns = [row[2] for row in await index_cursor.fetchall()]
            if index_columns == ["user_id", "playlist_id", "track_id", "quality"]:
                has_user_unique = True
                break

        if "user_id" in columns and has_user_unique:
            return

        await db.execute("ALTER TABLE playlist_downloads RENAME TO playlist_downloads_old")
        await db.execute(
            """
            CREATE TABLE playlist_downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL DEFAULT 0,
                playlist_id TEXT NOT NULL,
                playlist_title TEXT,
                track_id TEXT NOT NULL,
                quality TEXT NOT NULL,
                download_id INTEGER,
                status TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                UNIQUE(user_id, playlist_id, track_id, quality)
            )
            """
        )
        if "user_id" in columns:
            insert_columns = columns
            select_columns = columns
        else:
            insert_columns = ["user_id", *columns]
            select_columns = ["0 AS user_id", *columns]
        await db.execute(
            f"""
            INSERT OR IGNORE INTO playlist_downloads ({", ".join(insert_columns)})
            SELECT {", ".join(select_columns)}
            FROM playlist_downloads_old
            """
        )
        await db.execute("DROP TABLE playlist_downloads_old")

    async def create_pending(
        self,
        *,
        user_id: int,
        track: dict[str, Any],
        quality: str,
    ) -> int:
        now = encode_dt(utc_now())
        async with aiosqlite.connect(self.database_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO downloads (
                    user_id, track_id, title, artist, quality, status,
                    metadata, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    user_id,
                    track.get("id"),
                    track.get("title"),
                    track.get("artist"),
                    quality,
                    json.dumps(track),
                    now,
                    now,
                ),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def set_downloading(self, download_id: int) -> None:
        await self._update_status(download_id, "downloading")

    async def set_ready(
        self,
        download_id: int,
        *,
        file_path: Path | str,
        token: str,
        expires_at: datetime,
    ) -> None:
        now = encode_dt(utc_now())
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                """
                UPDATE downloads
                SET status = 'ready',
                    file_path = ?,
                    token = ?,
                    expires_at = ?,
                    updated_at = ?,
                    error_message = NULL
                WHERE id = ?
                """,
                (str(file_path), token, encode_dt(expires_at), now, download_id),
            )
            await db.commit()

    async def create_ready_file(
        self,
        *,
        user_id: int,
        track: dict[str, Any],
        quality: str,
        file_path: Path | str,
        token: str,
        expires_at: datetime,
    ) -> int:
        now = encode_dt(utc_now())
        async with aiosqlite.connect(self.database_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO downloads (
                    user_id, track_id, title, artist, quality, file_path, token,
                    status, metadata, created_at, updated_at, expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?)
                """,
                (
                    user_id,
                    track.get("id"),
                    track.get("title"),
                    track.get("artist"),
                    quality,
                    str(file_path),
                    token,
                    json.dumps(track),
                    now,
                    now,
                    encode_dt(expires_at),
                ),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def get_known_playlist_track_ids(self, *, user_id: int, playlist_id: str, quality: str) -> set[str]:
        async with aiosqlite.connect(self.database_path) as db:
            cursor = await db.execute(
                """
                SELECT track_id
                FROM playlist_downloads
                WHERE user_id = ?
                  AND playlist_id = ?
                  AND quality = ?
                  AND status IN ('queued', 'downloading', 'ready')
                """,
                (user_id, playlist_id, quality),
            )
            rows = await cursor.fetchall()
            return {str(row[0]) for row in rows}

    async def create_playlist_download(
        self,
        *,
        user_id: int,
        playlist_id: str,
        playlist_title: str,
        track_id: str,
        quality: str,
        download_id: int,
    ) -> None:
        now = encode_dt(utc_now())
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                """
                INSERT INTO playlist_downloads (
                    user_id, playlist_id, playlist_title, track_id, quality, download_id,
                    status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                ON CONFLICT(user_id, playlist_id, track_id, quality) DO UPDATE SET
                    download_id = excluded.download_id,
                    status = 'queued',
                    updated_at = excluded.updated_at
                """,
                (user_id, playlist_id, playlist_title, track_id, quality, download_id, now, now),
            )
            await db.commit()

    async def update_playlist_track_status(
        self,
        *,
        user_id: int,
        playlist_id: str,
        track_id: str,
        quality: str,
        status: str,
    ) -> None:
        now = encode_dt(utc_now())
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                """
                UPDATE playlist_downloads
                SET status = ?, updated_at = ?
                WHERE user_id = ?
                  AND playlist_id = ?
                  AND track_id = ?
                  AND quality = ?
                """,
                (status, now, user_id, playlist_id, track_id, quality),
            )
            await db.commit()

    async def update_playlist_download_status(self, download_id: int, status: str) -> None:
        now = encode_dt(utc_now())
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                """
                UPDATE playlist_downloads
                SET status = ?, updated_at = ?
                WHERE download_id = ?
                """,
                (status, now, download_id),
            )
            await db.commit()

    async def set_error(self, download_id: int, error_message: str) -> None:
        now = encode_dt(utc_now())
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                """
                UPDATE downloads
                SET status = 'error', error_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (error_message[:1000], now, download_id),
            )
            await db.commit()

    async def get_by_token(self, token: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM downloads WHERE token = ?", (token,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def mark_expired(self, download_id: int) -> None:
        await self._update_status(download_id, "expired")

    async def get_expired_ready(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or utc_now()
        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM downloads
                WHERE status = 'ready'
                  AND expires_at IS NOT NULL
                  AND expires_at <= ?
                """,
                (encode_dt(now),),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def _update_status(self, download_id: int, status: str) -> None:
        now = encode_dt(utc_now())
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                "UPDATE downloads SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, download_id),
            )
            await db.commit()
