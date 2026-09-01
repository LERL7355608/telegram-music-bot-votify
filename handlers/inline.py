from __future__ import annotations

import logging
import re
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle, InputMediaAudio, InputTextMessageContent, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from config import Settings
from handlers.search import is_allowed
from providers.base import DownloadProvider
from services.track_cache import TrackCache


logger = logging.getLogger(__name__)
SEARCH_THUMBNAIL_URL = "https://img.icons8.com/3d-fluency/94/search.png"


async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    inline_query = update.inline_query
    if inline_query is None:
        return

    user_id = inline_query.from_user.id if inline_query.from_user else None
    logger.info(
        "Inline query received user_id=%s chat_type=%s query=%r",
        user_id,
        inline_query.chat_type,
        inline_query.query,
    )

    if not is_allowed(update, settings):
        logger.warning("Rejected unauthorized inline query user_id=%s", user_id)
        await inline_query.answer([], cache_time=1, is_personal=True)
        return
    if inline_query.chat_type not in {"sender", "private"}:
        logger.info(
            "Rejected inline query outside private chat user_id=%s chat_type=%s",
            user_id,
            inline_query.chat_type,
        )
        await inline_query.answer([], cache_time=1, is_personal=True)
        return

    query = inline_query.query.strip()
    context.user_data["awaiting_playlist_url"] = False
    if len(query) < 2:
        help_result = InlineQueryResultArticle(
            id="help",
            title="Buscar musica",
            description="Escribe artista, cancion o album",
            input_message_content=InputTextMessageContent("Escribe artista, cancion o album."),
            thumbnail_url=SEARCH_THUMBNAIL_URL,
        )
        try:
            await inline_query.answer([help_result], cache_time=1, is_personal=True)
        except BadRequest:
            logger.exception("Inline help thumbnail rejected, retrying without thumbnail")
            await inline_query.answer(
                [
                    InlineQueryResultArticle(
                        id="help",
                        title="Buscar musica",
                        description="Escribe artista, cancion o album",
                        input_message_content=InputTextMessageContent("Escribe artista, cancion o album."),
                    )
                ],
                cache_time=1,
                is_personal=True,
            )
        return

    provider: DownloadProvider = context.application.bot_data["provider"]
    track_cache: TrackCache = context.application.bot_data["track_cache"]
    playlist_cache: TrackCache = context.application.bot_data["playlist_cache"]

    if _looks_like_playlist_url(query):
        await _answer_playlist_query(inline_query, provider, playlist_cache, query)
        return

    try:
        tracks = (await provider.search(query))[:40]
    except Exception:
        logger.exception("Inline search failed query=%s", query)
        await inline_query.answer([], cache_time=1, is_personal=True)
        return

    logger.info("Inline query results user_id=%s query=%r count=%s", user_id, query, len(tracks))

    results = []
    for track in tracks:
        ref = track_cache.add(track)
        title = str(track.get("title") or "Sin titulo")
        artist = str(track.get("artist") or "Artista desconocido")
        album = str(track.get("album") or "")
        duration = str(track.get("duration") or "")
        description_parts = [f"Artist: {artist}"]
        if album:
            description_parts.append(f"Album: {album}")
        if duration:
            description_parts.append(duration)
        results.append(
            InlineQueryResultArticle(
                id=ref,
                title=title,
                description=" | ".join(description_parts),
                input_message_content=InputTextMessageContent(_format_selected_track(track)),
                reply_markup=_quality_reply_markup(ref),
                thumbnail_url=_cover_url(track),
            )
        )

    await inline_query.answer(results, cache_time=1, is_personal=True)


async def handle_chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chosen = update.chosen_inline_result
    if chosen is None:
        return

    inline_message_id = chosen.inline_message_id
    ref = chosen.result_id
    logger.info(
        "Chosen inline result user_id=%s result_id=%s has_inline_message_id=%s",
        chosen.from_user.id if chosen.from_user else None,
        ref,
        bool(inline_message_id),
    )
    if not inline_message_id:
        return

    track_cache: TrackCache = context.application.bot_data["track_cache"]
    track = track_cache.get(ref)
    if track is None:
        logger.info("Chosen inline track expired ref=%s", ref)
        return

    preview_url = _preview_url(track)
    if not preview_url:
        logger.info("Chosen inline track has no preview_url track_id=%s", track.get("id"))
        return

    title = str(track.get("title") or "Sin titulo")
    artist = str(track.get("artist") or "Artista desconocido")
    filename = f"{_safe_audio_filename(artist)} - {_safe_audio_filename(title)}.mp3"
    try:
        await context.bot.edit_message_media(
            inline_message_id=inline_message_id,
            media=InputMediaAudio(
                media=preview_url,
                caption=_format_selected_track(track),
                duration=_duration_seconds(str(track.get("duration") or "")),
                performer=artist,
                title=title,
                filename=filename,
                thumbnail=_cover_url(track),
            ),
            reply_markup=_quality_reply_markup(ref),
        )
    except Exception:
        logger.exception("Could not convert inline result to audio preview track_id=%s", track.get("id"))


