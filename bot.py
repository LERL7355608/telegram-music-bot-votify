from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ChosenInlineResultHandler,
    CommandHandler,
    MessageHandler,
    InlineQueryHandler,
    filters,
)

from config import Settings
from handlers.callbacks import handle_callback
from handlers.inline import handle_chosen_inline_result, handle_inline_query
from handlers.playlist import handle_playlist_message
from handlers.search import start
from logging_config import configure_logging
from providers import build_provider
from services.cleanup import CleanupService
from services.database import DownloadRepository
from services.file_server import FileServer
from services.lyrics import LrcLibLyricsProvider
from services.queue import DownloadQueue
from services.rate_limit import InMemoryRateLimiter
from services.storage import build_storage
from services.track_cache import TrackCache


logger = logging.getLogger(__name__)


def format_quality(quality: str) -> str:
    labels = {
        "mp3_320": "MP3 320kbps",
        "flac": "FLAC",
    }
    return labels.get(quality, quality)


def new_search_button() -> InlineKeyboardButton:
    return InlineKeyboardButton("🔎 Nueva busqueda", callback_data="home")


async def post_init(application: Application) -> None:
    settings: Settings = application.bot_data["settings"]
    repository: DownloadRepository = application.bot_data["repository"]
    file_server: FileServer = application.bot_data["file_server"]
    download_queue: DownloadQueue = application.bot_data["download_queue"]
    cleanup_service: CleanupService = application.bot_data["cleanup_service"]

    await repository.init()

    async def notify_status(
        chat_id: int | None,
        message_id: int | None,
        inline_message_id: str | None,
        status: str,
        link: str | None,
        lyrics_link: str | None,
        error_message: str | None,
        title: str,
        artist: str,
        quality: str,
        has_media: bool,
        cover_url: str | None,
    ) -> None:
        track_label = f"{artist} - {title}".strip(" -")
        quality_label = format_quality(quality)
        reply_markup = None

        if status == "downloading":
            text = f"⏳ Descargando...\n{track_label}\nCalidad: {quality_label}"
        elif status == "ready":
            text = (
                f"✅ Listo\n"
                f"{track_label}\n"
                f"Calidad: {quality_label}\n"
                f"Link valido por {settings.file_expiry_hours} horas."
            )
            rows = []
            if link:
                rows.append([InlineKeyboardButton("⬇️ Descargar archivo", url=link)])
            if lyrics_link:
                rows.append([InlineKeyboardButton("📄 Descargar letras", url=lyrics_link)])
            rows.append([new_search_button()])
            reply_markup = InlineKeyboardMarkup(rows)
        else:
            text = (
                f"❌ Error\n"
                f"{track_label}\n"
                f"{error_message or 'No se pudo completar la descarga.'}"
            )
            reply_markup = InlineKeyboardMarkup([[new_search_button()]])

        try:
            if status == "ready" and cover_url:
                await application.bot.edit_message_media(
                    chat_id=chat_id,
                    message_id=message_id,
                    inline_message_id=inline_message_id,
                    media=InputMediaPhoto(media=cover_url, caption=text),
                    reply_markup=reply_markup,
                )
            elif has_media:
                await application.bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=message_id,
                    inline_message_id=inline_message_id,
                    caption=text,
                    reply_markup=reply_markup,
                )
            else:
                await application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    inline_message_id=inline_message_id,
                    text=text,
                    reply_markup=reply_markup,
                )
        except BadRequest as exc:
            if status == "ready" and cover_url and "Message is not modified" not in str(exc):
                logger.warning("Could not attach cover image, falling back to text: %s", exc)
                await application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    inline_message_id=inline_message_id,
                    text=text,
                    reply_markup=reply_markup,
                )
            elif "Message is not modified" not in str(exc):
                logger.warning("Could not update status message: %s", exc)

    download_queue.set_status_callback(notify_status)
    await file_server.start()
    await download_queue.start()
    await cleanup_service.start()
    logger.info("Bot initialized")


async def post_shutdown(application: Application) -> None:
    cleanup_service: CleanupService = application.bot_data["cleanup_service"]
    download_queue: DownloadQueue = application.bot_data["download_queue"]
    file_server: FileServer = application.bot_data["file_server"]

    await cleanup_service.stop()
    await download_queue.stop()
    await file_server.stop()
    logger.info("Bot shutdown complete")


def build_application(settings: Settings) -> Application:
    provider = build_provider(settings.provider_name)
    repository = DownloadRepository(settings.database_path)
    storage = build_storage(settings)
    lyrics_provider = LrcLibLyricsProvider(user_agent="telegram-music-bot/1.0")
    file_server = FileServer(repository, settings.http_host, settings.http_port, storage)
    download_queue = DownloadQueue(
        provider=provider,
        repository=repository,
        storage=storage,
        lyrics_provider=lyrics_provider,
        download_path=settings.download_path,
        base_url=settings.base_url,
        expiry_hours=settings.file_expiry_hours,
        workers=settings.workers,
    )
    cleanup_service = CleanupService(repository, storage)

    application = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.bot_data["settings"] = settings
    application.bot_data["provider"] = provider
    application.bot_data["repository"] = repository
    application.bot_data["storage"] = storage
    application.bot_data["lyrics_provider"] = lyrics_provider
    application.bot_data["file_server"] = file_server
    application.bot_data["download_queue"] = download_queue
    application.bot_data["cleanup_service"] = cleanup_service
    application.bot_data["rate_limiter"] = InMemoryRateLimiter(settings.max_downloads_per_hour)
    application.bot_data["track_cache"] = TrackCache(ttl_minutes=30)
    application.bot_data["playlist_cache"] = TrackCache(ttl_minutes=60, max_items=100)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_playlist_message))
    application.add_handler(InlineQueryHandler(handle_inline_query))
    application.add_handler(ChosenInlineResultHandler(handle_chosen_inline_result))
    application.add_handler(CallbackQueryHandler(handle_callback))
    return application


def main() -> None:
    settings = Settings.from_env()
    settings.ensure_directories()
    configure_logging(settings.logs_path)

    logger.info(
        "Starting telegram music bot provider=%s allowlist=%s users rate_limit=%s/h",
        settings.provider_name,
        len(settings.telegram_user_ids),
        settings.max_downloads_per_hour,
    )
    application = build_application(settings)
    application.run_polling(close_loop=False)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        logging.basicConfig(level=logging.ERROR)
        logging.error("%s", exc)
        raise
