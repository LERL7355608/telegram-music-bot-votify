from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


def _get_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer") from exc


def _get_optional_int(name: str) -> int | None:
    value = os.getenv(name)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer") from exc


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_user_id: int | None
    download_path: Path
    database_path: Path
    logs_path: Path
    base_url: str
    max_downloads_per_hour: int
    file_expiry_hours: int
    http_host: str
    http_port: int
    workers: int
    provider_name: str
    playlist_audio_concurrency: int
    zip_part_max_gb: int
    min_free_disk_gb: int
    storage_backend: str
    aws_region: str
    s3_bucket: str | None
    s3_prefix: str

    @classmethod
    def from_env(cls) -> "Settings":
        download_path = Path(os.getenv("DOWNLOAD_PATH", "/tmp/downloads")).expanduser()
        database_path = Path(os.getenv("DATABASE_PATH", "storage/downloads.sqlite3")).expanduser()
        logs_path = Path(os.getenv("LOGS_PATH", "logs")).expanduser()
        base_url = os.getenv("BASE_URL", "http://localhost:8080").rstrip("/")

        return cls(
            telegram_bot_token=_get_required("TELEGRAM_BOT_TOKEN"),
            telegram_user_id=_get_optional_int("TELEGRAM_USER_ID"),
            download_path=download_path,
            database_path=database_path,
            logs_path=logs_path,
            base_url=base_url,
            max_downloads_per_hour=_get_int("MAX_DOWNLOADS_PER_HOUR", 10),
            file_expiry_hours=_get_int("FILE_EXPIRY_HOURS", 12),
            http_host=os.getenv("HTTP_HOST", "0.0.0.0"),
            http_port=_get_int("HTTP_PORT", 8080),
            workers=_get_int("WORKERS", 2),
            provider_name=os.getenv("PROVIDER", "mock"),
            playlist_audio_concurrency=max(1, _get_int("PLAYLIST_AUDIO_CONCURRENCY", 2)),
            zip_part_max_gb=max(1, _get_int("ZIP_PART_MAX_GB", 10)),
            min_free_disk_gb=max(1, _get_int("MIN_FREE_DISK_GB", 5)),
            storage_backend=os.getenv("STORAGE_BACKEND", "local"),
            aws_region=os.getenv("AWS_REGION", "us-west-1"),
            s3_bucket=os.getenv("S3_BUCKET") or None,
            s3_prefix=os.getenv("S3_PREFIX", "downloads").strip("/"),
        )

    def ensure_directories(self) -> None:
        self.download_path.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.logs_path.mkdir(parents=True, exist_ok=True)