async def _answer_playlist_query(
    inline_query,
    provider: DownloadProvider,
    playlist_cache: TrackCache,
    query: str,
) -> None:
    resolve_playlist = getattr(provider, "resolve_playlist", None)
    if resolve_playlist is None:
        await inline_query.answer(
            [
                InlineQueryResultArticle(
                    id="playlist-unsupported",
                    title="Playlists no disponibles",
                    description="El provider aun no implementa resolve_playlist(url).",
                    input_message_content=InputTextMessageContent(
                        "El provider aun no implementa resolve_playlist(url)."
                    ),
                )
            ],
            cache_time=1,
            is_personal=True,
        )
        return

    try:
        playlist = await resolve_playlist(query)
    except NotImplementedError:
        await inline_query.answer(
            [
                InlineQueryResultArticle(
                    id="playlist-unsupported",
                    title="Playlists no disponibles",
                    description="El provider aun no implementa resolve_playlist(url).",
                    input_message_content=InputTextMessageContent(
                        "El provider aun no implementa resolve_playlist(url)."
                    ),
                )
            ],
            cache_time=1,
            is_personal=True,
        )
        return
    except Exception:
        logger.exception("Playlist resolve failed url=%s", query)
        await inline_query.answer(
            [
                InlineQueryResultArticle(
                    id="playlist-error",
                    title="No pude leer la playlist",
                    description="Revisa que el link sea publico y compatible.",
                    input_message_content=InputTextMessageContent("No pude leer la playlist."),
                )
            ],
            cache_time=1,
            is_personal=True,
        )
        return

    tracks = playlist.get("tracks") if isinstance(playlist, dict) else None
    if not isinstance(tracks, list) or not tracks:
        await inline_query.answer(
            [
                InlineQueryResultArticle(
                    id="playlist-empty",
                    title="Playlist sin tracks descargables",
                    description="No se encontraron tracks compatibles.",
                    input_message_content=InputTextMessageContent("Playlist sin tracks descargables."),
                )
            ],
            cache_time=1,
            is_personal=True,
        )
        return

    ref = playlist_cache.add_item(playlist)
    title = str(playlist.get("title") or "Playlist")
    total = playlist.get("track_count") or len(tracks)
    resolved = playlist.get("resolved_count") or len(tracks)
    skipped = playlist.get("skipped_count") or 0
    description = f"Tracks: {resolved}/{total}"
    if skipped:
        description = f"{description} | No encontrados: {skipped}"

    text = (
        f"📚 Playlist: {title}\n"
        f"Tracks: {resolved}/{total}\n"
        f"No encontrados: {skipped}\n\n"
        f"Elige calidad:"
    )
    reply_markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("MP3 320kbps", callback_data=f"playlist:{ref}:mp3_320"),
                InlineKeyboardButton("FLAC", callback_data=f"playlist:{ref}:flac"),
            ],
            [InlineKeyboardButton("🔎 Nueva busqueda", callback_data="home")],
        ]
    )
    result = InlineQueryResultArticle(
        id=ref,
        title=f"Playlist: {title}",
        description=description,
        input_message_content=InputTextMessageContent(text),
        reply_markup=reply_markup,
        thumbnail_url=_playlist_cover_url(playlist),
    )
    try:
        await inline_query.answer([result], cache_time=1, is_personal=True)
    except BadRequest:
        logger.exception("Playlist thumbnail rejected, retrying without thumbnail")
        await inline_query.answer(
            [
                InlineQueryResultArticle(
                    id=ref,
                    title=f"Playlist: {title}",
                    description=description,
                    input_message_content=InputTextMessageContent(text),
                    reply_markup=reply_markup,
                )
            ],
            cache_time=1,
            is_personal=True,
        )


def _looks_like_playlist_url(query: str) -> bool:
    return bool(
        re.search(r"https?://", query)
        and (
            re.search(r"(?:open\.)?spotify\.com/(?:intl-[a-z]{2}/)?playlist/[A-Za-z0-9]+", query)
            or re.search(r"(?:www\.)?deezer\.com/(?:[a-z]{2}/)?playlist/\d+", query)
        )
    )


def _playlist_cover_url(playlist: dict[str, Any]) -> str | None:
    for key in ("cover_url", "thumbnail_url", "image_url", "picture"):
        value = playlist.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    tracks = playlist.get("tracks")
    if isinstance(tracks, list):
        for track in tracks:
            if isinstance(track, dict):
                cover = _cover_url(track)
                if cover:
                    return cover
    return None


def _format_selected_track(track: dict[str, Any]) -> str:
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
    lines.append("")
    lines.append("Elige calidad:")
    return "\n".join(lines)


def _quality_reply_markup(ref: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("MP3 320kbps", callback_data=f"download_refm:{ref}:mp3_320"),
                InlineKeyboardButton("FLAC", callback_data=f"download_refm:{ref}:flac"),
            ],
            [InlineKeyboardButton("🔎 Nueva busqueda", callback_data="home")],
        ]
    )


def _preview_url(track: dict[str, Any]) -> str | None:
    for key in ("preview_url", "preview", "previewUrl", "preview_mp3"):
        value = track.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    return None


def _duration_seconds(value: str) -> int | None:
    if not value:
        return None
    parts = value.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        return None
    return None


def _safe_audio_filename(value: str) -> str:
    clean = re.sub(r'[<>:"/\\|?*]+', "_", str(value))
    clean = re.sub(r"\s+", " ", clean).strip(". ")
    return clean[:80] or "preview"


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
