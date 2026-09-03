"""Prueba de humo end-to-end sin Telegram: config -> provider -> cola -> storage -> HTTP."""
import asyncio
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

WORK = Path(tempfile.mkdtemp(prefix="smoke-"))
PORT = 8099

os.environ.update({
    "TELEGRAM_BOT_TOKEN": "123456:dummy-token-for-smoke-test",
    "TELEGRAM_USER_ID": "111, 222",
    "DOWNLOAD_PATH": str(WORK / "downloads"),
    "DATABASE_PATH": str(WORK / "db.sqlite3"),
    "LOGS_PATH": str(WORK / "logs"),
    "BASE_URL": f"http://127.0.0.1:{PORT}",
    "HTTP_HOST": "127.0.0.1",
    "HTTP_PORT": str(PORT),
    "MAX_DOWNLOADS_PER_HOUR": "2",
    "FILE_EXPIRY_HOURS": "12",
    "PROVIDER": "mock",
    "STORAGE_BACKEND": "local",
})

failures = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def fake_update(user_id):
    user = types.SimpleNamespace(id=user_id) if user_id is not None else None
    return types.SimpleNamespace(effective_user=user)


async def main():
    from config import Settings
    from handlers.search import is_allowed
    from providers import build_provider
    from services.database import DownloadRepository
    from services.file_server import FileServer
    from services.queue import DownloadJob, DownloadQueue
    from services.rate_limit import InMemoryRateLimiter
    from services.storage import build_storage

    settings = Settings.from_env()
    settings.ensure_directories()

    # --- 1. allowlist ---
    check("allowlist parsea lista con coma", settings.telegram_user_ids == frozenset({111, 222}),
          str(sorted(settings.telegram_user_ids)))
    check("usuario permitido pasa", is_allowed(fake_update(111), settings) is True)
    check("usuario ajeno RECHAZADO", is_allowed(fake_update(999), settings) is False)
    check("update sin usuario RECHAZADO", is_allowed(fake_update(None), settings) is False)

    # --- 2. falla cerrado sin allowlist ---
    saved = os.environ["TELEGRAM_USER_ID"]
    os.environ["TELEGRAM_USER_ID"] = ""
    try:
        Settings.from_env()
        check("arranque sin allowlist falla", False, "no lanzo RuntimeError")
    except RuntimeError as exc:
        check("arranque sin allowlist falla", "private" in str(exc), str(exc)[:60])
    os.environ["TELEGRAM_USER_ID"] = "no-soy-numero"
    try:
        Settings.from_env()
        check("allowlist no numerica falla", False, "no lanzo RuntimeError")
    except RuntimeError:
        check("allowlist no numerica falla", True)
    os.environ["TELEGRAM_USER_ID"] = saved

    # --- 3. rate limiter ---
    limiter = InMemoryRateLimiter(settings.max_downloads_per_hour)
    check("rate limit permite hasta el tope",
          limiter.allow(111) and limiter.allow(111), "2 de 2")
    check("rate limit bloquea el excedente", limiter.allow(111) is False, "3o denegado")
    check("rate limit es por usuario", limiter.allow(222) is True)
    limiter2 = InMemoryRateLimiter(5)
    check("costo > presupuesto no consume nada",
          limiter2.allow(1, cost=9) is False and limiter2.remaining(1) == 5)

    # --- 4. flujo real: provider -> cola -> storage -> HTTP ---
    provider = build_provider(settings.provider_name)
    repository = DownloadRepository(settings.database_path)
    storage = build_storage(settings)
    file_server = FileServer(repository, settings.http_host, settings.http_port, storage)
    queue = DownloadQueue(
        provider=provider, repository=repository, storage=storage,
        lyrics_provider=None, download_path=settings.download_path,
        base_url=settings.base_url, expiry_hours=settings.file_expiry_hours, workers=2,
    )

    seen = {}

    async def on_status(chat_id, message_id, inline_message_id, status, link,
                        lyrics_link, error_message, title, artist, quality, has_media, cover_url):
        seen[status] = {"link": link, "error": error_message}

    queue.set_status_callback(on_status)
    await repository.init()
    await file_server.start()
    await queue.start()

    try:
        results = await provider.search("cochinita pibil")
        check("provider.search devuelve resultados", len(results) == 5, f"{len(results)} tracks")
        track = results[0]
        check("resultado trae campos del contrato",
              all(k in track for k in ("id", "title", "artist", "album", "duration")))

        download_id = await repository.create_pending(user_id=111, track=track, quality="mp3_320")
        await queue.put(DownloadJob(
            download_id=download_id, user_id=111, chat_id=1, message_id=1,
            inline_message_id=None, track_id=str(track["id"]), quality="mp3_320",
            title=track["title"], artist=track["artist"], album=track["album"],
            duration=track["duration"], has_media=False, cover_url=None,
        ))

        for _ in range(100):
            if "ready" in seen or "error" in seen:
                break
            await asyncio.sleep(0.2)

        check("la cola completo la descarga", "ready" in seen, seen.get("error", {}).get("error") or "")
        link = seen.get("ready", {}).get("link")
        check("genero link de descarga", bool(link), link or "")

        row = await repository.get_by_token(link.rsplit("/", 1)[-1]) if link else None
        check("SQLite quedo en estado ready", row is not None and row["status"] == "ready")

        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{PORT}/health") as resp:
                check("/health responde 200", resp.status == 200, str(await resp.json()))
            async with session.get(link) as resp:
                body = await resp.text()
                check("HTTP sirve el archivo", resp.status == 200, f"status={resp.status}")
                check("contenido correcto", "track_id=" in body, body.splitlines()[0] if body else "")
                check("Cache-Control: no-store",
                      resp.headers.get("Cache-Control") == "no-store",
                      resp.headers.get("Cache-Control", "ausente"))
                check("Content-Disposition attachment",
                      "attachment" in resp.headers.get("Content-Disposition", ""))
            async with session.get(f"http://127.0.0.1:{PORT}/download/token-inventado") as resp:
                check("token invalido da 404", resp.status == 404, f"status={resp.status}")
    finally:
        await queue.stop()
        await file_server.stop()
        shutil.rmtree(WORK, ignore_errors=True)

    print()
    if failures:
        print(f"RESULTADO: {len(failures)} FALLAS -> {failures}")
        return 1
    print("RESULTADO: TODO PASA")
    return 0


sys.exit(asyncio.run(main()))
