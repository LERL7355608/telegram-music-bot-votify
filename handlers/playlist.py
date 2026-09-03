from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
import re
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from config import Settings
from handlers.search import is_allowed
from providers.base import DownloadProvider
from services.database import DownloadRepository
from services.lyrics import LrcLibLyricsProvider
from services.storage import LocalStorage, S3Storage, StoredFile
from services.rate_limit import InMemoryRateLimiter
from services.track_cache import TrackCache


logger = logging.getLogger(__name__)
LYRICS_CONCURRENCY = 4


async def prompt_playlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    context.user_data["awaiting_playlist_url"] = True
    await query.edit_message_text(
        "Pega una playlist publica de Spotify o Deezer.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Volver", callback_data="home")]]),
    )


async def handle_playlist_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    settings: Settings = context.application.bot_data["settings"]
    if not is_allowed(update, settings):
        logger.warning("Rejected unauthorized message user_id=%s", user.id)
        await message.reply_text("No tienes permiso para usar este bot.")
        return

    repository: DownloadRepository = context.application.bot_data["repository"]
    if not context.user_data.get("awaiting_playlist_url"):
        if not await repository.is_registered_user(user.id) and not context.user_data.get("intro_prompt_sent"):
            context.user_data["intro_prompt_sent"] = True
            await message.reply_text("Bienvenido. Escribe /start para empezar.")
        return

    url = (message.text or "").strip()
    logger.info("Playlist mode message user_id=%s text=%r", user.id, url[:300])
    if not _looks_like_playlist_url(url):
        if _looks_like_track_url(url):
            context.user_data["awaiting_playlist_url"] = False
            status_message = await message.reply_text("Leyendo cancion...")
            await _resolve_track_to_message(context, status_message, url)
            return
        if _looks_like_supported_short_url(url):
            context.user_data["awaiting_playlist_url"] = False
            status_message = await message.reply_text("Leyendo link...")
            await _resolve_music_url_to_message(context, status_message, url)
            return
        await message.reply_text(
            "Pega una URL valida de playlist o cancion de Spotify o Deezer.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Volver", callback_data="home")]]),
        )
        return

    context.user_data["awaiting_playlist_url"] = False
    status_message = await message.reply_text("⏳ Leyendo playlist...")
    await _resolve_playlist_to_message(context, status_message, url)


async def show_playlist_options(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    query = update.callback_query
    if query is None:
        return
    context.user_data["awaiting_playlist_url"] = False

    try:
        _, ref, quality = data.split(":", 2)
    except ValueError:
        await query.edit_message_text("Solicitud invalida.")
        return

    if quality not in {"mp3_320", "flac"}:
        await query.edit_message_text("Calidad invalida.")
        return

    playlist = _playlist_from_cache(context, ref)
    if playlist is None:
        await query.edit_message_text("Esa playlist expiro. Vuelve a pegar el link.")
        return

    title = str(playlist.get("title") or "Playlist")
    tracks = _playlist_tracks(playlist)
    total = playlist.get("track_count") or len(tracks)
    resolved = playlist.get("resolved_count") or len(tracks)
    skipped = playlist.get("skipped_count") or 0
    text = (
        f"📚 Playlist: {title}\n"
        f"Tracks encontrados: {resolved}/{total}\n"
        f"No encontrados: {skipped}\n"
        f"Calidad: {_format_quality(quality)}\n\n"
        "Elige modo:"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⬇️ Solo nuevas", callback_data=f"playlist_mode:{ref}:{quality}:new")],
            [InlineKeyboardButton("🔁 Todas de nuevo", callback_data=f"playlist_mode:{ref}:{quality}:all")],
            [InlineKeyboardButton("🔎 Nueva busqueda", callback_data="home")],
        ]
    )
    await _edit_playlist_message(update, context, text, keyboard)


async def show_playlist_lyrics_options(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    query = update.callback_query
    if query is None:
        return
    context.user_data["awaiting_playlist_url"] = False

    try:
        _, ref, quality, selection = data.split(":", 3)
    except ValueError:
        await query.edit_message_text("Solicitud invalida.")
        return

    if quality not in {"mp3_320", "flac"} or selection not in {"new", "all"}:
        await query.edit_message_text("Solicitud invalida.")
        return

    playlist = _playlist_from_cache(context, ref)
    if playlist is None:
        await query.edit_message_text("Esa playlist expiro. Vuelve a pegar el link.")
        return

    new_only = selection == "new"
    title = str(playlist.get("title") or "Playlist")
    text = (
        f"📚 Playlist: {title}\n"
        f"Calidad: {_format_quality(quality)}\n"
        f"Modo: {'Solo nuevas' if new_only else 'Todas de nuevo'}\n\n"
        "Elige letras:"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Sin letras", callback_data=f"playlist_zip:{ref}:{quality}:{selection}:audio")],
            [InlineKeyboardButton("Con letras", callback_data=f"playlist_zip:{ref}:{quality}:{selection}:lyrics")],
            [InlineKeyboardButton("Volver", callback_data=f"playlist:{ref}:{quality}")],
        ]
    )
    await _edit_playlist_message(update, context, text, keyboard)


