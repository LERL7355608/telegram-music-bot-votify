from __future__ import annotations

import asyncio
import mimetypes
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import boto3
from botocore.config import Config

from services.database import utc_now


@dataclass(frozen=True)
class StoredFile:
    file_path: str
    token: str
    url: str
    expires_at: datetime


class LocalStorage:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    async def store(
        self,
        *,
        local_path: Path,
        user_id: int,
        download_id: int,
        expiry_hours: int,
    ) -> StoredFile:
        token = secrets.token_urlsafe(32)
        expires_at = utc_now() + timedelta(hours=expiry_hours)
        return StoredFile(
            file_path=str(local_path),
            token=token,
            url=f"{self.base_url}/download/{token}",
            expires_at=expires_at,
        )

    async def delete(self, file_path: str) -> None:
        path = Path(file_path)
        if path.is_file():
            path.unlink()


class S3Storage:
    def __init__(self, *, bucket: str, prefix: str, region: str, base_url: str):
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.region = region
        self.base_url = base_url.rstrip("/")
        self.client = boto3.client(
            "s3",
            region_name=region,
            config=Config(signature_version="s3v4"),
        )

    async def store(
        self,
        *,
        local_path: Path,
        user_id: int,
        download_id: int,
        expiry_hours: int,
    ) -> StoredFile:
        token = secrets.token_urlsafe(24)
        key = self._object_key(user_id=user_id, download_id=download_id, token=token, filename=local_path.name)
        expires_at = utc_now() + timedelta(hours=expiry_hours)

        content_type = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
        await asyncio.to_thread(
            self.client.upload_file,
            str(local_path),
            self.bucket,
            key,
            ExtraArgs={
                "ContentType": content_type,
                "Metadata": {
                    "download-id": str(download_id),
                    "user-id": str(user_id),
                    "expires-at": expires_at.isoformat(),
                },
            },
        )

        if local_path.is_file():
            local_path.unlink()

        return StoredFile(
            file_path=f"s3://{self.bucket}/{key}",
            token=token,
            url=f"{self.base_url}/download/{token}",
            expires_at=expires_at,
        )

    async def generate_download_url(
        self,
        *,
        file_path: str,
        filename: str,
        expires_seconds: int = 3600,
    ) -> str:
        bucket, key = self._parse_s3_uri(file_path)
        if bucket != self.bucket:
            raise ValueError(f"Unexpected S3 bucket: {bucket}")
        return await asyncio.to_thread(
            self.client.generate_presigned_url,
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ResponseContentDisposition": _content_disposition(filename),
            },
            ExpiresIn=expires_seconds,
        )

    async def delete(self, file_path: str) -> None:
        bucket, key = self._parse_s3_uri(file_path)
        if bucket != self.bucket:
            return
        await asyncio.to_thread(self.client.delete_object, Bucket=bucket, Key=key)

    def _object_key(self, *, user_id: int, download_id: int, token: str, filename: str) -> str:
        clean_name = filename.replace("\\", "_").replace("/", "_")
        return f"{self.prefix}/{user_id}/{download_id}/{token}-{clean_name}"

    @staticmethod
    def _parse_s3_uri(value: str) -> tuple[str, str]:
        if not value.startswith("s3://"):
            raise ValueError(f"Not an S3 URI: {value}")
        without_scheme = value[5:]
        bucket, key = without_scheme.split("/", 1)
        return bucket, key


def build_storage(settings) -> LocalStorage | S3Storage:
    if settings.storage_backend == "local":
        return LocalStorage(settings.base_url)
    if settings.storage_backend == "s3":
        if not settings.s3_bucket:
            raise RuntimeError("S3_BUCKET is required when STORAGE_BACKEND=s3")
        return S3Storage(
            bucket=settings.s3_bucket,
            prefix=settings.s3_prefix,
            region=settings.aws_region,
            base_url=settings.base_url,
        )
    raise RuntimeError(f"Unknown storage backend: {settings.storage_backend}")


def _content_disposition(filename: str) -> str:
    ascii_name = filename.encode("ascii", "ignore").decode("ascii")
    ascii_name = re.sub(r'[\r\n"\\]+', "_", ascii_name).strip() or "download"
    utf8_name = quote(filename, safe="")
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{utf8_name}'
