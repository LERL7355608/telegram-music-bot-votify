from __future__ import annotations

import base64
import logging
from pathlib import Path

from aiohttp import web

from services.database import DownloadRepository, decode_dt, utc_now
from services.storage import S3Storage


logger = logging.getLogger(__name__)


SEARCH_THUMB_JPG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRT/2wBDAQMEBAUEBQkFBQkUDQsNFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBT/wAARCACAAIADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDy+iiiv7QPsAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAp8EEt1MkUMbzSudqpGpZmPoAOtfRH7O37IOqfFiGHXvEUk+ieFm+aEoALi9HrGGBCp/tkHPYHqPuvwF8J/CXwysUtvDmhWmnFV2tcKm6eT/flOWb8Tivz/ADnjLBZXN0KS9rUW6Tsl5N66+ST87HBWxkKT5Vqz8wbD4G/ETU4/MtvA/iB4+oc6bMoP0JUZ/CsvX/hp4u8KwvNrPhfWdKgQ4M15YSxR/wDfTKB+tfr/AEjKGUqwBBGCD3r4yPiJiVK8sPG3q7/fr+RxrMJX1ifi7RX6c/Fr9kzwL8UoJJ47FPDmtEHbqOmRKgY/9NIxhX+vDf7Vfn58WfhB4i+DfiRtJ1+12q+Wtb2LJgukB+8jfiMqeRkZHIr9FyTibBZ37lN8tT+V7/J9fz8j0aOJhW0WjOJooor646gooooAKKKKACiiigAr3H9k74Gx/GPx68uqRM3hvSAs94BwJ3J/dw5/2sEn/ZU9CQa8Or9OP2PfBC+C/gTobsgW71fdqkzDv5mPL/8AIYj/AFr4ri7NZ5VlrlRdpzfKn2vu/u/Fo48XVdKnpuz2iGGO2hjhhjWKKNQiRoAFVQMAADoBXk3xu/aX8K/BKMWt6z6rr0ib4tKtGG8Ds0jHiNT7gk9ga6H43/EqP4S/DLWvEhVZLmCMR2kTdHnc7YwfYE5Pspr8pdc1y/8AEusXmq6pdSXuoXkrTT3EpyzsTkn/AOt2r8p4U4ZjnUpYnFN+yi7WW8nva/ZdevY8vC4ZVvelsfSmt/8ABQXx1eXTHTNF0PTrXOVSWOWeQD0Lb1B/BRXQ+B/+ChmppexxeL/DVpNaMwDXOjM0ckY9fLkZg/03LXx5RX6/U4UyapT9n9XS81dP773PWeFotW5T9hvBXjjRPiJ4ettb8P38Wo6dOOJIzyjd0dTyrDuDzWN8X/hRo/xj8F3WgasgRm/eWt2q5e1mA+V1/kR3BIr89v2WfjHd/Cf4mWCSXDDw/q0yWmoQE/JhjtSX2KE5z/d3DvX6f1+G55lNbhnHxdCbt8UJddOj819zXrY8WvSeGqLlfofjf4m8O33hHxDqOi6lF5F/YTvbzJ2DKcHHqD1B7gisyvqb/goD4KTRviPo3iOFAketWZjlIH3poCFJP/AHiH/Aa+Wa/ofKMeszwNLFr7S19dn+KZ79KftIKfcKKKK9c1CiiigAooooAK/Xr4VRww/C/wAHx25zbpo9msZHdRAmP0r8ha/Uv9ljxdF4x+BHhOdCPNsbUabMmclWg/djP1VVb/gVfk/iHSlLB0Kq2Umn81p+R5ePT5Is8y/4KEzzp8KdAiTIt31lTIQe4gl2j9W/KvgGv1O/ab+GM/xX+EGraTZRiXVbcrfWKf3pY8/KPdkLqPdhX5aTQyW8rxSo0cqMVZHGGUjggjsa7+AsTTq5W6EX70JO69dU/wBPkXgZJ0uXsMooor9JPRDpX7I+GpZp/DmlS3IIuHtImkz13FBn9a/K/wCA/wAL7r4t/EzSNDiiZrESCe/lCkrFbqQXJPbP3R7sK/WFVCqFUAADAA7V+I+ImJpyqYfDRfvRUm/JO1vvszxswkm4x6nyJ/wUUihPhTwbIx/0hb2dUH+yY13fqFr4Yr65/wCChvi2O98WeFvDkRBbT7WW8mwf4pmCqD7gRE/8Dr5Gr7rg6lKlklBT68z+Tk7f5nbhE1RjcKKKK+0OwKKKKACiiigAr6b/AGIvjbD4D8W3HhPWbpbfRdbYNbySnCQ3YwF56AOPlJ9VTpzXzJR0rzMyy+lmmEnhK20lv2fRr0ZnUpqrBwZ+0dfNH7Q37G1h8UNRuPEXhi5h0TxFNl7mGZT9mvG/vHHMbnuwBB7jJJrgv2bf20oLSztvDPxDujGsKCK011lLZA4CT4yc/wDTT/vrux+y9N1Oz1mxhvbC6hvrOdd8VxbyCSORfVWHBH0r+cKtDNeE8bzRvF7J7xkvyfpuj55qrhZ3/pn5h6z+yX8VtFu2gfwlcXYBws1lLHMjD1BDZH4gGuh8D/sSfEnxVeRDU7GHwzYEjfc38qu4X/ZjQlifY7fqK/SSivfqcf5pKnyxhBPvZ/k3b8zd4+q1ayPP/g18E/D3wS8OHTdFjaa6n2teahMB5ty4HU/3VGThRwM9yST2mr6kmjaTe38kU08drC87RW0ZklcKpJCKOWY44A6mqms+LtD8PX1hZ6pq9lp93qEghtILmdUedz2RScn047kDvWvX59iKtfEVfrOKbk5a3fX5/h5HBJyk+aXU/IL4leO7/wCJfjnWPEmpfLc385cRZyIkHCRj2VQB+FczX3J+1l+yaNcF5418FWeNSGZtS0mBf+Pju0sSj+Puyj73UfNnd8NkEEgjBFf1HkWZ4PM8HCeD0UUk49Y26enZ9T6WhUhUgnAKKKK+hOgKKKKACiiigAooooAK6bwd8TPFfw/l3+HfEGoaQC25oreciJz/ALUf3W/EGuZorKpSp1ouFWKkn0auhNJqzPfdP/bh+K1mm2bU7C+OMbriwjB6Yz8gUe//ANbisrxB+2H8VvEFu8B8S/2fC/UafbRQt+Dhd4/A14vRXkRyLKoS544aF/8ACv8AIxVCknflRc1TWtQ1u/e+1G/ub+9c5a5upmkkY+7MSa+3/wBk79rEeJFtPBfjS8A1cARafqszf8fXYRSE/wDLTsG/i6H5vvfClKrFWDKSCDkEdqjN8lwucYX6tWVrfC1vF+Xl3XUKtGNaPKz9oq+QP2sv2TRrgvPGvgqzxqQzNqWkwL/x8d2liUfx92Ufe6j5s7p/2Tv2sR4kW08F+NLwDVwBFp+qzN/x9dhFIT/y07Bv4uh+b731xX8+p5jwjmPZr/wGcf8AL8U/M8H95hKn9an4uEEEgjBFFfav7Z37NumWmnX3xE0B7bTJUYNqdi7iOOcsceZHnjzCTyv8XUfNnd8VV/Q+UZtQznCrFUNOjXVPqv8Agnv0qsa0eaIUUUV7RsFFFFABRRRQAUUUUAFFFFABRRRQAqsVYMpIIOQR2r7Z/Zw/bPsotAk0P4iXzRXNhAz2urupc3KKM+XJjkyYHB/i6H5vvfEtFeJm2T4XOaHsMSttmt16fqY1aUa0eWR61+0H+0Hq/wAcvEW5vMsPDlo5+w6bu6dvMkxwZCPwA4HcnyWiivQwmEo4GjHD4ePLGOy/rr3ZcYqC5Y7BRRRXWWFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQB//Z"
)