async def enqueue_playlist_zip(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message
    if query is None or user is None:
        return
    context.user_data["awaiting_playlist_url"] = False

    try:
        _, ref, quality, selection, mode = data.split(":", 4)
    except ValueError:
        await query.edit_message_text("Solicitud invalida.")
        return

    if quality not in {"mp3_320", "flac"} or selection not in {"new", "all"} or mode not in {"audio", "lyrics"}:
        await query.edit_message_text("Solicitud invalida.")
        return

    playlist = _playlist_from_cache(context, ref)
    if playlist is None:
        await query.edit_message_text("Esa playlist expiro. Vuelve a pegar el link.")
        return

    settings: Settings = context.application.bot_data["settings"]
    rate_limiter: InMemoryRateLimiter = context.application.bot_data["rate_limiter"]
    if not rate_limiter.allow(user.id):
        logger.info("Rate limited playlist zip user_id=%s", user.id)
        await query.edit_message_text(
            f"⚠️ Limite alcanzado: {settings.max_downloads_per_hour} descargas por hora.\n"
            "Intenta de nuevo mas tarde.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Volver", callback_data="home")]]),
        )
        return

    app_data = context.application.bot_data
    if "playlist_zip_semaphore" not in app_data:
        app_data["playlist_zip_semaphore"] = asyncio.Semaphore(1)

    target = {
        "chat_id": chat.id if chat is not None else None,
        "message_id": message.message_id if message is not None else None,
        "inline_message_id": query.inline_message_id,
    }
    include_lyrics = mode == "lyrics"
    new_only = selection == "new"
    await _edit_playlist_message(
        update,
        context,
        _progress_text(
            playlist=playlist,
            quality=quality,
            include_lyrics=include_lyrics,
            new_only=new_only,
            done=0,
            total=len(_playlist_tracks(playlist)),
            downloaded=0,
            failed=0,
            skipped=0,
            lyrics_found=0,
            lyrics_missing=0,
            phase="En cola",
        ),
        None,
    )
    context.application.create_task(
        _run_playlist_zip_job(
            context=context,
            user_id=user.id,
            target=target,
            playlist=playlist,
            quality=quality,
            include_lyrics=include_lyrics,
            new_only=new_only,
        )
    )


async def _resolve_playlist_to_message(context: ContextTypes.DEFAULT_TYPE, message: Message, url: str) -> None:
    provider: DownloadProvider = context.application.bot_data["provider"]
    playlist_cache: TrackCache = context.application.bot_data["playlist_cache"]

    try:
        playlist = await _resolve_playlist_data(provider, url)
    except Exception as exc:
        logger.exception("Playlist resolve failed url=%s", url)
        await message.edit_text(f"No pude leer la playlist: {exc}")
        return

    tracks = _playlist_tracks(playlist)
    if not tracks:
        await message.edit_text("Playlist sin tracks descargables.")
        return

    ref = playlist_cache.add_item(playlist)
    await message.edit_text(
        _playlist_quality_text(playlist),
        reply_markup=_quality_keyboard(ref),
    )


async def _resolve_music_url_to_message(context: ContextTypes.DEFAULT_TYPE, message: Message, url: str) -> None:
    provider: DownloadProvider = context.application.bot_data["provider"]
    playlist_cache: TrackCache = context.application.bot_data["playlist_cache"]
    resolve_playlist = getattr(provider, "resolve_playlist", None)
    resolve_track = getattr(provider, "resolve_track", None)

    if resolve_playlist is not None:
        try:
            playlist = await _resolve_playlist_data(provider, url)
            tracks = _playlist_tracks(playlist)
            if tracks:
                ref = playlist_cache.add_item(playlist)
                await message.edit_text(
                    _playlist_quality_text(playlist),
                    reply_markup=_quality_keyboard(ref),
                )
                return
        except Exception:
            logger.debug("Generic music URL was not resolved as playlist url=%s", url, exc_info=True)

    if resolve_track is not None:
        await _resolve_track_to_message(context, message, url)
        return

    await message.edit_text(
        "No pude leer ese link.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Volver", callback_data="home")]]),
    )


async def _resolve_playlist_data(provider: DownloadProvider, url: str) -> dict[str, Any]:
    resolve_playlist = getattr(provider, "resolve_playlist", None)
    if resolve_playlist is None:
        raise RuntimeError("El provider aun no implementa resolve_playlist(url).")
    playlist = await resolve_playlist(url)
    if not isinstance(playlist, dict):
        raise RuntimeError("El provider no devolvio una playlist valida.")
    return playlist


async def _resolve_track_to_message(context: ContextTypes.DEFAULT_TYPE, message: Message, url: str) -> None:
    provider: DownloadProvider = context.application.bot_data["provider"]
    track_cache: TrackCache = context.application.bot_data["track_cache"]
    resolve_track = getattr(provider, "resolve_track", None)

    try:
        if resolve_track is not None:
            track = await resolve_track(url)
        else:
            results = await provider.search(url)
            track = results[0] if results else None
    except Exception as exc:
        logger.exception("Track URL resolve failed url=%s", url)
        await message.edit_text(
            f"No pude leer la cancion: {exc}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Volver", callback_data="home")]]),
        )
        return

    if not isinstance(track, dict) or not track.get("id"):
        await message.edit_text(
            "No encontre una cancion descargable para ese link.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Volver", callback_data="home")]]),
        )
        return

    ref = track_cache.add(track)
    text = _track_quality_text(track)
    keyboard = _track_quality_keyboard(ref, has_media=bool(_cover_url(track)))
    cover_url = _cover_url(track)
    if cover_url:
        try:
            await message.delete()
            await context.bot.send_photo(
                chat_id=message.chat_id,
                photo=cover_url,
                caption=text,
                reply_markup=keyboard,
            )
            return
        except Exception:
            logger.exception("Could not send cover image for track url=%s", url)

    await message.edit_text(text, reply_markup=_track_quality_keyboard(ref, has_media=False))


