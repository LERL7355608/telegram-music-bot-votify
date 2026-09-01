# providers/custom.py
import os
import re
import asyncio
import logging
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import List, Dict, Optional

from providers.base import DownloadProvider

# Imports de deemix / deezer-py (solo se cargan cuando se usa el provider)
try:
    from deezer import Deezer
    from deezer.utils import map_track
    from deemix import generateDownloadObject, parseLink
    from deemix.types.Track import Track
    from deemix.downloader import Downloader
    from deemix.settings import DEFAULTS as DEEMIX_DEFAULTS
except ImportError as exc:
    raise ImportError(
        "Faltan dependencias de deemix. "
        "Asegúrate de tener: deemix==3.6.6 y deezer-py==1.3.7. "
        f"Error original: {exc}"
    ) from exc

# requests ya viene como dependencia de deemix
import requests


logger = logging.getLogger(__name__)


class CustomProvider(DownloadProvider):
    """
    Provider de descarga usando deemix (Deezer) vía deezer-py.
    Soporta búsqueda, descarga individual y resolución de playlists
    (Deezer nativo y Spotify ? Deezer matching).
    """

    # Mapeo de calidades del bot a códigos de bitrate de deemix
    _QUALITY_MAP = {
        "flac": "9",      # FLAC_LOSSLESS
        "mp3_320": "3",   # MP3_320
    }

    def __init__(self) -> None:
        self._arl = os.getenv("DEEZER_ARL", "").strip()
        self._dz: Optional[Deezer] = None
        self._dz_pool: list[Deezer] = []
        self._dz_queue: asyncio.Queue[Deezer] | None = None
        self._initialized = False
        # Lazy init para spotdl/spotapi
        self._spotdl_spotify = None

    def _ensure_initialized(self) -> None:
        """Lazy init: no toca la red hasta que se necesita."""
        if self._initialized:
            return

        if not self._arl:
            raise RuntimeError(
                "DEEZER_ARL no está configurado. "
                "Obtén tu ARL desde las cookies de deezer.com y agrégalo al .env"
            )

        pool_size = _env_int("PLAYLIST_AUDIO_CONCURRENCY", 2)
        pool_size = max(1, min(pool_size, 4))

        for _ in range(pool_size):
            dz = Deezer()
            dz.login_via_arl(self._arl)
            if not dz.logged_in:
                raise RuntimeError("El ARL de Deezer no es válido o expiró.")
            self._dz_pool.append(dz)

        self._dz = self._dz_pool[0]
        self._dz_queue = asyncio.Queue()
        for dz in self._dz_pool:
            self._dz_queue.put_nowait(dz)

        self._initialized = True

    def _ensure_spotify_client(self):
        """Inicializa el cliente de SpotipyFree (spotdl) para leer playlists públicas de Spotify."""
        if self._spotdl_spotify is not None:
            return

        try:
            from spotdl.utils.spotify import _init_free_spotify_client
        except ImportError as exc:
            raise ImportError(
                "spotdl no está instalado. "
                "Agrega 'spotdl' a requirements.txt para soporte de playlists de Spotify. "
                f"Error original: {exc}"
            ) from exc

        # SpotipyFree no necesita client_id/client_secret reales para playlists públicas
        # pero la firma los requiere. Usamos dummies.
        self._spotdl_spotify = _init_free_spotify_client(
            client_id="5fe01282e44241328a84e7c5cc169165",
            client_secret="da967fb6ca1b463f9e5ebc175a78aadf",
            user_auth=False,
            no_cache=True,
            headless=False,
            max_retries=3,
            use_cache_file=False,
            auth_token=None,
            cache_path=None,
        )

    # ------------------------------------------------------------------
    #  search  (ya funciona, no tocar)
    # ------------------------------------------------------------------
    async def search(self, query: str) -> List[Dict]:
        self._ensure_initialized()
        loop = asyncio.get_event_loop()

        def _search() -> List[Dict]:
            raw = self._dz.api.search_track(query, limit=40)
            data = raw.get("data", []) if isinstance(raw, dict) else []

            results_by_id = {}
            for item in data:
                result = _search_result_from_track_item(item)
                if result["id"] and result["id"] not in results_by_id:
                    results_by_id[result["id"]] = result

            try:
                album_raw = self._dz.api.search_album(query, limit=6)
                albums = album_raw.get("data", []) if isinstance(album_raw, dict) else []
                for album_index, album_item in enumerate(albums):
                    album_id = album_item.get("id")
                    if not album_id:
                        continue
                    tracks_raw = self._dz.api.get_album_tracks(album_id, limit=100)
                    tracks = tracks_raw.get("data", []) if isinstance(tracks_raw, dict) else []
                    for item in tracks:
                        result = _search_result_from_track_item(item, album_item=album_item)
                        result["_album_search_boost"] = max(0, 70 - (album_index * 10))
                        if not result["id"] or result["id"] in results_by_id:
                            continue
                        if _search_result_score(query, result) >= 35:
                            results_by_id[result["id"]] = result
            except Exception as exc:
                logger.debug("Album-assisted search failed for query=%r: %s", query, exc)

            results = list(results_by_id.values())
            results.sort(key=lambda item: _search_result_score(query, item), reverse=True)
            for result in results:
                result.pop("_album_search_boost", None)
            return results[:40]

        return await loop.run_in_executor(None, _search)

    # ------------------------------------------------------------------
    #  resolve_playlist  (nuevo)
    # ------------------------------------------------------------------
    async def resolve_playlist(self, url: str) -> Dict:
        """
        Resuelve una playlist de Deezer o Spotify y devuelve sus tracks
        mapeados a IDs de Deezer (compatibles con download()).
        """
        self._ensure_initialized()
        loop = asyncio.get_event_loop()
        url = await loop.run_in_executor(None, _expand_shared_url, url)

        if re.search(r"(?:www\.)?deezer\.com/(?:[a-z]{2}/)?playlist/\d+", url):
            return await loop.run_in_executor(None, self._resolve_deezer_playlist, url)
        elif re.search(r"(?:open\.)?spotify\.com/(?:intl-[a-z]{2}/)?playlist/[A-Za-z0-9]+", url):
            return await loop.run_in_executor(None, self._resolve_spotify_playlist, url)
        else:
            raise ValueError(
                "URL de playlist no soportada. "
                "Usa deezer.com/playlist o spotify.com/playlist"
            )

    async def resolve_track(self, url: str) -> Dict:
        """
        Resuelve un link individual de Deezer o Spotify a metadata descargable.
        Spotify se matchea contra Deezer para que download() siga usando IDs Deezer.
        """
        self._ensure_initialized()
        loop = asyncio.get_event_loop()
        url = await loop.run_in_executor(None, _expand_shared_url, url)

        if re.search(r"(?:www\.)?deezer\.com/(?:[a-z]{2}/)?track/\d+", url):
            return await loop.run_in_executor(None, self._resolve_deezer_track, url)
        if re.search(r"(?:open\.)?spotify\.com/(?:intl-[a-z]{2}/)?track/[A-Za-z0-9]+", url):
            return await loop.run_in_executor(None, self._resolve_spotify_track, url)

        raise ValueError("URL de cancion no soportada. Usa deezer.com/track o spotify.com/track")

    def _resolve_deezer_track(self, url: str) -> Dict:
        match = re.search(r"/track/(\d+)", url)
        if not match:
            raise ValueError(f"No se pudo extraer track_id de URL de Deezer: {url}")

        track_id = match.group(1)
        item = self._dz.api.get_track(track_id)
        if isinstance(item, dict) and "error" in item:
            raise RuntimeError(f"Deezer API error: {item.get('error', {})}")
        result = _search_result_from_track_item(item)
        if not result.get("id"):
            raise RuntimeError("No se pudo resolver la cancion de Deezer.")
        return result

    def _resolve_spotify_track(self, url: str) -> Dict:
        self._ensure_spotify_client()
        match = re.search(r"spotify\.com/(?:intl-[a-z]{2}/)?track/([a-zA-Z0-9]+)", url)
        if not match:
            raise ValueError(f"No se pudo extraer track_id de URL de Spotify: {url}")

        spotify_track = self._spotdl_spotify.track(match.group(1))
        if not isinstance(spotify_track, dict):
            raise RuntimeError("Spotify no devolvio metadata valida para la cancion.")

        sp_name = str(spotify_track.get("name") or "")
        sp_artists = spotify_track.get("artists") or []
        sp_artist_names = [
            str(artist.get("name", "")).strip()
            for artist in sp_artists
            if isinstance(artist, dict) and str(artist.get("name", "")).strip()
        ]
        sp_artist = sp_artist_names[0] if sp_artist_names else ""
        sp_album_obj = spotify_track.get("album") if isinstance(spotify_track.get("album"), dict) else {}
        sp_album = str(sp_album_obj.get("name") or "")
        sp_duration_seconds = int((spotify_track.get("duration_ms") or 0) / 1000)
        sp_cover = _spotify_cover_url(sp_album_obj)
        sp_preview = spotify_track.get("preview_url")
        isrc = None
        external_ids = spotify_track.get("external_ids")
        if isinstance(external_ids, dict):
            isrc = external_ids.get("isrc")

        dz_track = None
        if isrc:
            try:
                dz_result = self._dz.api.get_track_by_ISRC(isrc)
                if isinstance(dz_result, dict) and "id" in dz_result and "error" not in dz_result:
                    dz_track = dz_result
                    dz_track["_match_info"] = {"method": "isrc", "reasons": "isrc_exact"}
            except Exception:
                logger.debug("Spotify track ISRC lookup failed isrc=%s", isrc)

        if not dz_track:
            dz_track = self._find_best_spotify_match(
                artist=sp_artist,
                artists=sp_artist_names,
                title=sp_name,
                album=sp_album,
                duration_seconds=sp_duration_seconds,
            )

        if not dz_track:
            raise RuntimeError(f"No encontre match confiable en Deezer para {sp_artist} - {sp_name}".strip(" -"))

        result = _search_result_from_track_item(dz_track)
        if not result.get("cover_url"):
            result["cover_url"] = sp_cover
        if isinstance(sp_preview, str) and sp_preview.startswith(("http://", "https://")):
            result["preview_url"] = result.get("preview_url") or sp_preview
        return result

    # ----- Deezer nativo -----
    def _resolve_deezer_playlist(self, url: str) -> Dict:
        match = re.search(r"/playlist/(\d+)", url)
        if not match:
            raise ValueError(f"No se pudo extraer playlist_id de URL de Deezer: {url}")
        playlist_id = match.group(1)

        # Metadata
        meta = self._dz.api.get_playlist(playlist_id)
        if isinstance(meta, dict) and "error" in meta:
            raise RuntimeError(f"Deezer API error: {meta.get('error', {})}")

        title = meta.get("title", "Playlist sin título")

        # Tracks (limit=1000 trae todos en una sola llamada para la mayoría)
        tracks_raw = self._dz.api.get_playlist_tracks(playlist_id, limit=1000)
        data = tracks_raw.get("data", []) if isinstance(tracks_raw, dict) else []

        resolved = []
        skipped_tracks = []
        for item in data:
            track_id = str(item.get("id", ""))
            if not track_id or track_id == "0":
                skipped_tracks.append(str(item.get("title") or "Track desconocido"))
                continue

            track_title = str(item.get("title", "Unknown"))
            artist = "Unknown"
            album = ""
            duration = ""
            cover_url = ""

            artist_obj = item.get("artist")
            if isinstance(artist_obj, dict):
                artist = str(artist_obj.get("name", "Unknown"))
            elif isinstance(artist_obj, str):
                artist = artist_obj

            album_obj = item.get("album")
            if isinstance(album_obj, dict):
                album = str(album_obj.get("title", ""))
                md5 = album_obj.get("md5_image", "")
                if md5:
                    cover_url = f"https://e-cdns-images.dzcdn.net/images/cover/{md5}/800x800-000000-80-0-0.jpg"
            elif isinstance(album_obj, str):
                album = album_obj

            dur_sec = item.get("duration")
            if isinstance(dur_sec, int):
                m, s = divmod(dur_sec, 60)
                duration = f"{m}:{s:02d}"

            resolved.append({
                "id": track_id,
                "title": track_title,
                "artist": artist,
                "album": album,
                "duration": duration,
                "cover_url": cover_url,
            })

        return {
            "id": f"deezer_{playlist_id}",
            "source": "deezer",
            "title": title,
            "track_count": len(data),
            "resolved_count": len(resolved),
            "skipped_count": len(data) - len(resolved),
            "skipped_tracks": skipped_tracks,
            "tracks": resolved,
        }

    # ----- Spotify ? Deezer -----
    def _resolve_spotify_playlist(self, url: str) -> Dict:
        self._ensure_spotify_client()

        # Extraer playlist_id de URL de Spotify
        match = re.search(r"playlist/([a-zA-Z0-9]+)", url)
        if not match:
            raise ValueError(f"No se pudo extraer playlist_id de URL de Spotify: {url}")
        playlist_id = match.group(1)

        # Obtener metadata de la playlist. Si SpotipyFree cambia su shape,
        # no bloqueamos la descarga: playlist_items puede seguir funcionando.
        try:
            pl_meta = self._spotdl_spotify.playlist(playlist_id)
        except Exception as e:
            pl_meta = {}

        title = pl_meta.get("name", f"Spotify playlist {playlist_id}") if isinstance(pl_meta, dict) else f"Spotify playlist {playlist_id}"

        # Obtener tracks paginados
        try:
            items_data = self._spotdl_spotify.playlist_items(playlist_id)
        except Exception as e:
            raise RuntimeError(
                f"No se pudieron obtener los tracks de la playlist de Spotify. Error: {e}"
            ) from e

        items = items_data.get("items", []) if isinstance(items_data, dict) else []

        # Resolver cada track a Deezer
        resolved = []
        skipped_tracks = []
        skipped = 0
        for sp_item in items:
            sp_track = sp_item.get("track") if isinstance(sp_item, dict) else None
            if not sp_track:
                skipped += 1
                skipped_tracks.append("Track desconocido")
                continue
            if sp_track.get("is_local"):
                skipped += 1
                skipped_tracks.append(str(sp_track.get("name") or "Track local"))
                continue

            sp_name = sp_track.get("name", "")
            sp_artists = sp_track.get("artists", [])
            sp_artist_names = [
                str(artist.get("name", "")).strip()
                for artist in sp_artists
                if isinstance(artist, dict) and str(artist.get("name", "")).strip()
            ]
            sp_artist = sp_artist_names[0] if sp_artist_names else ""
            sp_album = sp_track.get("album", {}).get("name", "") if isinstance(sp_track.get("album"), dict) else ""
            sp_duration_ms = sp_track.get("duration_ms", 0)

            # Cover de Spotify como fallback
            sp_cover = ""
            images = sp_track.get("album", {}).get("images", []) if isinstance(sp_track.get("album"), dict) else []
            if images:
                sp_cover = images[0].get("url", "")

            isrc = sp_track.get("external_ids", {}).get("isrc") if isinstance(sp_track.get("external_ids"), dict) else None

            dz_track = None

            # 1) Match por ISRC
            if isrc:
                try:
                    dz_result = self._dz.api.get_track_by_ISRC(isrc)
                    if isinstance(dz_result, dict) and "id" in dz_result and "error" not in dz_result:
                        dz_track = dz_result
                        dz_track["_match_info"] = {
                            "method": "isrc",
                            "score": "",
                            "reasons": "isrc_exact",
                            "spotify_title": sp_name,
                            "spotify_artist": sp_artist,
                            "spotify_artists": ", ".join(sp_artist_names),
                            "spotify_album": sp_album,
                            "spotify_duration": str(sp_duration_ms // 1000 if sp_duration_ms else ""),
                            "spotify_isrc": str(isrc or ""),
                        }
                except Exception:
                    pass

            # 2) Fallback por búsqueda
            if not dz_track:
                dz_track = self._find_best_spotify_match(
                    artist=sp_artist,
                    artists=sp_artist_names,
                    title=sp_name,
                    album=sp_album,
                    duration_seconds=sp_duration_ms // 1000 if sp_duration_ms else 0,
                )

            if not dz_track:
                skipped += 1
                skipped_tracks.append(
                    f"{sp_artist} - {sp_name}: sin match confiable en Deezer".strip(" -")
                    or "Track desconocido"
                )
                continue

            dz_id = str(dz_track.get("id", ""))
            if not dz_id or dz_id == "0":
                skipped += 1
                skipped_tracks.append(f"{sp_artist} - {sp_name}".strip(" -") or "Track desconocido")
                continue

            dz_title = str(dz_track.get("title", sp_name))
            dz_artist = sp_artist
            dz_album = sp_album
            dz_duration = ""
            dz_cover = ""

            artist_obj = dz_track.get("artist")
            if isinstance(artist_obj, dict):
                dz_artist = str(artist_obj.get("name", sp_artist))
            elif isinstance(artist_obj, str):
                dz_artist = artist_obj

            album_obj = dz_track.get("album")
            if isinstance(album_obj, dict):
                dz_album = str(album_obj.get("title", sp_album))
                md5 = album_obj.get("md5_image", "")
                if md5:
                    dz_cover = f"https://e-cdns-images.dzcdn.net/images/cover/{md5}/800x800-000000-80-0-0.jpg"
            elif isinstance(album_obj, str):
                dz_album = album_obj

            dur_sec = dz_track.get("duration")
            if isinstance(dur_sec, int):
                m, s = divmod(dur_sec, 60)
                dz_duration = f"{m}:{s:02d}"
            elif sp_duration_ms:
                m, s = divmod(sp_duration_ms // 1000, 60)
                dz_duration = f"{m}:{s:02d}"

            resolved.append({
                "id": dz_id,
                "title": dz_title,
                "artist": dz_artist,
                "album": dz_album,
                "duration": dz_duration,
                "cover_url": dz_cover or sp_cover,
                "match": dz_track.get("_match_info", {}),
            })

        return {
            "id": f"spotify_{playlist_id}",
            "source": "spotify",
            "title": title,
            "track_count": len(items),
            "resolved_count": len(resolved),
            "skipped_count": skipped,
            "skipped_tracks": skipped_tracks,
            "tracks": resolved,
        }

    def _find_best_spotify_match(
        self,
        *,
        artist: str,
        artists: list[str],
        title: str,
        album: str,
        duration_seconds: int,
    ) -> dict | None:
        candidates_by_id: dict[str, dict] = {}

        for query in _spotify_match_queries(artist, title):
            try:
                search_result = self._dz.api.search_track(query, limit=15)
            except Exception:
                logger.debug("Spotify fallback search failed query=%r", query)
                continue
            if not isinstance(search_result, dict):
                continue
            for candidate in search_result.get("data", []) or []:
                candidate_id = str(candidate.get("id", "") or "")
                if candidate_id and candidate_id not in candidates_by_id:
                    candidates_by_id[candidate_id] = candidate

        for query in _spotify_album_match_queries(artist, album, title):
            try:
                album_result = self._dz.api.search_album(query, limit=8)
            except Exception:
                logger.debug("Spotify fallback album search failed query=%r", query)
                continue
            if not isinstance(album_result, dict):
                continue
            for album_item in album_result.get("data", []) or []:
                album_id = album_item.get("id")
                if not album_id:
                    continue
                try:
                    tracks_result = self._dz.api.get_album_tracks(album_id, limit=100)
                except Exception:
                    logger.debug("Spotify fallback album tracks failed album_id=%s", album_id)
                    continue
                if not isinstance(tracks_result, dict):
                    continue
                for track_item in tracks_result.get("data", []) or []:
                    candidate = _album_track_candidate(track_item, album_item)
                    candidate_id = str(candidate.get("id", "") or "")
                    if candidate_id and candidate_id not in candidates_by_id:
                        candidates_by_id[candidate_id] = candidate

        best_candidate = None
        best_score = -1
        best_reasons: list[str] = []
        for candidate in candidates_by_id.values():
            score, reasons = _spotify_deezer_match_score(
                sp_artist=artist,
                sp_artists=artists,
                sp_title=title,
                sp_album=album,
                sp_duration_seconds=duration_seconds,
                dz_track=candidate,
            )
            if score > best_score:
                best_candidate = candidate
                best_score = score
                best_reasons = reasons

        if best_candidate is None or best_score < 150:
            logger.info(
                "Spotify match rejected artist=%s title=%s best_score=%s reasons=%s",
                artist,
                title,
                best_score,
                ", ".join(best_reasons),
            )
            return None

        logger.info(
            "Spotify match accepted artist=%s title=%s -> deezer_artist=%s deezer_title=%s score=%s reasons=%s",
            artist,
            title,
            _candidate_artist_name(best_candidate),
            best_candidate.get("title"),
            best_score,
            ", ".join(best_reasons),
        )
        best_candidate["_match_info"] = {
            "method": "text_album_score",
            "score": str(best_score),
            "reasons": "; ".join(best_reasons),
            "spotify_title": title,
            "spotify_artist": artist,
            "spotify_artists": ", ".join(artists),
            "spotify_album": album,
            "spotify_duration": str(duration_seconds or ""),
            "spotify_isrc": "",
        }
        return best_candidate

    # ------------------------------------------------------------------
    #  download  (ya funciona, no tocar)
    # ------------------------------------------------------------------
    async def download(self, track_id: str, quality: str, output_dir: Path) -> Path:
        self._ensure_initialized()
        if self._dz_queue is None:
            raise RuntimeError("Pool de Deezer no inicializado.")

        quality = quality.lower()
        bitrate = self._QUALITY_MAP.get(quality)
        if bitrate is None:
            raise ValueError(f"Calidad no soportada: {quality}. Usa 'flac' o 'mp3_320'.")

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        link = f"https://www.deezer.com/track/{track_id}"

        loop = asyncio.get_event_loop()
        dz = await self._dz_queue.get()

        def _download() -> Path:
            total_started_at = time.monotonic()
            parse_seconds = 0.0
            object_seconds = 0.0
            settings_seconds = 0.0
            downloader_seconds = 0.0
            scan_seconds = 0.0
            file_size = 0

            parse_started_at = time.monotonic()
            _, url_type, parsed_id = parseLink(link)
            parse_seconds = time.monotonic() - parse_started_at
            if url_type != "track":
                raise ValueError(f"El link no es un track válido: {link}")

            object_started_at = time.monotonic()
            download_object = generateDownloadObject(
                dz=dz,
                link=link,
                bitrate=bitrate,
                plugins=None,
                listener=None,
            )
            object_seconds = time.monotonic() - object_started_at

            settings_started_at = time.monotonic()
            settings = dict(DEEMIX_DEFAULTS)
            settings["downloadLocation"] = str(output_dir)
            settings["createArtistFolder"] = False
            settings["createAlbumFolder"] = False
            settings["createSingleFolder"] = False
            settings["createCDFolder"] = False
            settings["createPlaylistFolder"] = False
            settings["createStructurePlaylist"] = False
            settings["saveArtwork"] = False
            settings["syncedLyrics"] = False
            settings["overwriteFile"] = "y"
            settings_seconds = time.monotonic() - settings_started_at

            downloader = Downloader(
                dz=dz,
                downloadObject=download_object,
                settings=settings,
                listener=None,
            )
            downloader_started_at = time.monotonic()
            downloader.start()
            downloader_seconds = time.monotonic() - downloader_started_at

            if getattr(download_object, "failed", 0):
                raise RuntimeError(_format_deemix_errors(getattr(download_object, "errors", [])))

            if quality == "flac":
                expected_ext = ".flac"
            else:
                expected_ext = ".mp3"

            scan_started_at = time.monotonic()
            candidates = list(output_dir.glob(f"*{expected_ext}"))
            if not candidates:
                for ext in (".flac", ".mp3", ".m4a", ".ogg"):
                    candidates = list(output_dir.glob(f"*{ext}"))
                    if candidates:
                        break

            if not candidates:
                raise FileNotFoundError(
                    f"No se encontró archivo descargado en {output_dir} para track {track_id}"
                )

            final_file = max(candidates, key=lambda p: p.stat().st_mtime)
            scan_seconds = time.monotonic() - scan_started_at
            try:
                file_size = final_file.stat().st_size
            except OSError:
                file_size = 0
            total_seconds = time.monotonic() - total_started_at
            logger.info(
                (
                    "Provider metric track_id=%s quality=%s parse_s=%.3f object_s=%.3f "
                    "settings_s=%.3f downloader_s=%.3f scan_s=%.3f total_s=%.3f file_mb=%.2f"
                ),
                track_id,
                quality,
                parse_seconds,
                object_seconds,
                settings_seconds,
                downloader_seconds,
                scan_seconds,
                total_seconds,
                file_size / (1024 * 1024),
            )
            return final_file.resolve()

        try:
            final_path = await loop.run_in_executor(None, _download)
        finally:
            self._dz_queue.put_nowait(dz)

        if not final_path.exists():
            raise FileNotFoundError(
                f"El archivo descargado no existe: {final_path}"
            )

        return final_path


def _spotify_match_queries(artist: str, title: str) -> list[str]:
    clean_artist = _clean_match_text(artist)
    clean_title = _clean_match_text(title)
    raw = f"{artist} {title}".strip()
    candidates = [
        raw,
        f"{clean_artist} {clean_title}".strip(),
        f"{clean_title} {clean_artist}".strip(),
        clean_title,
    ]
    unique = []
    seen = set()
    for query in candidates:
        query = re.sub(r"\s+", " ", query).strip()
        if query and query.lower() not in seen:
            seen.add(query.lower())
            unique.append(query)
    return unique


def _spotify_album_match_queries(artist: str, album: str, title: str) -> list[str]:
    clean_artist = _clean_match_text(artist)
    clean_album = _clean_match_text(album)
    clean_title = _clean_match_text(title)
    candidates = [
        f"{clean_artist} {clean_album}".strip(),
        clean_album,
        f"{clean_artist} {clean_title}".strip(),
    ]
    unique = []
    seen = set()
    for query in candidates:
        query = re.sub(r"\s+", " ", query).strip()
        if query and query.lower() not in seen:
            seen.add(query.lower())
            unique.append(query)
    return unique


def _album_track_candidate(track_item: dict, album_item: dict) -> dict:
    candidate = dict(track_item)
    if not isinstance(candidate.get("artist"), dict):
        album_artist = album_item.get("artist") if isinstance(album_item, dict) else None
        if isinstance(album_artist, dict):
            candidate["artist"] = album_artist
    if not isinstance(candidate.get("album"), dict):
        candidate["album"] = {
            "id": album_item.get("id"),
            "title": album_item.get("title", ""),
            "cover": album_item.get("cover", ""),
            "cover_small": album_item.get("cover_small", ""),
            "cover_medium": album_item.get("cover_medium", ""),
            "cover_big": album_item.get("cover_big", ""),
            "cover_xl": album_item.get("cover_xl", ""),
            "md5_image": album_item.get("md5_image", ""),
        }
    return candidate


def _spotify_cover_url(album_obj: dict) -> str:
    images = album_obj.get("images") if isinstance(album_obj, dict) else None
    if isinstance(images, list) and images:
        value = images[0].get("url") if isinstance(images[0], dict) else None
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value

    cover_art = album_obj.get("coverArt") if isinstance(album_obj, dict) else None
    sources = cover_art.get("sources") if isinstance(cover_art, dict) else None
    if isinstance(sources, list) and sources:
        value = sources[-1].get("url") if isinstance(sources[-1], dict) else None
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    return ""


def _expand_shared_url(url: str) -> str:
    if not re.search(r"https?://(?:spotify\.link|deezer\.page\.link)/", url):
        return url
    try:
        response = requests.get(url, allow_redirects=True, timeout=8)
        final_url = response.url
        if isinstance(final_url, str) and final_url.startswith(("http://", "https://")):
            return final_url
    except Exception as exc:
        logger.debug("Could not expand shared url=%s: %s", url, exc)
    return url


def _spotify_deezer_match_score(
    *,
    sp_artist: str,
    sp_artists: list[str],
    sp_title: str,
    sp_album: str,
    sp_duration_seconds: int,
    dz_track: dict,
) -> tuple[int, list[str]]:
    dz_title = str(dz_track.get("title", "") or "")
    dz_artist = _candidate_artist_name(dz_track)
    dz_album = _candidate_album_title(dz_track)
    dz_duration = dz_track.get("duration")
    dz_duration_seconds = dz_duration if isinstance(dz_duration, int) else 0

    sp_title_base = _base_track_norm(sp_title)
    dz_title_base = _base_track_norm(dz_title)
    sp_artist_norms = [_artist_norm(value) for value in ([sp_artist] + sp_artists) if value]
    dz_artist_norm = _artist_norm(dz_artist)
    sp_album_norm = _search_norm(sp_album)
    dz_album_norm = _search_norm(dz_album)

    source_variants = _variant_terms(f"{sp_title} {sp_album}")
    candidate_variants = _variant_terms(f"{dz_title} {dz_album}")
    unexpected_variants = sorted(candidate_variants - source_variants)

    title_ratio = _ratio(sp_title_base, dz_title_base)
    artist_ratio = max((_ratio(value, dz_artist_norm) for value in sp_artist_norms), default=0.0)
    album_ratio = _ratio(sp_album_norm, dz_album_norm) if sp_album_norm and dz_album_norm else 0.0

    reasons = [
        f"title={title_ratio:.2f}",
        f"artist={artist_ratio:.2f}",
    ]

    if unexpected_variants:
        return -100, reasons + [f"unexpected_variant={','.join(unexpected_variants)}"]

    if artist_ratio < 0.58:
        return -90, reasons + ["artist_mismatch"]

    if title_ratio < 0.72 and sp_title_base not in dz_title_base and dz_title_base not in sp_title_base:
        return -80, reasons + ["title_mismatch"]

    score = int(title_ratio * 100) + int(artist_ratio * 80)
    if sp_title_base == dz_title_base:
        score += 60
        reasons.append("exact_title")
    if artist_ratio >= 0.92:
        score += 45
        reasons.append("strong_artist")
    if album_ratio >= 0.75:
        score += 20
        reasons.append(f"album={album_ratio:.2f}")

    if sp_duration_seconds and dz_duration_seconds:
        diff = abs(sp_duration_seconds - dz_duration_seconds)
        if diff <= 3:
            score += 35
            reasons.append(f"duration_diff={diff}")
        elif diff <= 8:
            score += 25
            reasons.append(f"duration_diff={diff}")
        elif diff <= 15:
            score += 10
            reasons.append(f"duration_diff={diff}")
        elif diff > max(25, int(sp_duration_seconds * 0.12)):
            score -= 45
            reasons.append(f"bad_duration_diff={diff}")

    return score, reasons


def _candidate_artist_name(track: dict) -> str:
    artist_obj = track.get("artist")
    if isinstance(artist_obj, dict):
        return str(artist_obj.get("name", "") or "")
    if isinstance(artist_obj, str):
        return artist_obj
    return ""


def _candidate_album_title(track: dict) -> str:
    album_obj = track.get("album")
    if isinstance(album_obj, dict):
        return str(album_obj.get("title", "") or "")
    if isinstance(album_obj, str):
        return album_obj
    return ""


def _variant_terms(value: str) -> set[str]:
    norm = _search_norm(value)
    variants = set()
    patterns = {
        "remix": r"\bremix(?:es)?\b",
        "live": r"\blive\b",
        "sped_up": r"\b(?:sped|speed)\s+up\b|\bnightcore\b",
        "karaoke": r"\bkaraoke\b",
        "instrumental": r"\binstrumental\b",
        "cover": r"\bcover\b",
        "tribute": r"\btribute\b",
        "remake": r"\bremake\b",
        "acoustic": r"\bacoustic\b",
        "unpeeled": r"\bunpeeled\b",
        "demo": r"\bdemo\b",
        "mixed": r"\bmixed\b",
    }
    for name, pattern in patterns.items():
        if re.search(pattern, norm):
            variants.add(name)
    return variants


def _base_track_norm(value: str) -> str:
    norm = _search_norm(value)
    norm = re.sub(
        r"\b(?:remix(?:es)?|live|karaoke|instrumental|cover|tribute|remake|acoustic|unpeeled|nightcore|demo|mixed)\b",
        " ",
        norm,
    )
    norm = re.sub(r"\b(?:sped|speed)\s+up\b", " ", norm)
    norm = re.sub(r"\b(?:radio|single|album|original|extended)\s+(?:edit|version|mix)\b", " ", norm)
    return re.sub(r"\s+", " ", norm).strip()


def _artist_norm(value: str) -> str:
    norm = _search_norm(value)
    norm = re.sub(r"\b(?:and|&|feat|ft|with)\b", " ", norm)
    return re.sub(r"\s+", " ", norm).strip()


def _ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        return 0.9
    return SequenceMatcher(None, left, right).ratio()


def _search_result_from_track_item(item: dict, album_item: Optional[dict] = None) -> Dict:
    track_id = str(item.get("id", "") or "")
    title = str(item.get("title", "Unknown") or "Unknown")
    artist = "Unknown"
    album = ""
    duration = ""
    cover_url = ""
    preview_url = ""

    artist_obj = item.get("artist")
    if isinstance(artist_obj, dict):
        artist = str(artist_obj.get("name", "Unknown") or "Unknown")
    elif isinstance(artist_obj, str):
        artist = artist_obj

    album_obj = item.get("album")
    if isinstance(album_obj, dict):
        album = str(album_obj.get("title", "") or "")
        for cover_key in ("cover_xl", "cover_big", "cover_medium", "cover"):
            value = album_obj.get(cover_key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                cover_url = value
                break
    elif isinstance(album_obj, str):
        album = album_obj

    if isinstance(album_item, dict):
        album = str(album_item.get("title", album) or album)
        album_artist = album_item.get("artist")
        if artist == "Unknown" and isinstance(album_artist, dict):
            artist = str(album_artist.get("name", artist) or artist)
        for cover_key in ("cover_xl", "cover_big", "cover_medium", "cover"):
            value = album_item.get(cover_key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                cover_url = value
                break

    dur_sec = item.get("duration")
    if isinstance(dur_sec, int):
        m, s = divmod(dur_sec, 60)
        duration = f"{m}:{s:02d}"

    preview = item.get("preview")
    if isinstance(preview, str) and preview.startswith(("http://", "https://")):
        preview_url = preview

    return {
        "id": track_id,
        "title": title,
        "artist": artist,
        "album": album,
        "duration": duration,
        "cover_url": cover_url,
        "preview_url": preview_url,
    }


def _search_result_score(query: str, item: Dict) -> int:
    query_norm = _search_norm(query)
    title_norm = _search_norm(str(item.get("title", "")))
    artist_norm = _search_norm(str(item.get("artist", "")))
    album_norm = _search_norm(str(item.get("album", "")))
    haystack = f"{artist_norm} {title_norm} {album_norm}".strip()
    query_tokens = [token for token in query_norm.split() if len(token) > 1]

    score = 0
    for token in query_tokens:
        if token in haystack:
            score += 10
        if token in title_norm:
            score += 4
        if token in artist_norm:
            score += 3

    if query_norm and query_norm in f"{artist_norm} {title_norm}":
        score += 80
    if query_norm and query_norm in f"{title_norm} {artist_norm}":
        score += 70
    if title_norm and title_norm in query_norm:
        score += 55
    if artist_norm and artist_norm in query_norm:
        score += 25
    if title_norm == query_norm:
        score += 70
    score += int(item.get("_album_search_boost", 0) or 0)

    query_asks_variant = any(
        word in query_norm
        for word in ("remix", "karaoke", "instrumental", "cover", "tribute", "remake", "acoustic", "live")
    )
    variant_text = f"{title_norm} {album_norm}"
    if not query_asks_variant:
        if any(word in variant_text for word in ("remix", "karaoke", "instrumental", "cover", "tribute", "remake")):
            score -= 70
        if "made popular by" in variant_text or "originally performed" in variant_text:
            score -= 80
        if "remixes" in album_norm:
            score -= 35

    return score


def _search_norm(value: str) -> str:
    value = _clean_match_text(value).lower()
    value = value.replace("$", "s")
    value = re.sub(r"\b(?:the|a|an)\b", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _format_deemix_errors(errors) -> str:
    if not errors:
        return "Deemix no pudo descargar el track."
    messages = []
    for error in errors:
        if not isinstance(error, dict):
            messages.append(str(error))
            continue
        errid = str(error.get("errid") or "")
        message = str(error.get("message") or "").strip()
        data = error.get("data") if isinstance(error.get("data"), dict) else {}
        artist = str(data.get("artist") or "").strip()
        title = str(data.get("title") or "").strip()
        label = f"{artist} - {title}".strip(" -")
        if errid == "wrongGeolocationNoAlternative":
            reason = "No disponible por geolocalizacion para esta cuenta/region."
        elif message:
            reason = message
        elif errid:
            reason = errid
        else:
            reason = "Deemix no pudo descargar el track."
        messages.append(f"{label}: {reason}" if label else reason)
    return "; ".join(messages)


def _clean_match_text(value: str) -> str:
    value = re.sub(r"[()\[\]{}]", " ", str(value))
    value = re.sub(r"\b(?:\d{4}\s*)?remaster(?:ed)?\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(?:feat|ft)\.?\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"[^\w\sÀ-ÿ'&.-]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()
