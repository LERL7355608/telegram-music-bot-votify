# providers/votify.py
"""
Provider de procesamiento multimedia usando Votify CLI (Spotify) + ffmpeg.
Requiere:
  - SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET (Web API, gratis en developer.spotify.com)
  - SPOTIFY_DLL_PATH (Spotify.dll compatible, default /app/config/Spotify.dll)
  - COOKIES_PATH (cookies.txt, default /app/config/cookies.txt)

Calidades soportadas:
  - mp3_320: Vorbis 320kbps de Spotify -> MP3 320kbps
  - flac: FLAC nativo de Spotify
"""
import os
import re
import asyncio
import logging
import time
import subprocess
import urllib.parse
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime, timedelta, timezone

import requests

from providers.base import DownloadProvider

logger = logging.getLogger(__name__)


class VotifyProvider(DownloadProvider):
    """Provider que orquesta descarga desde Spotify via Votify CLI + ffmpeg."""

    def __init__(self) -> None:
        self._client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
        self._client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
        self._spotify_dll_path = os.getenv(
            "SPOTIFY_DLL_PATH", "/app/config/Spotify.dll"
        ).strip()
        self._cookies_path = os.getenv("COOKIES_PATH", "/app/config/cookies.txt").strip()

        self._token: Optional[str] = None
        self._token_expires_at: Optional[datetime] = None

    # ------------------------------------------------------------------
    #  Spotify Web API (Client Credentials)
    # ------------------------------------------------------------------
    def _ensure_credentials(self) -> None:
        if not self._client_id or not self._client_secret:
            raise RuntimeError(
                "SPOTIFY_CLIENT_ID y SPOTIFY_CLIENT_SECRET deben estar en .env. "
                "Obténlos gratis en https://developer.spotify.com/dashboard"
            )

    def _get_access_token(self) -> str:
        if self._token and self._token_expires_at and datetime.now(timezone.utc) < self._token_expires_at:
            return self._token

        self._ensure_credentials()
        resp = requests.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            auth=(self._client_id, self._client_secret),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        expires_in = data.get("expires_in", 3600)
        self._token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)
        return self._token

    def _spotify_get(self, endpoint: str) -> dict:
        token = self._get_access_token()
        url = f"https://api.spotify.com/v1{endpoint}"
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=20)
        if resp.status_code == 401:
            self._token = None
            token = self._get_access_token()
            resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=20)
        resp.raise_for_status()
        return resp.json()

    def _track_to_result(self, track: dict) -> dict:
        artists = track.get("artists", [])
        artist_names = [a.get("name", "") for a in artists if isinstance(a, dict)]
        artist = artist_names[0] if artist_names else "Unknown"

        album = track.get("album", {}) or {}
        album_name = album.get("name", "") if isinstance(album, dict) else ""

        images = album.get("images", []) if isinstance(album, dict) else []
        cover_url = images[0].get("url", "") if images else ""

        duration_ms = track.get("duration_ms", 0) or 0
        m, s = divmod(duration_ms // 1000, 60)
        duration = f"{m}:{s:02d}"

        return {
            "id": track.get("id", ""),
            "title": track.get("name", "Unknown"),
            "artist": artist,
            "album": album_name,
            "duration": duration,
            "cover_url": cover_url,
            "preview_url": track.get("preview_url", ""),
        }

    # ------------------------------------------------------------------
    #  search
    # ------------------------------------------------------------------
    async def search(self, query: str) -> List[Dict]:
        loop = asyncio.get_event_loop()

        def _search() -> List[Dict]:
            encoded = urllib.parse.quote(query)
            results = []
            seen = set()
            for offset in range(0, 40, 10):
                data = self._spotify_get(
                    f"/search?q={encoded}&type=track&limit=10&offset={offset}"
                )
                tracks = data.get("tracks", {}).get("items", [])
                for track in tracks:
                    tid = track.get("id")
                    if not tid or tid in seen:
                        continue
                    seen.add(tid)
                    results.append(self._track_to_result(track))
                if len(tracks) < 10:
                    break
            return results[:40]

        return await loop.run_in_executor(None, _search)

    # ------------------------------------------------------------------
    #  resolve_track
    # ------------------------------------------------------------------
    async def resolve_track(self, url: str) -> Dict:
        loop = asyncio.get_event_loop()
        url = await loop.run_in_executor(None, _expand_shared_url, url)

        match = re.search(
            r"(?:open\.)?spotify\.com/(?:intl-[a-z]{2}/)?track/([a-zA-Z0-9]+)", url
        )
        if not match:
            raise ValueError(f"URL de cancion no soportada: {url}")

        track_id = match.group(1)

        def _resolve() -> Dict:
            track = self._spotify_get(f"/tracks/{track_id}")
            return self._track_to_result(track)

        return await loop.run_in_executor(None, _resolve)

    # ------------------------------------------------------------------
    #  resolve_playlist
    # ------------------------------------------------------------------
    async def resolve_playlist(self, url: str) -> Dict:
        loop = asyncio.get_event_loop()
        url = await loop.run_in_executor(None, _expand_shared_url, url)

        match = re.search(
            r"(?:open\.)?spotify\.com/(?:intl-[a-z]{2}/)?playlist/([a-zA-Z0-9]+)", url
        )
        if not match:
            raise ValueError("URL de playlist no soportada. Usa spotify.com/playlist")

        playlist_id = match.group(1)

        def _resolve() -> Dict:
            meta = self._spotify_get(f"/playlists/{playlist_id}")
            title = meta.get("name", f"Playlist {playlist_id}")
            total_tracks = 0

            resolved = []
            skipped = []
            endpoint = f"/playlists/{playlist_id}/items?limit=50&offset=0"

            while endpoint:
                data = self._spotify_get(endpoint)
                total_tracks = data.get("total", total_tracks)
                items = data.get("items", [])
                for item in items:
                    track = None
                    if isinstance(item, dict):
                        track = item.get("item") or item.get("track")
                    if not track:
                        skipped.append("Track desconocido")
                        continue
                    if track.get("is_local"):
                        skipped.append(str(track.get("name") or "Track local"))
                        continue
                    if not track.get("id"):
                        skipped.append(str(track.get("name") or "Sin ID"))
                        continue
                    resolved.append(self._track_to_result(track))

                next_url = data.get("next")
                if next_url:
                    parsed = urllib.parse.urlparse(next_url)
                    endpoint = f"{parsed.path}?{parsed.query}"
                else:
                    endpoint = None

            return {
                "id": f"spotify_{playlist_id}",
                "source": "spotify",
                "title": title,
                "track_count": total_tracks,
                "resolved_count": len(resolved),
                "skipped_count": len(skipped),
                "skipped_tracks": skipped,
                "tracks": resolved,
            }

        return await loop.run_in_executor(None, _resolve)

    # ------------------------------------------------------------------
    #  download
    # ------------------------------------------------------------------
    async def download(self, track_id: str, quality: str, output_dir: Path) -> Path:
        quality = quality.lower()
        if quality not in ("mp3_320", "flac"):
            raise ValueError(f"Calidad no soportada: {quality}. Usa 'mp3_320' o 'flac'.")

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        if not Path(self._spotify_dll_path).is_file():
            raise RuntimeError(
                f"Spotify.dll no encontrado en: {self._spotify_dll_path}. "
                "Monta la version compatible dentro del contenedor."
            )
        if not Path(self._cookies_path).exists():
            raise RuntimeError(
                f"Archivo cookies no encontrado en: {self._cookies_path}. "
                "Exporta las cookies de open.spotify.com con la extension 'Get cookies.txt'."
            )

        url = f"https://open.spotify.com/track/{track_id}"
        loop = asyncio.get_event_loop()

        def _download() -> Path:
            tmp_dir = output_dir / f"_votify_{track_id}_{int(time.time())}"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            votify_quality = "vorbis-high" if quality == "mp3_320" else "flac-flac"

            cmd = [
                "votify",
                "--audio-quality", votify_quality,
                "--session-type", "desktop",
                "--output", str(tmp_dir),
                "--cookies-path", self._cookies_path,
                "--spotify-dll-path", self._spotify_dll_path,
                "--no-config-file",
                url,
            ]

            logger.info("Ejecutando Votify: %s", " ".join(cmd))
            started_at = time.monotonic()
            votify_env = os.environ.copy()
            votify_env["PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION"] = "python"

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=300,
                    env=votify_env,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"Votify timeout descargando track {track_id}") from exc

            votify_s = time.monotonic() - started_at
            logger.info(
                "Votify termino en %.2fs para track %s (exit=%s)",
                votify_s, track_id, result.returncode,
            )

            if result.returncode != 0:
                logger.error("Votify stderr: %s", result.stderr)
                raise RuntimeError(
                    f"Votify fallo para track {track_id}. stderr: {result.stderr[:800]}"
                )

            # Buscar archivo descargado recursivamente
            candidates = []
            for ext in (".ogg", ".m4a", ".mp4", ".flac", ".mp3"):
                candidates.extend(tmp_dir.rglob(f"*{ext}"))

            if not candidates:
                raise FileNotFoundError(
                    f"Votify no genero archivo de audio para track {track_id} en {tmp_dir}"
                )

            source_file = max(candidates, key=lambda p: p.stat().st_mtime)
            logger.info("Archivo descargado por Votify: %s", source_file)

            # Convertir segun calidad
            if quality == "mp3_320":
                final_ext = ".mp3"
                ffmpeg_cmd = [
                    "ffmpeg", "-y", "-i", str(source_file),
                    "-codec:a", "libmp3lame", "-b:a", "320k",
                    "-map_metadata", "0",
                    str(output_dir / f"{track_id}{final_ext}"),
                ]
            else:  # flac
                final_ext = ".flac"
                ffmpeg_cmd = [
                    "ffmpeg", "-y", "-i", str(source_file),
                    str(output_dir / f"{track_id}{final_ext}"),
                ]

            final_file = output_dir / f"{track_id}{final_ext}"

            logger.info("Convirtiendo con ffmpeg: %s -> %s", source_file.name, final_file.name)
            ffmpeg_started = time.monotonic()
            ffmpeg_result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=120)
            ffmpeg_s = time.monotonic() - ffmpeg_started

            if ffmpeg_result.returncode != 0:
                logger.error("ffmpeg stderr: %s", ffmpeg_result.stderr)
                raise RuntimeError(
                    f"ffmpeg fallo convirtiendo track {track_id}. stderr: {ffmpeg_result.stderr[:800]}"
                )

            # Limpiar temporal
            try:
                for f in tmp_dir.rglob("*"):
                    if f.is_file():
                        f.unlink()
                tmp_dir.rmdir()
            except OSError as exc:
                logger.debug("No se pudo limpiar tmp_dir: %s", exc)

            if not final_file.exists():
                raise FileNotFoundError(f"El archivo convertido no existe: {final_file}")

            file_size = final_file.stat().st_size
            total_s = time.monotonic() - started_at
            logger.info(
                "Provider metric track_id=%s quality=%s votify_s=%.3f ffmpeg_s=%.3f total_s=%.3f file_mb=%.2f",
                track_id, quality, votify_s, ffmpeg_s, total_s,
                file_size / (1024 * 1024),
            )

            return final_file.resolve()

        return await loop.run_in_executor(None, _download)


# ------------------------------------------------------------------
#  Helpers
# ------------------------------------------------------------------
def _expand_shared_url(url: str) -> str:
    if not re.search(r"https?://spotify\.link/", url):
        return url
    try:
        resp = requests.get(url, allow_redirects=True, timeout=8)
        final = resp.url
        if isinstance(final, str) and final.startswith(("http://", "https://")):
            return final
    except Exception as exc:
        logger.debug("Could not expand shared url=%s: %s", url, exc)
    return url