async def _run_playlist_zip_job(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    target: dict[str, int | str | None],
    playlist: dict[str, Any],
    quality: str,
    include_lyrics: bool,
    new_only: bool,
) -> None:
    settings: Settings = context.application.bot_data["settings"]
    provider: DownloadProvider = context.application.bot_data["provider"]
    repository: DownloadRepository = context.application.bot_data["repository"]
    storage: LocalStorage | S3Storage = context.application.bot_data["storage"]
    lyrics_provider: LrcLibLyricsProvider | None = context.application.bot_data.get("lyrics_provider")
    semaphore: asyncio.Semaphore = context.application.bot_data["playlist_zip_semaphore"]

    playlist_id = str(playlist.get("id") or "")
    playlist_title = str(playlist.get("title") or "Playlist")
    tracks = _playlist_tracks(playlist)
    skipped_by_provider = int(playlist.get("skipped_count") or 0)
    skipped_provider_tracks = _playlist_skipped_tracks(playlist)

    async with semaphore:
        if new_only:
            known_track_ids = await repository.get_known_playlist_track_ids(
                user_id=user_id,
                playlist_id=playlist_id,
                quality=quality,
            )
            new_tracks = [track for track in tracks if str(track.get("id")) not in known_track_ids]
            skipped_existing = len(tracks) - len(new_tracks)
        else:
            new_tracks = tracks
            skipped_existing = 0

        if not new_tracks:
            await _edit_target(
                context,
                target,
                (
                    f"✅ Playlist revisada\n"
                    f"{playlist_title}\n"
                    f"Calidad: {_format_quality(quality)}\n"
                    f"No hay canciones nuevas.\n"
                    f"Ya existian: {skipped_existing}\n"
                    f"No encontradas: {skipped_by_provider}"
                ),
                InlineKeyboardMarkup([[InlineKeyboardButton("🔎 Nueva busqueda", callback_data="home")]]),
            )
            return

        zip_track = {
            "id": playlist_id,
            "title": f"{playlist_title}.zip",
            "artist": "Playlist",
            "album": playlist_title,
        }
        zip_quality = f"playlist_{quality}_{'lyrics' if include_lyrics else 'audio'}"
        zip_download_id = await repository.create_pending(user_id=user_id, track=zip_track, quality=zip_quality)

        root_dir = settings.download_path / str(user_id) / f"playlist-{zip_download_id}"
        work_dir = root_dir / "tracks"
        safe_playlist_title = _safe_name(playlist_title)
        root_dir.mkdir(parents=True, exist_ok=True)
        work_dir.mkdir(parents=True, exist_ok=True)
        zip_part_max_bytes = settings.zip_part_max_gb * 1024 * 1024 * 1024
        min_free_disk_bytes = settings.min_free_disk_gb * 1024 * 1024 * 1024

        downloaded: list[str] = []
        failed: list[str] = []
        lyrics_found: list[str] = []
        lyrics_missing: list[str] = []
        stored_files: list[StoredFile] = []
        audio_tasks: list[asyncio.Task[dict[str, Any]]] = []
        audio_semaphore = asyncio.Semaphore(settings.playlist_audio_concurrency)
        lyrics_tasks: list[asyncio.Task[dict[str, Any]]] = []
        lyrics_semaphore = asyncio.Semaphore(LYRICS_CONCURRENCY)
        lyrics_started_at: float | None = None
        current_part_lyrics_tasks: list[asyncio.Task[dict[str, Any]]] = []
        current_part_downloaded: list[str] = []
        current_part_bytes = 0.0
        current_part_number = 0
        current_zip_file: zipfile.ZipFile | None = None
        current_zip_path: Path | None = None
        metrics = _new_metrics()
        metrics["audio_concurrency"] = float(settings.playlist_audio_concurrency)
        used_names: set[str] = set()
        last_update = 0.0
        total_started_at = time.monotonic()

        def _open_next_zip_part() -> None:
            nonlocal current_part_number, current_zip_file, current_zip_path, current_part_bytes, current_part_downloaded
            current_part_number += 1
            if current_part_number == 1:
                filename = f"{safe_playlist_title} - {_format_quality(quality)}.zip"
            else:
                filename = f"{safe_playlist_title} - {_format_quality(quality)} - parte {current_part_number:03}.zip"
            current_zip_path = root_dir / filename
            current_zip_file = zipfile.ZipFile(current_zip_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True)
            current_part_bytes = 0.0
            current_part_downloaded = []

        async def _write_completed_lyrics(tasks: list[asyncio.Task[dict[str, Any]]], zip_file: zipfile.ZipFile) -> None:
            nonlocal last_update
            if not tasks:
                return
            completed_lyrics = 0
            for task in asyncio.as_completed(tasks):
                result = await task
                completed_lyrics += 1
                metrics["lyrics_seconds"] += float(result.get("seconds") or 0.0)
                metrics["lyrics_count"] += 1
                label = str(result.get("label") or "Cancion desconocida")
                lyrics = result.get("lyrics")
                if lyrics is None:
                    lyrics_missing.append(label)
                else:
                    audio_stem = str(result.get("audio_stem") or label)
                    lrc_arcname = _unique_arcname(
                        used_names,
                        f"{safe_playlist_title}/{_safe_name(audio_stem)}.lrc",
                    )
                    zip_file.write(lyrics.file_path, lrc_arcname)
                    lyrics_found.append(label)
                    _unlink_if_file(lyrics.file_path)

                now = time.monotonic()
                if completed_lyrics == len(tasks) or completed_lyrics % 5 == 0 or now - last_update >= 15:
                    last_update = now
                    await _edit_target(
                        context,
                        target,
                        _progress_text(
                            playlist=playlist,
                            quality=quality,
                            include_lyrics=include_lyrics,
                            new_only=new_only,
                            done=len(downloaded) + len(failed),
                            total=len(new_tracks),
                            downloaded=len(downloaded),
                            failed=len(failed),
                            skipped=skipped_existing,
                            lyrics_found=len(lyrics_found),
                            lyrics_missing=len(lyrics_missing),
                            phase="Buscando letras",
                        ),
                        None,
                    )

        async def _finish_zip_part(*, final: bool) -> None:
            nonlocal current_zip_file, current_zip_path, current_part_lyrics_tasks, current_part_downloaded
            if current_zip_file is None or current_zip_path is None:
                return

            if current_part_lyrics_tasks:
                await _edit_target(
                    context,
                    target,
                    _progress_text(
                        playlist=playlist,
                        quality=quality,
                        include_lyrics=include_lyrics,
                        new_only=new_only,
                        done=len(downloaded) + len(failed),
                        total=len(new_tracks),
                        downloaded=len(downloaded),
                        failed=len(failed),
                        skipped=skipped_existing,
                        lyrics_found=len(lyrics_found),
                        lyrics_missing=len(lyrics_missing),
                        phase="Buscando letras",
                    ),
                    None,
                )
                lyrics_wait_started_at = time.monotonic()
                await _write_completed_lyrics(current_part_lyrics_tasks, current_zip_file)
                metrics["lyrics_wait_seconds"] += time.monotonic() - lyrics_wait_started_at

            current_zip_file.writestr(
                f"{safe_playlist_title}/parte-{current_part_number:03}.txt",
                _part_summary_text(
                    playlist_title=playlist_title,
                    quality=quality,
                    part_number=current_part_number,
                    zip_part_max_gb=settings.zip_part_max_gb,
                    downloaded=current_part_downloaded,
                ),
            )
            if final:
                if lyrics_started_at is not None:
                    metrics["lyrics_wall_seconds"] = time.monotonic() - lyrics_started_at
                current_zip_file.writestr(
                    f"{safe_playlist_title}/resumen.txt",
                    _summary_text(
                        playlist_title=playlist_title,
                        quality=quality,
                        include_lyrics=include_lyrics,
                        downloaded=downloaded,
                        failed=failed,
                        skipped_existing=skipped_existing,
                        skipped_by_provider=skipped_by_provider,
                        skipped_provider_tracks=skipped_provider_tracks,
                        lyrics_found=lyrics_found,
                        lyrics_missing=lyrics_missing,
                        metrics=metrics,
                    ),
                )
                if _match_log_enabled():
                    current_zip_file.writestr(
                        f"{safe_playlist_title}/match_log.csv",
                        _match_log_csv(playlist),
                    )
                if failed or lyrics_missing or skipped_by_provider:
                    current_zip_file.writestr(
                        f"{safe_playlist_title}/faltantes.txt",
                        _missing_text(
                            failed=failed,
                            lyrics_missing=lyrics_missing,
                            skipped_by_provider=skipped_by_provider,
                            skipped_provider_tracks=skipped_provider_tracks,
                        ),
                    )

            current_zip_file.close()
            current_zip_file = None
            metrics["zip_bytes"] += _file_size(current_zip_path)
            upload_started_at = time.monotonic()
            stored_file = await storage.store(
                local_path=current_zip_path,
                user_id=user_id,
                download_id=zip_download_id,
                expiry_hours=settings.file_expiry_hours,
            )
            metrics["upload_seconds"] += time.monotonic() - upload_started_at
            stored_files.append(stored_file)
            logger.info(
                "Playlist ZIP part ready download_id=%s part=%s size_mb=%.2f url_index=%s",
                zip_download_id,
                current_part_number,
                _bytes_to_mb(metrics["zip_bytes"]),
                len(stored_files),
            )
            current_part_lyrics_tasks = []

        try:
            await repository.set_downloading(zip_download_id)
            await _edit_target(
                context,
                target,
                _progress_text(
                    playlist=playlist,
                    quality=quality,
                    include_lyrics=include_lyrics,
                    new_only=new_only,
                    done=0,
                    total=len(new_tracks),
                    downloaded=0,
                    failed=0,
                    skipped=skipped_existing,
                    lyrics_found=0,
                    lyrics_missing=0,
                    phase="Creando ZIP",
                ),
                None,
            )

            _ensure_min_free_disk(root_dir, min_free_disk_bytes)
            _open_next_zip_part()
            for index, track in enumerate(new_tracks, start=1):
                track_id = str(track.get("id") or "")
                track_dir = work_dir / str(index)
                track_dir.mkdir(parents=True, exist_ok=True)
                await repository.create_playlist_download(
                    user_id=user_id,
                    playlist_id=playlist_id,
                    playlist_title=playlist_title,
                    track_id=track_id,
                    quality=quality,
                    download_id=zip_download_id,
                )
                await repository.update_playlist_track_status(
                    user_id=user_id,
                    playlist_id=playlist_id,
                    track_id=track_id,
                    quality=quality,
                    status="downloading",
                )

                audio_tasks.append(
                    asyncio.create_task(
                        _download_playlist_track(
                            provider=provider,
                            track=track,
                            index=index,
                            quality=quality,
                            output_dir=track_dir,
                            semaphore=audio_semaphore,
                        )
                    )
                )

            completed_downloads = 0
            for task in asyncio.as_completed(audio_tasks):
                result = await task
                completed_downloads += 1
                index = int(result.get("index") or completed_downloads)
                track = result["track"]
                track_id = str(result.get("track_id") or "")
                label = str(result.get("label") or _track_label(track))
                track_dir = Path(result["track_dir"])

                try:
                    error = result.get("error")
                    if error is not None:
                        raise RuntimeError(str(error))

                    file_path = Path(result["file_path"])
                    file_size = _file_size(file_path)
                    _ensure_min_free_disk(root_dir, min_free_disk_bytes)
                    if current_zip_file is None:
                        _open_next_zip_part()
                    if current_part_bytes > 0 and current_part_bytes + file_size > zip_part_max_bytes:
                        await _finish_zip_part(final=False)
                        _ensure_min_free_disk(root_dir, min_free_disk_bytes)
                        _open_next_zip_part()

                    download_seconds = float(result.get("download_seconds") or 0.0)
                    track_seconds = float(result.get("track_seconds") or download_seconds)
                    metrics["download_seconds"] += download_seconds
                    metrics["download_count"] += 1
                    metrics["track_seconds"] += track_seconds
                    metrics["audio_bytes"] += float(result.get("audio_bytes") or file_size)
                    arcname = _unique_arcname(
                        used_names,
                        f"{safe_playlist_title}/{_safe_name(file_path.name)}",
                    )
                    zip_started_at = time.monotonic()
                    current_zip_file.write(file_path, arcname)
                    metrics["zip_write_seconds"] += time.monotonic() - zip_started_at
                    current_part_bytes += file_size
                    current_part_downloaded.append(label)
                    downloaded.append(label)
                    await repository.update_playlist_track_status(
                        user_id=user_id,
                        playlist_id=playlist_id,
                        track_id=track_id,
                        quality=quality,
                        status="ready",
                    )
                    _unlink_if_file(file_path)

                    if include_lyrics and lyrics_provider is not None:
                        if lyrics_started_at is None:
                            lyrics_started_at = time.monotonic()
                        lyrics_dir = root_dir / "lyrics" / str(index)
                        lyrics_task = asyncio.create_task(
                            _fetch_playlist_lyrics(
                                lyrics_provider=lyrics_provider,
                                track=track,
                                output_dir=lyrics_dir,
                                label=label,
                                audio_stem=Path(arcname).stem,
                                semaphore=lyrics_semaphore,
                            )
                        )
                        lyrics_tasks.append(lyrics_task)
                        current_part_lyrics_tasks.append(lyrics_task)
                    logger.info(
                        "Playlist metric track download_id=%s index=%s track_id=%s download_s=%.2f track_s=%.2f",
                        zip_download_id,
                        index,
                        track_id,
                        download_seconds,
                        track_seconds,
                    )
                except Exception as exc:
                    failed.append(f"{label}: {exc}")
                    await repository.update_playlist_track_status(
                        user_id=user_id,
                        playlist_id=playlist_id,
                        track_id=track_id,
                        quality=quality,
                        status="error",
                    )
                    logger.exception("Playlist ZIP track failed playlist_id=%s track_id=%s", playlist_id, track_id)
                finally:
                    shutil.rmtree(track_dir, ignore_errors=True)

                now = time.monotonic()
                if completed_downloads == len(new_tracks) or completed_downloads % 5 == 0 or now - last_update >= 15:
                    last_update = now
                    await _edit_target(
                        context,
                        target,
                        _progress_text(
                            playlist=playlist,
                            quality=quality,
                            include_lyrics=include_lyrics,
                            new_only=new_only,
                            done=completed_downloads,
                            total=len(new_tracks),
                            downloaded=len(downloaded),
                            failed=len(failed),
                            skipped=skipped_existing,
                            lyrics_found=len(lyrics_found),
                            lyrics_missing=len(lyrics_missing),
                            phase=f"Creando ZIP parte {current_part_number}",
                        ),
                        None,
                    )

            if not downloaded:
                raise RuntimeError("No se pudo descargar ninguna cancion de la playlist.")

            await _finish_zip_part(final=True)

            metrics["total_seconds"] = time.monotonic() - total_started_at
            if not stored_files:
                raise RuntimeError("No se genero ningun ZIP de la playlist.")

            first_file = stored_files[0]
            await repository.set_ready(
                zip_download_id,
                file_path=first_file.file_path,
                token=first_file.token,
                expires_at=first_file.expires_at,
            )
            for part_index, stored_file in enumerate(stored_files[1:], start=2):
                await repository.create_ready_file(
                    user_id=user_id,
                    track={
                        "id": f"{playlist_id}:part:{part_index}",
                        "title": f"{playlist_title}.zip parte {part_index}",
                        "artist": "Playlist",
                        "album": playlist_title,
                    },
                    quality=zip_quality,
                    file_path=stored_file.file_path,
                    token=stored_file.token,
                    expires_at=stored_file.expires_at,
                )

            keyboard_rows = [
                [InlineKeyboardButton(_zip_button_label(index, len(stored_files)), url=stored_file.url)]
                for index, stored_file in enumerate(stored_files, start=1)
            ]
            keyboard_rows.append([InlineKeyboardButton("🔎 Nueva busqueda", callback_data="home")])
            final_text = (
                f"✅ Playlist lista\n"
                f"{playlist_title}\n"
                f"Calidad: {_format_quality(quality)}\n"
                f"Descargadas: {len(downloaded)}\n"
                f"Ya existian: {skipped_existing}\n"
                f"Fallidas: {len(failed)}\n"
                f"No encontradas: {skipped_by_provider}\n"
                f"Letras encontradas: {len(lyrics_found)}\n"
                f"Letras faltantes: {len(lyrics_missing)}\n"
                f"Partes ZIP: {len(stored_files)}\n"
                f"Peso ZIP total: {_format_file_size(metrics['zip_bytes'])}\n"
                f"Link valido por {settings.file_expiry_hours} horas."
                f"{_final_missing_summary(failed=failed, skipped_tracks=skipped_provider_tracks, lyrics_missing=lyrics_missing)}"
            )

            await _edit_target(
                context,
                target,
                final_text,
                InlineKeyboardMarkup(keyboard_rows),
            )
            logger.info(
                (
                    "Playlist ZIP ready download_id=%s playlist_id=%s downloaded=%s failed=%s "
                    "audio_concurrency=%s total_s=%.2f download_s=%.2f lyrics_s=%.2f lyrics_wall_s=%.2f lyrics_wait_s=%.2f zip_write_s=%.2f upload_s=%.2f "
                    "zip_mb=%.2f upload_mbps=%.2f avg_download_s=%.2f"
                ),
                zip_download_id,
                playlist_id,
                len(downloaded),
                len(failed),
                settings.playlist_audio_concurrency,
                metrics["total_seconds"],
                metrics["download_seconds"],
                metrics["lyrics_seconds"],
                metrics["lyrics_wall_seconds"],
                metrics["lyrics_wait_seconds"],
                metrics["zip_write_seconds"],
                metrics["upload_seconds"],
                _bytes_to_mb(metrics["zip_bytes"]),
                _mbps(metrics["zip_bytes"], metrics["upload_seconds"]),
                _average(metrics["download_seconds"], metrics["download_count"]),
            )
        except Exception as exc:
            await repository.set_error(zip_download_id, str(exc))
            await _edit_target(
                context,
                target,
                (
                    f"❌ Error\n"
                    f"{playlist_title}\n"
                    f"No se pudo completar el ZIP: {exc}"
                ),
                InlineKeyboardMarkup([[InlineKeyboardButton("🔎 Nueva busqueda", callback_data="home")]]),
            )
            logger.exception("Playlist ZIP failed playlist_id=%s", playlist_id)
            for stored_file in stored_files:
                try:
                    await storage.delete(stored_file.file_path)
                except Exception:
                    logger.exception("Could not delete uploaded playlist part after failure: %s", stored_file.file_path)
        finally:
            if current_zip_file is not None:
                current_zip_file.close()
            pending_audio_tasks = [task for task in audio_tasks if not task.done()]
            for task in pending_audio_tasks:
                task.cancel()
            if pending_audio_tasks:
                await asyncio.gather(*pending_audio_tasks, return_exceptions=True)
            pending_lyrics_tasks = [task for task in lyrics_tasks if not task.done()]
            for task in pending_lyrics_tasks:
                task.cancel()
            if pending_lyrics_tasks:
                await asyncio.gather(*pending_lyrics_tasks, return_exceptions=True)
            shutil.rmtree(root_dir, ignore_errors=True)


