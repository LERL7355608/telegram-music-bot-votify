from __future__ import annotations

import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from config import Settings
from handlers.download import enqueue_download, enqueue_download_ref
from handlers.playlist import enqueue_playlist_zip, prompt_playlist, show_playlist_lyrics_options, show_playlist_options
from handlers.search import is_allowed, show_main_menu


logger = logging.getLogger(__name__)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    query = update.callback_query
    if query is None:
        return

    await query.answer()

    if not is_allowed(update, settings):
        await query.edit_message_text("No tienes permiso para usar este bot.")
        return

    data = query.data or ""
    logger.info(
        "Callback received user_id=%s inline=%s data_prefix=%s",
        query.from_user.id if query.from_user else None,
        bool(query.inline_message_id),
        data.split(":", 1)[0] if data else "",
    )
    if data == "home":
        await show_main_menu(update, context)
        return

    if data.startswith("select:"):
        context.user_data["awaiting_playlist_url"] = False
        await _select_track(update, context, data)
        return

    if data.startswith("download:"):
        context.user_data["awaiting_playlist_url"] = False
        await enqueue_download(update, context, data)
        return

    if data.startswith("download_ref:") or data.startswith("download_refm:"):
        context.user_data["awaiting_playlist_url"] = False
        await enqueue_download_ref(update, context, data)
        return

    if data == "playlist_prompt":
        await prompt_playlist(update, context)
        return

    if data.startswith("playlist:"):
        await show_playlist_options(update, context, data)
        return

    if data.startswith("playlist_mode:"):
        await show_playlist_lyrics_options(update, context, data)
        return

    if data.startswith("playlist_zip:"):
        await enqueue_playlist_zip(update, context, data)
        return

    if data == "back:results":
        context.user_data["awaiting_playlist_url"] = False
        await _show_saved_results(update, context)
        return

    logger.warning("Unknown callback data=%s", data)
    await query.edit_message_text("Opcion no reconocida.")


async def _select_track(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    query = update.callback_query
    chat = update.effective_chat
    if query is None or chat is None:
        return

    results = context.user_data.get("search_results") or []
    try:
        index = int(data.split(":", 1)[1])
        track = results[index]
    except (ValueError, IndexError):
        await query.edit_message_text("Ese resultado ya no esta disponible. Haz la busqueda de nuevo.")
        return

    details = _format_track_details(track)
    cover_url = _cover_url(track)

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("MP3 320kbps", callback_data=f"download:{index}:mp3_320"),
                InlineKeyboardButton("FLAC", callback_data=f"download:{index}:flac"),
            ],
            [InlineKeyboardButton("Volver", callback_data="back:results")],
        ]
    )
    text = f"{details}\n\nElige calidad:"

    if cover_url:
        await _delete_message(query.message)
        try:
            await context.bot.send_photo(
                chat_id=chat.id,
                photo=cover_url,
                caption=text,
                reply_markup=keyboard,
            )
            return
        except Exception:
            logger.exception("Could not send cover image for track=%s", track.get("id"))

    await _edit_or_send_text(update, context, text, keyboard)


async def _show_saved_results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return

    results = context.user_data.get("search_results") or []
    if not results:
        await _edit_or_send_text(update, context, "No hay resultados guardados. Haz la busqueda de nuevo.", None)
        return

    buttons = []
    for index, track in enumerate(results[:5]):
        title = track.get("title", "Sin titulo")
        artist = track.get("artist", "Artista desconocido")
        duration = track.get("duration", "")
        label = f"{artist} - {title}"
        if duration:
            label = f"{label} ({duration})"
        buttons.append([InlineKeyboardButton(label[:64], callback_data=f"select:{index}")])

    await _edit_or_send_text(update, context, "Elige un resultado:", InlineKeyboardMarkup(buttons))


def _format_track_details(track: dict[str, Any]) -> str:
    title = track.get("title", "Sin titulo")
    artist = track.get("artist", "Artista desconocido")
    album = track.get("album", "")
    duration = track.get("duration", "")
    date = track.get("date") or track.get("release_date") or track.get("year")

    lines = [
        f"🎧 Track: {title}",
        f"👤 Artist: {artist}",
    ]
    if album:
        lines.append(f"💿 Album: {album}")
    if date:
        lines.append(f"📅 Date: {date}")
    if duration:
        lines.append(f"⏱ Duration: {duration}")
    return "\n".join(lines)


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


async def _edit_or_send_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
) -> None:
    query = update.callback_query
    chat = update.effective_chat
    if query is None:
        return

    message = query.message
    if isinstance(message, Message) and (message.photo or message.video or message.animation or message.document):
        await _delete_message(message)
        if chat is not None:
            await context.bot.send_message(chat_id=chat.id, text=text, reply_markup=reply_markup)
        return

    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            raise


async def _delete_message(message: Message | None) -> None:
    if message is None:
        return
    try:
        await message.delete()
    except BadRequest:
        return
