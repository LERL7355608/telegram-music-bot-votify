from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from services.database import DownloadRepository
from services.storage import LocalStorage, S3Storage


logger = logging.getLogger(__name__)


class CleanupService:
    def __init__(
        self,
        repository: DownloadRepository,
        storage: LocalStorage | S3Storage | None = None,
        interval_seconds: int = 3600,
    ):
        self.repository = repository
        self.storage = storage
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="cleanup-service")
        logger.info("Cleanup service started")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def run_once(self) -> int:
        expired = await self.repository.get_expired_ready()
        removed = 0

        for row in expired:
            file_path = row["file_path"] or ""
            try:
                if self.storage is not None:
                    await self.storage.delete(file_path)
                    removed += 1
                else:
                    path = Path(file_path)
                    if path.is_file():
                        path.unlink()
                        removed += 1
                await self.repository.mark_expired(row["id"])
            except Exception:
                logger.exception("Failed to cleanup download id=%s file=%s", row["id"], file_path)

        if expired:
            logger.info("Cleanup processed %s expired download(s), removed %s file(s)", len(expired), removed)
        return removed

    async def _loop(self) -> None:
        while True:
            await self.run_once()
            await asyncio.sleep(self.interval_seconds)


async def main() -> None:
    from config import Settings
    from logging_config import configure_logging

    settings = Settings.from_env()
    settings.ensure_directories()
    configure_logging(settings.logs_path)

    repository = DownloadRepository(settings.database_path)
    await repository.init()
    from services.storage import build_storage

    service = CleanupService(repository, build_storage(settings))
    removed = await service.run_once()
    print(f"Removed {removed} expired file(s)")


if __name__ == "__main__":
    asyncio.run(main())