async def _download_playlist_track(
    *,
    provider: DownloadProvider,
    track: dict[str, Any],
    index: int,
    quality: str,
    output_dir: Path,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    track_id = str(track.get("id") or "")
    label = _track_label(track)
    async with semaphore:
        track_started_at = time.monotonic()
        download_started_at = time.monotonic()
        try:
            file_path = await provider.download(track_id, quality, output_dir)
        except Exception as exc:
            return {
                "index": index,
                "track": track,
                "track_id": track_id,
                "label": label,
                "track_dir": output_dir,
                "file_path": None,
                "download_seconds": time.monotonic() - download_started_at,
                "track_seconds": time.monotonic() - track_started_at,
                "audio_bytes": 0.0,
                "error": str(exc),
            }
        return {
            "index": index,
            "track": track,
            "track_id": track_id,
            "label": label,
            "track_dir": output_dir,
            "file_path": file_path,
            "download_seconds": time.monotonic() - download_started_at,
            "track_seconds": time.monotonic() - track_started_at,
            "audio_bytes": _file_size(file_path),
            "error": None,
        }


async def _fetch_playlist_lyrics(
    *,
    lyrics_provider: LrcLibLyricsProvider,
    track: dict[str, Any],
    output_dir: Path,
    label: str,
    audio_stem: str,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    async with semaphore:
        started_at = time.monotonic()
        try:
            lyrics = await lyrics_provider.fetch_lrc(track=track, output_dir=output_dir)
        except Exception as exc:
            logger.exception("Playlist ZIP lyrics lookup failed track_id=%s", track.get("id"))
            return {
                "label": label,
                "audio_stem": audio_stem,
                "lyrics": None,
                "seconds": time.monotonic() - started_at,
                "error": str(exc),
            }
        return {
            "label": label,
            "audio_stem": audio_stem,
            "lyrics": lyrics,
            "seconds": time.monotonic() - started_at,
            "error": None,
        }


def _playlist_quality_text(playlist: dict[str, Any]) -> str:
    title = str(playlist.get("title") or "Playlist")
    tracks = _playlist_tracks(playlist)
    total = playlist.get("track_count") or len(tracks)
    resolved = playlist.get("resolved_count") or len(tracks)
    skipped = playlist.get("skipped_count") or 0
    return (
        f"📚 Playlist: {title}\n"
        f"Tracks encontrados: {resolved}/{total}\n"
        f"No encontrados: {skipped}\n\n"
        "Elige calidad:"
    )


def _quality_keyboard(ref: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("MP3 320kbps", callback_data=f"playlist:{ref}:mp3_320"),
                InlineKeyboardButton("FLAC", callback_data=f"playlist:{ref}:flac"),
            ],
            [InlineKeyboardButton("🔎 Nueva busqueda", callback_data="home")],
        ]
    )


