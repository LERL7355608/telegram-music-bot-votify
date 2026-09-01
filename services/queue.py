from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from providers.base import DownloadProvider
from services.database import DownloadRepository
from services.lyrics import LrcLibLyricsProvider
from services.storage import LocalStorage, S3Storage


logger = logging.getLogger(__name__)


StatusCallback = Callable[
    [
        int | None,
        int | None,
        str | None,
        str,
        str | None,
        str | None,
        str | None,
        str,
        str,
        str,
        bool,
        str | None,
    ],
    Awaitable[None],
]


@dataclass(frozen=True)
class DownloadJob:
    download_id: int
    user_id: int
    chat_id: int | None
    message_id: int | None
    inline_message_id: str | None
    track_id: str
    quality: str
    title: str
    artist: str
    album: str
    duration: str
    has_media: bool
    cover_url: str | None
    playlist_id: str | None = None


class DownloadQueue:
    def __init__(
        self,
        *,
        provider: DownloadProvider,
        repository: DownloadRepository,
        storage: LocalStorage | S3Storage,
        lyrics_provider: LrcLibLyricsProvider | None,
        download_path: Path,
        base_url: str,
        expiry_hours: int,
        workers: int = 2,
    ):
        self.provider = provider
        self.repository = repository
        self.storage = storage
        self.lyrics_provider = lyrics_provider
        self.download_path = download_path
        self.base_url = base_url.rstrip("/")
        self.expiry_hours = expiry_hours
        self.workers = workers
        self.queue: asyncio.Queue[DownloadJob] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._status_callback: StatusCallback | None = None

    def set_status_callback(self, callback: StatusCallback) -> None:
        self._status_callback = callback

    async def start(self) -> None:
        for index in range(self.workers):
            task = asyncio.create_task(self._worker(index + 1), name=f"download-worker-{index + 1}")
            self._tasks.append(task)
        logger.info("Started %s download worker(s)", self.workers)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def put(self, job: DownloadJob) -> None:
        await self.queue.put(job)

    async def _worker(self, worker_id: int) -> None:
        while True:
            job = await self.queue.get()
            try:
                await self._process_job(job, worker_id)
            except Exception:
                logger.exception("Worker %s failed job %s", worker_id, job.download_id)
            finally:
                self.queue.task_done()

    async def _process_job(self, job: DownloadJob, worker_id: int) -> None:
        logger.info(
            "Worker %s processing download_id=%s track_id=%s quality=%s",
            worker_id,
            job.download_id,
            job.track_id,
            job.quality,
        )
        await self.repository.set_downloading(job.download_id)
        if job.playlist_id:
            await self.repository.update_playlist_download_status(job.download_id, "downloading")
        await self._notify(job, "downloading", None, None, None)

        try:
            output_dir = self.download_path / str(job.user_id) / str(job.download_id)
            total_started_at = time.monotonic()
            download_started_at = time.monotonic()
            file_path = await self.provider.download(job.track_id, job.quality, output_dir)
            download_seconds = time.monotonic() - download_started_at
            upload_started_at = time.monotonic()
            stored_file = await self.storage.store(
                local_path=file_path,
                user_id=job.user_id,
                download_id=job.download_id,
                expiry_hours=self.expiry_hours,
            )
            upload_seconds = time.monotonic() - upload_started_at
            await self.repository.set_ready(
                job.download_id,
                file_path=stored_file.file_path,
                token=stored_file.token,
                expires_at=stored_file.expires_at,
            )
            if job.playlist_id:
                await self.repository.update_playlist_download_status(job.download_id, "ready")
            lyrics_started_at = time.monotonic()
            lyrics_url = await self._try_store_lyrics(job, output_dir)
            lyrics_seconds = time.monotonic() - lyrics_started_at
            await self._notify(job, "ready", stored_file.url, lyrics_url, None)
            logger.info(
                "Download ready download_id=%s file=%s total_s=%.2f download_s=%.2f upload_s=%.2f lyrics_s=%.2f",
                job.download_id,
                file_path,
                time.monotonic() - total_started_at,
                download_seconds,
                upload_seconds,
                lyrics_seconds,
            )
        except Exception as exc:
            await self.repository.set_error(job.download_id, str(exc))
            if job.playlist_id:
                await self.repository.update_playlist_download_status(job.download_id, "error")
            await self._notify(job, "error", None, None, str(exc))
            logger.exception("Download failed download_id=%s", job.download_id)

    async def _try_store_lyrics(self, job: DownloadJob, output_dir: Path) -> str | None:
        if self.lyrics_provider is None:
            return None

        track = {
            "id": job.track_id,
            "title": job.title,
            "artist": job.artist,
            "album": job.album,
            "duration": job.duration,
        }
        try:
            lyrics = await self.lyrics_provider.fetch_lrc(track=track, output_dir=output_dir)
            if lyrics is None:
                return None
            stored_lyrics = await self.storage.store(
                local_path=lyrics.file_path,
                user_id=job.user_id,
                download_id=job.download_id,
                expiry_hours=self.expiry_hours,
            )
            await self.repository.create_ready_file(
                user_id=job.user_id,
                track=track,
                quality="lrc",
                file_path=stored_lyrics.file_path,
                token=stored_lyrics.token,
                expires_at=stored_lyrics.expires_at,
            )
            logger.info("Lyrics ready download_id=%s source=%s", job.download_id, lyrics.source)
            return stored_lyrics.url
        except Exception:
            logger.exception("Lyrics lookup failed download_id=%s", job.download_id)
            return None

    async def _notify(
        self,
        job: DownloadJob,
        status: str,
        link: str | None,
        lyrics_link: str | None,
        error_message: str | None,
    ) -> None:
        if self._status_callback is None:
            return
        if job.inline_message_id is None and (job.chat_id is None or job.message_id is None):
            return
        await self._status_callback(
            job.chat_id,
            job.message_id,
            job.inline_message_id,
            status,
            link,
            lyrics_link,
            error_message,
            job.title,
            job.artist,
            job.quality,
            job.has_media,
            job.cover_url,
        )
