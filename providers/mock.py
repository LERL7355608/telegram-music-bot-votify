from __future__ import annotations

import asyncio
import re
from pathlib import Path

from providers.base import DownloadProvider


class MockProvider(DownloadProvider):
    async def search(self, query: str) -> list[dict]:
        await asyncio.sleep(0.3)
        clean_query = query.strip() or "Demo"

        return [
            {
                "id": f"mock-{index}-{_slugify(clean_query)}",
                "title": f"{clean_query} Track {index}",
                "artist": f"Mock Artist {index}",
                "album": "Mock Album",
                "duration": f"3:{index + 10}",
                "cover_url": f"https://picsum.photos/seed/{_slugify(clean_query)}-{index}/512/512",
            }
            for index in range(1, 6)
        ]

    async def download(self, track_id: str, quality: str, output_dir: Path) -> Path:
        await asyncio.sleep(2)
        output_dir.mkdir(parents=True, exist_ok=True)

        extension = "flac" if quality == "flac" else "mp3"
        file_path = output_dir / f"{_slugify(track_id)}-{quality}.{extension}"
        file_path.write_text(
            f"Mock audio file\ntrack_id={track_id}\nquality={quality}\n",
            encoding="utf-8",
        )
        return file_path


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-")
    return slug[:80] or "track"