def _track_quality_text(track: dict[str, Any]) -> str:
    title = str(track.get("title") or "Sin titulo")
    artist = str(track.get("artist") or "Artista desconocido")
    album = str(track.get("album") or "")
    duration = str(track.get("duration") or "")
    date = track.get("date") or track.get("release_date") or track.get("year")
    lines = [
        f"Track: {title}",
        f"Artist: {artist}",
    ]
    if album:
        lines.append(f"Album: {album}")
    if date:
        lines.append(f"Date: {date}")
    if duration:
        lines.append(f"Duration: {duration}")
    lines.extend(["", "Elige calidad:"])
    return "\n".join(lines)


def _track_quality_keyboard(ref: str, *, has_media: bool) -> InlineKeyboardMarkup:
    prefix = "download_refm" if has_media else "download_ref"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("MP3 320kbps", callback_data=f"{prefix}:{ref}:mp3_320"),
                InlineKeyboardButton("FLAC", callback_data=f"{prefix}:{ref}:flac"),
            ],
            [InlineKeyboardButton("Volver", callback_data="home")],
        ]
    )


def _progress_text(
    *,
    playlist: dict[str, Any],
    quality: str,
    include_lyrics: bool,
    new_only: bool,
    done: int,
    total: int,
    downloaded: int,
    failed: int,
    skipped: int,
    lyrics_found: int,
    lyrics_missing: int,
    phase: str,
) -> str:
    title = str(playlist.get("title") or "Playlist")
    bar = _progress_bar(done, total)
    lines = [
        f"⏳ {phase}",
        title,
        f"Calidad: {_format_quality(quality)}",
        f"Modo: {'Solo nuevas' if new_only else 'Todas de nuevo'}",
        f"Letras: {'si' if include_lyrics else 'no'}",
        f"{bar} {done}/{total}",
        f"Descargadas: {downloaded}",
        f"Fallidas: {failed}",
        f"Saltadas: {skipped}",
    ]
    if include_lyrics:
        lines.extend(
            [
                f"Letras encontradas: {lyrics_found}",
                f"Letras faltantes: {lyrics_missing}",
            ]
        )
    return "\n".join(lines)


