from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class DownloadProvider(ABC):
    @abstractmethod
    async def search(self, query: str) -> list[dict]:
        """
        Search songs.

        Returns:
            [
                {
                    "id": "...",
                    "title": "...",
                    "artist": "...",
                    "album": "...",
                    "duration": "...",
                    "cover_url": "https://..."  # optional artwork image
                },
                ...
            ]
        """
        raise NotImplementedError

    @abstractmethod
    async def download(self, track_id: str, quality: str, output_dir: Path) -> Path:
        """
        Download a file.

        quality: "mp3_320" or "flac"
        Returns: path to the downloaded file.
        """
        raise NotImplementedError

    async def resolve_playlist(self, url: str) -> dict:
        """
        Optional: resolve a playlist URL into track metadata.

        Returns:
            {
                "id": "source_playlist_id",
                "source": "deezer|spotify|...",
                "title": "Playlist title",
                "track_count": 80,
                "resolved_count": 78,
                "skipped_count": 2,
                "tracks": [
                    {
                        "id": "downloadable_track_id",
                        "title": "...",
                        "artist": "...",
                        "album": "...",
                        "duration": "...",
                        "cover_url": "https://..."
                    }
                ]
            }
        """
        raise NotImplementedError("This provider does not support playlists")

    async def resolve_track(self, url: str) -> dict:
        """
        Optional: resolve a track URL into downloadable track metadata.

        Returns:
            {
                "id": "downloadable_track_id",
                "title": "...",
                "artist": "...",
                "album": "...",
                "duration": "...",
                "cover_url": "https://..."
            }
        """
        raise NotImplementedError("This provider does not support track URLs")
