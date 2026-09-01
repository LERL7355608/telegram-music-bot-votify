from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import aiohttp


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LyricsResult:
    file_path: Path
    source: str


class LrcLibLyricsProvider:
    def __init__(self, *, user_agent: str = "telegram-music-bot/1.0"):
        self.base_url = "https://lrclib.net/api"
        self.user_agent = user_agent

    async def fetch_lrc(self, *, track: dict[str, Any], output_dir: Path) -> LyricsResult | None:
        title = _clean_name(str(track.get("title") or ""))
        artist = _clean_name(str(track.get("artist") or ""))
        album = _clean_name(str(track.get("album") or ""))
        duration = _duration_seconds(track.get("duration"))
        if not title or not artist:
            return None

        try:
            data = await self._get_exact(title=title, artist=artist, album=album, duration=duration)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            logger.info("LRCLIB exact lookup failed artist=%s title=%s", artist, title)
            data = None
        if data is None:
            try:
                data = await self._search_best(title=title, artist=artist)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                logger.info("LRCLIB search request failed artist=%s title=%s", artist, title)
                return None
        if data is None:
            logger.info("No synced lyrics found artist=%s title=%s", artist, title)
            return None

        synced = data.get("syncedLyrics")
        if not isinstance(synced, str) or not synced.strip():
            logger.info("Lyrics found without synced LRC artist=%s title=%s", artist, title)
            return None

        output_dir.mkdir(parents=True, exist_ok=True)
        file_path = output_dir / f"{_slugify(artist)} - {_slugify(title)}.lrc"
        file_path.write_text(synced.strip() + "\n", encoding="utf-8", newline="\n")
        return LyricsResult(file_path=file_path, source="lrclib")

    async def _get_exact(
        self,
        *,
        title: str,
        artist: str,
        album: str,
        duration: int | None,
    ) -> dict[str, Any] | None:
        params: dict[str, str | int] = {
            "track_name": title,
            "artist_name": artist,
        }
        if album:
            params["album_name"] = album
        if duration is not None:
            params["duration"] = duration

        url = f"{self.base_url}/get?{urlencode(params)}"
        async with aiohttp.ClientSession(headers={"User-Agent": self.user_agent}) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status == 404:
                    return None
                if response.status != 200:
                    logger.info("LRCLIB exact lookup failed status=%s", response.status)
                    return None
                return await response.json()

    async def _search_best(self, *, title: str, artist: str) -> dict[str, Any] | None:
        params = {"track_name": title, "artist_name": artist}
        url = f"{self.base_url}/search?{urlencode(params)}"
        async with aiohttp.ClientSession(headers={"User-Agent": self.user_agent}) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status != 200:
                    logger.info("LRCLIB search failed status=%s", response.status)
                    return None
                results = await response.json()

        if not isinstance(results, list):
            return None
        for item in results:
            if isinstance(item, dict) and isinstance(item.get("syncedLyrics"), str) and item["syncedLyrics"].strip():
                return item
        return None


def _clean_name(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _duration_seconds(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    parts = value.strip().split(":")
    try:
        if len(parts) == 2:
            minutes, seconds = parts
            return int(minutes) * 60 + int(seconds)
        if len(parts) == 3:
            hours, minutes, seconds = parts
            return int(hours) * 3600 + int(minutes) * 60 + int(seconds)
    except ValueError:
        return None
    return None


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._ -]+", "", value).strip()
    slug = re.sub(r"\s+", " ", slug)
    return slug[:80] or "lyrics"