def _progress_bar(done: int, total: int, width: int = 12) -> str:
    if total <= 0:
        return "[" + "-" * width + "]"
    filled = min(width, max(0, round(width * done / total)))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _summary_text(
    *,
    playlist_title: str,
    quality: str,
    include_lyrics: bool,
    downloaded: list[str],
    failed: list[str],
    skipped_existing: int,
    skipped_by_provider: int,
    skipped_provider_tracks: list[str],
    lyrics_found: list[str],
    lyrics_missing: list[str],
    metrics: dict[str, float],
) -> str:
    lines = [
        f"Playlist: {playlist_title}",
        f"Calidad: {_format_quality(quality)}",
        f"Con letras: {'si' if include_lyrics else 'no'}",
        "",
        f"Descargadas: {len(downloaded)}",
        f"Ya existian: {skipped_existing}",
        f"Fallidas: {len(failed)}",
        f"No encontradas por el provider: {skipped_by_provider}",
        f"Letras encontradas: {len(lyrics_found)}",
        f"Letras faltantes: {len(lyrics_missing)}",
        "",
        "Metricas:",
        f"Concurrencia audio: {metrics['audio_concurrency']:.0f}",
        f"Tiempo descarga audio: {_format_seconds(metrics['download_seconds'])}",
        f"Promedio descarga por cancion: {_format_seconds(_average(metrics['download_seconds'], metrics['download_count']))}",
        f"Tiempo letras: {_format_seconds(metrics['lyrics_seconds'])}",
        f"Ventana letras paralelas: {_format_seconds(metrics['lyrics_wall_seconds'])}",
        f"Espera final letras: {_format_seconds(metrics['lyrics_wait_seconds'])}",
        f"Tiempo escritura ZIP: {_format_seconds(metrics['zip_write_seconds'])}",
        f"Audio descargado: {_bytes_to_mb(metrics['audio_bytes']):.2f} MB",
        "",
        "Canciones descargadas:",
        *downloaded,
    ]
    if failed:
        lines.extend(["", "Fallidas:", *failed])
    if skipped_provider_tracks:
        lines.extend(["", "No encontradas por el provider:", *skipped_provider_tracks])
    if lyrics_missing:
        lines.extend(["", "Sin letras LRC:", *lyrics_missing])
    return "\n".join(lines) + "\n"


