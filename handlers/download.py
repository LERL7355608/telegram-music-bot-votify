from __future__ import annotations

import logging
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from services.database import DownloadRepository
from services.queue import DownloadJob, DownloadQueue
from services.track_cache import TrackCache


logger = logging.getLogger(__name__)


async def enqueue_download(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    query = update.callback_query
    if query is None:
        return

    try:
        _, index_value, quality = data.split(":", 2)
        index = int(index_value)
    except ValueError:
        await query.edit_message_text("Solicitud invalida.")
        return

    results = context.user_data.get("search_results") or []
    try:
        track = results[index]
    except IndexError:
        await query.edit_message_text("Ese resultado ya no esta disponible. Haz la busqueda de nuevo.")
        return

    await _enqueue_track_download(update, context, track, quality)


async def enqueue_download_ref(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    query = update.callback_query
    if query is None:
        return

    try:
        prefix, ref, quality = data.split(":", 2)
    except ValueError:
        await query.edit_message_text("Solicitud invalida.")
        return

    track_cache: TrackCache = context.application.bot_data["track_cache"]
    track = track_cache.get(ref)
    if track is None:
        await query.edit_message_text("Ese resultado expiro. Haz la busqueda de nuevo.")
        return

    await _enqueue_track_download(update, context, track, quality, force_has_media=prefix == "download_refm")


async def _enqueue_track_download(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    track: dict[str, Any],
    quality: str,
    force_has_media: bool = False,
) -> None:
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message
    if query is None or user is None:
        return

    if quality not in {"mp3_320", "flac"}:
        await query.edit_message_text("Calidad invalida.")
        return

    repository: DownloadRepository = context.application.bot_data["repository"]
    download_queue: DownloadQueue = context.application.bot_data["download_queue"]

    title = str(track.get("title", "Sin titulo"))
    artist = str(track.get("artist", "Artista desconocido"))
    album = str(track.get("album", ""))
    duration = str(track.get("duration", ""))
    cover_url = _cover_url(track)
    has_media = force_has_media or bool(message and (message.photo or message.video or message.animation or message.document))

    await _send_preview_if_available(context, chat.id if chat is not None else None, track, title, artist, cover_url)

    download_id = await repository.create_pending(user_id=user.id, track=track, quality=quality)
    job = DownloadJob(
        download_id=download_id,
        user_id=user.id,
        chat_id=chat.id if chat is not None else None,
        message_id=message.message_id if message is not None else None,
        inline_message_id=query.inline_message_id,
        track_id=str(track.get("id", "")),
        quality=quality,
        title=title,
        artist=artist,
        album=album,
        duration=duration,
        has_media=has_media,
        cover_url=cover_url,
    )
    await download_queue.put(job)

    text = f"⏳ Descargando...\n{artist} - {title}\nCalidad: {_format_quality(quality)}"
    if has_media:
        await query.edit_message_caption(caption=text)
    else:
        await query.edit_message_text(text)
    logger.info("Queued download_id=%s user_id=%s track_id=%s", download_id, user.id, track.get("id"))


def _format_quality(quality: str) -> str:
    labels = {
        "mp3_320": "MP3 320kbps",
        "flac": "FLAC",
    }
    return labels.get(quality, quality)


def _cover_url(track: dict[str, Any]) -> str | None:
    for key in (
        "cover_url",
        "thumbnail_url",
        "album_cover",
        "image_url",
        "cover_xl",
        "cover_big",
        "cover_medium",
        "cover",
        "picture_xl",
        "picture_big",
        "picture_medium",
        "picture",
    ):
        value = track.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    return None


def _preview_url(track: dict[str, Any]) -> str | None:
    for key in ("preview_url", "preview", "previewUrl", "preview_mp3"):
        value = track.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    return None


async def _send_preview_if_available(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int | None,
    track: dict[str, Any],
    title: str,
    artist: str,
    cover_url: str | None,
) -> None:
    if chat_id is None:
        return
    preview_url = _preview_url(track)
    if not preview_url:
        return
    try:
        kwargs: dict[str, Any] = {
            "chat_id": chat_id,
            "audio": preview_url,
            "title": title,
            "performer": artist,
        }
        if cover_url:
            kwargs["thumbnail"] = cover_url
        await context.bot.send_audio(**kwargs)
    except Exception:
        logger.exception("Could not send preview audio for track=%s", track.get("id"))