class FileServer:
    def __init__(
        self,
        repository: DownloadRepository,
        host: str,
        port: int,
        storage: object | None = None,
    ):
        self.repository = repository
        self.host = host
        self.port = port
        self.storage = storage
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/health", self.health)
        app.router.add_get("/assets/search-thumb.jpg", self.search_thumb)
        app.router.add_get("/download/{token}", self.download)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.info("File server listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def search_thumb(self, request: web.Request) -> web.Response:
        return web.Response(
            body=SEARCH_THUMB_JPG,
            content_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    async def download(self, request: web.Request) -> web.StreamResponse:
        token = request.match_info["token"]
        row = await self.repository.get_by_token(token)
        if row is None or row["status"] != "ready":
            raise web.HTTPNotFound(text="File not found")

        expires_at = decode_dt(row["expires_at"])
        if expires_at is None or expires_at <= utc_now():
            await self.repository.mark_expired(row["id"])
            raise web.HTTPGone(text="Link expired")

        raw_file_path = row["file_path"] or ""
        if raw_file_path.startswith("s3://"):
            if not isinstance(self.storage, S3Storage):
                logger.error("S3 download requested without S3 storage configured: id=%s", row["id"])
                raise web.HTTPInternalServerError(text="Storage unavailable")

            filename = _download_filename(row, raw_file_path)
            redirect_url = await self.storage.generate_download_url(
                file_path=raw_file_path,
                filename=filename,
                expires_seconds=3600,
            )
            raise web.HTTPFound(redirect_url)

        file_path = Path(raw_file_path)
        if not file_path.is_file():
            logger.warning("Ready download missing on disk: id=%s path=%s", row["id"], file_path)
            raise web.HTTPNotFound(text="File missing")

        return web.FileResponse(
            file_path,
            headers={
                "Content-Disposition": f'attachment; filename="{file_path.name}"',
                "Cache-Control": "no-store",
            },
        )


def _download_filename(row: dict, file_path: str) -> str:
    if file_path.startswith("s3://"):
        name = file_path.rsplit("/", 1)[-1]
        token = str(row.get("token") or "")
        token_prefix = f"{token}-"
        if token and name.startswith(token_prefix):
            return name[len(token_prefix):]
        return name or "download"

    path_name = Path(file_path).name
    if path_name:
        return path_name

    title = str(row.get("title") or "download").strip() or "download"
    quality = str(row.get("quality") or "").strip()
    suffix = ".zip" if "playlist" in quality else ""
    return f"{title}{suffix}"