def _part_summary_text(
    *,
    playlist_title: str,
    quality: str,
    part_number: int,
    zip_part_max_gb: int,
    downloaded: list[str],
) -> str:
    lines = [
        f"Playlist: {playlist_title}",
        f"Calidad: {_format_quality(quality)}",
        f"Parte: {part_number}",
        f"Limite por parte: {zip_part_max_gb} GB",
        f"Canciones en esta parte: {len(downloaded)}",
        "",
        "Canciones:",
        *downloaded,
    ]
    return "\n".join(lines) + "\n"


def _match_log_enabled() -> bool:
    return os.getenv("MATCH_LOG_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _match_log_csv(playlist: dict[str, Any]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "playlist_id",
            "playlist_title",
            "index",
            "match_method",
            "match_score",
            "match_reasons",
            "spotify_artist",
            "spotify_artists",
            "spotify_title",
            "spotify_album",
            "spotify_duration_seconds",
            "spotify_isrc",
            "deezer_id",
            "deezer_artist",
            "deezer_title",
            "deezer_album",
            "deezer_duration",
        ],
        lineterminator="\n",
    )
    writer.writeheader()
    playlist_id = str(playlist.get("id") or "")
    playlist_title = str(playlist.get("title") or "")
    for index, track in enumerate(playlist.get("tracks") or [], start=1):
        match = track.get("match") if isinstance(track.get("match"), dict) else {}
        writer.writerow(
            {
                "playlist_id": playlist_id,
                "playlist_title": playlist_title,
                "index": index,
                "match_method": match.get("method", ""),
                "match_score": match.get("score", ""),
                "match_reasons": match.get("reasons", ""),
                "spotify_artist": match.get("spotify_artist", ""),
                "spotify_artists": match.get("spotify_artists", ""),
                "spotify_title": match.get("spotify_title", ""),
                "spotify_album": match.get("spotify_album", ""),
                "spotify_duration_seconds": match.get("spotify_duration", ""),
                "spotify_isrc": match.get("spotify_isrc", ""),
                "deezer_id": track.get("id", ""),
                "deezer_artist": track.get("artist", ""),
                "deezer_title": track.get("title", ""),
                "deezer_album": track.get("album", ""),
                "deezer_duration": track.get("duration", ""),
            }
        )
    return output.getvalue()


def _zip_button_label(index: int, total: int) -> str:
    if total <= 1:
        return "⬇️ Descargar ZIP"
    return f"⬇️ Descargar parte {index}/{total}"


def _missing_text(
    *,
    failed: list[str],
    lyrics_missing: list[str],
    skipped_by_provider: int,
    skipped_provider_tracks: list[str],
) -> str:
    lines = []
    if skipped_provider_tracks:
        lines.extend(["Tracks no encontrados por el provider:", *skipped_provider_tracks, ""])
    elif skipped_by_provider:
        lines.extend(["Tracks no encontrados por el provider:", str(skipped_by_provider), ""])
    if failed:
        lines.extend(["Canciones fallidas:", *failed, ""])
    if lyrics_missing:
        lines.extend(["Canciones sin letras LRC:", *lyrics_missing, ""])
    return "\n".join(lines).strip() + "\n"


def _final_missing_summary(
    *,
    failed: list[str],
    skipped_tracks: list[str],
    lyrics_missing: list[str],
    limit: int = 50,
) -> str:
    total = len(failed) + len(skipped_tracks) + len(lyrics_missing)
    if total <= 0:
        return ""
    if total > limit:
        return f"\n\nFaltantes: {total}\nLista completa en faltantes.txt dentro del ZIP."

    lines = [""]
    if skipped_tracks:
        lines.append("")
        lines.append("No encontradas:")
        lines.extend(f"- {item}" for item in skipped_tracks)
    if failed:
        lines.append("")
        lines.append("Fallidas:")
        lines.extend(f"- {item}" for item in failed)
    if lyrics_missing:
        lines.append("")
        lines.append("Sin letras LRC:")
        lines.extend(f"- {item}" for item in lyrics_missing)
    text = "\n".join(lines)
    if len(text) > 2500:
        return f"\n\nFaltantes: {total}\nLista completa en faltantes.txt dentro del ZIP."
    return text


def _ensure_min_free_disk(path: Path, min_free_bytes: int) -> None:
    usage = shutil.disk_usage(path)
    if usage.free < min_free_bytes:
        raise RuntimeError(
            "Espacio insuficiente en EC2: "
            f"libre {_bytes_to_mb(float(usage.free)) / 1024:.2f} GB, "
            f"minimo requerido {_bytes_to_mb(float(min_free_bytes)) / 1024:.2f} GB"
        )


async def _edit_playlist_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
) -> None:
    query = update.callback_query
    if query is None:
        return
    await _edit_target(
        context,
        {
            "chat_id": update.effective_chat.id if update.effective_chat is not None else None,
            "message_id": update.effective_message.message_id if update.effective_message is not None else None,
            "inline_message_id": query.inline_message_id,
        },
        text,
        reply_markup,
    )


async def _edit_target(
    context: ContextTypes.DEFAULT_TYPE,
    target: dict[str, int | str | None],
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
) -> None:
    try:
        await context.bot.edit_message_text(
            chat_id=target.get("chat_id"),
            message_id=target.get("message_id"),
            inline_message_id=target.get("inline_message_id"),
            text=text,
            reply_markup=reply_markup,
        )
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            logger.warning("Could not edit playlist message: %s", exc)
    except TelegramError as exc:
        logger.warning("Could not edit playlist message due to Telegram error: %s", exc)


def _playlist_from_cache(context: ContextTypes.DEFAULT_TYPE, ref: str) -> dict[str, Any] | None:
    playlist_cache: TrackCache = context.application.bot_data["playlist_cache"]
    playlist = playlist_cache.get_item(ref)
    return playlist if isinstance(playlist, dict) else None


def _playlist_tracks(playlist: dict[str, Any]) -> list[dict[str, Any]]:
    return [track for track in playlist.get("tracks", []) if isinstance(track, dict) and track.get("id")]


def _playlist_skipped_tracks(playlist: dict[str, Any]) -> list[str]:
    values = playlist.get("skipped_tracks") or playlist.get("missing_tracks") or []
    if not isinstance(values, list):
        return []
    labels = []
    for value in values:
        if isinstance(value, str) and value.strip():
            labels.append(value.strip())
        elif isinstance(value, dict):
            labels.append(_track_label(value))
    return labels


def _looks_like_playlist_url(value: str) -> bool:
    if not value.startswith(("http://", "https://")):
        return False
    return bool(
        re.search(r"(?:open\.)?spotify\.com/(?:intl-[a-z]{2}/)?playlist/[A-Za-z0-9]+", value)
        or re.search(r"(?:www\.)?deezer\.com/(?:[a-z]{2}/)?playlist/\d+", value)
    )


def _looks_like_track_url(value: str) -> bool:
    if not value.startswith(("http://", "https://")):
        return False
    return bool(
        re.search(r"(?:open\.)?spotify\.com/(?:intl-[a-z]{2}/)?track/[A-Za-z0-9]+", value)
        or re.search(r"(?:www\.)?deezer\.com/(?:[a-z]{2}/)?track/\d+", value)
    )


def _looks_like_supported_short_url(value: str) -> bool:
    if not value.startswith(("http://", "https://")):
        return False
    return bool(re.search(r"(?:spotify\.link|deezer\.page\.link)/", value))


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


def _format_quality(quality: str) -> str:
    labels = {
        "mp3_320": "MP3 320kbps",
        "flac": "FLAC",
    }
    return labels.get(quality, quality)


def _track_label(track: dict[str, Any]) -> str:
    artist = str(track.get("artist") or "Artista desconocido")
    title = str(track.get("title") or "Sin titulo")
    return f"{artist} - {title}".strip(" -")


def _safe_name(value: str) -> str:
    clean = "".join("_" if char in '<>:"/\\|?*' else char for char in value)
    clean = " ".join(clean.split()).strip(". ")
    return clean[:120] or "archivo"


def _unique_arcname(used_names: set[str], arcname: str) -> str:
    path = Path(arcname)
    candidate = arcname
    counter = 2
    while candidate in used_names:
        candidate = str(path.with_name(f"{path.stem} ({counter}){path.suffix}"))
        counter += 1
    used_names.add(candidate)
    return candidate


def _unlink_if_file(path: Path) -> None:
    if path.is_file():
        path.unlink()


def _new_metrics() -> dict[str, float]:
    return {
        "download_seconds": 0.0,
        "download_count": 0.0,
        "audio_concurrency": 0.0,
        "track_seconds": 0.0,
        "lyrics_seconds": 0.0,
        "lyrics_wall_seconds": 0.0,
        "lyrics_wait_seconds": 0.0,
        "lyrics_count": 0.0,
        "zip_write_seconds": 0.0,
        "upload_seconds": 0.0,
        "total_seconds": 0.0,
        "audio_bytes": 0.0,
        "zip_bytes": 0.0,
    }


def _file_size(path: Path) -> float:
    try:
        return float(path.stat().st_size)
    except OSError:
        return 0.0


def _average(total: float, count: float) -> float:
    if count <= 0:
        return 0.0
    return total / count


def _bytes_to_mb(value: float) -> float:
    return value / (1024 * 1024)


def _format_file_size(value: float) -> str:
    gb = value / (1024 * 1024 * 1024)
    if gb >= 1:
        return f"{gb:.2f} GB"
    return f"{_bytes_to_mb(value):.2f} MB"


def _mbps(byte_count: float, seconds: float) -> float:
    if seconds <= 0:
        return 0.0
    return (byte_count * 8) / seconds / 1_000_000


def _format_seconds(value: float) -> str:
    return f"{value:.2f}s"
