"""Résolution d'une entrée utilisateur (URL ou texte) vers une ou plusieurs Track.

Cas gérés (dans l'ordre) :
1. URL YouTube (track) → yt-dlp direct
2. URL YouTube (playlist) → expansion liste
3. URL Spotify (track) → spotipy → recherche YouTube
4. URL Spotify (playlist/album) → spotipy → expansion → recherche YouTube par track
5. Texte libre → ytsearch
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import yt_dlp

from .spotify import SpotifyClient, SpotifyTrackInfo

if TYPE_CHECKING:
    from .player import Track

log = logging.getLogger(__name__)


# Options yt-dlp partagées
YDL_OPTS_BASE = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "noplaylist": False,
    "extract_flat": False,
    "skip_download": True,
}

# Header FFmpeg pour gérer les reconnects (utile pour HLS / longues lives)
FFMPEG_BEFORE_OPTS = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
)

# Filtres audio :
#   -vn                : pas de stream vidéo (on est en audio uniquement)
#   dynaudnorm         : normalisation dynamique du volume — égalise les tracks
#                        YouTube/Spotify dont les niveaux varient. Tuning :
#     f=250            : frame de 250ms (défaut 500, plus réactif)
#     g=15             : fenêtre gaussienne sur 15 frames (défaut 31, plus lisse)
#     p=0.95           : peak target 95% (défaut, évite le clipping)
#     m=10             : multiplicateur max (défaut 10, limite le gain en haut)
# Coût CPU négligeable sur SRV-DEB.
FFMPEG_OPTS = "-vn -af dynaudnorm=f=250:g=15:p=0.95:m=10"


# --- Regex de détection ---
_YT_TRACK = re.compile(
    r"^(https?://)?(www\.|m\.|music\.)?(youtube\.com/watch\?|youtu\.be/)", re.IGNORECASE
)
_YT_PLAYLIST = re.compile(
    r"^(https?://)?(www\.|m\.|music\.)?youtube\.com/playlist\?list=", re.IGNORECASE
)
_YT_PLAYLIST_PARAM = re.compile(r"[?&]list=([A-Za-z0-9_-]+)")
_SP_TRACK = re.compile(r"open\.spotify\.com/(?:intl-\w+/)?track/([A-Za-z0-9]+)", re.IGNORECASE)
_SP_PLAYLIST = re.compile(r"open\.spotify\.com/(?:intl-\w+/)?playlist/([A-Za-z0-9]+)", re.IGNORECASE)
_SP_ALBUM = re.compile(r"open\.spotify\.com/(?:intl-\w+/)?album/([A-Za-z0-9]+)", re.IGNORECASE)


@dataclass(slots=True)
class ResolvedBatch:
    """Résultat d'une résolution : 1+ tracks + libellé de la source."""

    tracks: list["Track"]
    source_label: str  # "YouTube", "YouTube Playlist", "Spotify Playlist", "Recherche", etc.

    @property
    def is_playlist(self) -> bool:
        return len(self.tracks) > 1


class Resolver:
    """Résout une entrée utilisateur en Track(s) jouables."""

    def __init__(self, spotify: SpotifyClient | None = None) -> None:
        self.spotify = spotify or SpotifyClient()

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    async def resolve(self, query: str, requester: str | None = None) -> ResolvedBatch:
        """Point d'entrée principal. Détecte le type d'entrée et dispatch."""
        query = query.strip()

        # 1/2. YouTube
        if _YT_PLAYLIST.search(query) or (_YT_TRACK.search(query) and "list=" in query):
            return await self._resolve_youtube_playlist(query, requester)
        if _YT_TRACK.search(query):
            return await self._resolve_youtube_track(query, requester)

        # 3. Spotify track
        if m := _SP_TRACK.search(query):
            return await self._resolve_spotify_track(m.group(1), requester)

        # 4. Spotify playlist / album
        if m := _SP_PLAYLIST.search(query):
            return await self._resolve_spotify_playlist(m.group(1), requester)
        if m := _SP_ALBUM.search(query):
            return await self._resolve_spotify_album(m.group(1), requester)

        # 5. Texte libre → ytsearch
        return await self._resolve_search(query, requester)

    async def search_youtube(self, query: str, limit: int = 5) -> list[dict]:
        """Recherche YouTube — retourne `limit` résultats bruts (pour !search)."""

        def _run() -> list[dict]:
            opts = {**YDL_OPTS_BASE, "extract_flat": True}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
            entries = (info or {}).get("entries") or []
            return entries[:limit]

        return await asyncio.to_thread(_run)

    # ------------------------------------------------------------------
    # YouTube
    # ------------------------------------------------------------------

    async def _resolve_youtube_track(self, url: str, requester: str | None) -> ResolvedBatch:
        info = await asyncio.to_thread(self._ydl_extract, url, flat=False)
        track = self._info_to_track(info, requester)
        return ResolvedBatch(tracks=[track] if track else [], source_label="YouTube")

    async def _resolve_youtube_playlist(self, url: str, requester: str | None) -> ResolvedBatch:
        """Extrait toutes les pistes d'une playlist en mode `extract_flat=True` (rapide)."""
        info = await asyncio.to_thread(self._ydl_extract, url, flat=True)
        entries = (info or {}).get("entries") or []
        tracks: list["Track"] = []
        for entry in entries:
            if not entry:
                continue
            t = self._info_to_track(entry, requester, lazy=True)
            if t:
                tracks.append(t)
        return ResolvedBatch(tracks=tracks, source_label="YouTube Playlist")

    # ------------------------------------------------------------------
    # Spotify
    # ------------------------------------------------------------------

    async def _resolve_spotify_track(self, track_id: str, requester: str | None) -> ResolvedBatch:
        if not self.spotify.enabled:
            raise RuntimeError("Spotify n'est pas configuré (vérifie .env).")
        info = await asyncio.to_thread(self.spotify.get_track, track_id)
        if not info:
            return ResolvedBatch(tracks=[], source_label="Spotify")
        track = await self._spotify_to_track(info, requester)
        return ResolvedBatch(tracks=[track] if track else [], source_label="Spotify")

    async def _resolve_spotify_playlist(
        self, playlist_id: str, requester: str | None
    ) -> ResolvedBatch:
        if not self.spotify.enabled:
            raise RuntimeError("Spotify n'est pas configuré (vérifie .env).")
        sp_tracks = await asyncio.to_thread(self.spotify.get_playlist_tracks, playlist_id)
        tracks = await self._spotify_batch_to_tracks(sp_tracks, requester)
        return ResolvedBatch(tracks=tracks, source_label="Spotify Playlist")

    async def _resolve_spotify_album(self, album_id: str, requester: str | None) -> ResolvedBatch:
        if not self.spotify.enabled:
            raise RuntimeError("Spotify n'est pas configuré (vérifie .env).")
        sp_tracks = await asyncio.to_thread(self.spotify.get_album_tracks, album_id)
        tracks = await self._spotify_batch_to_tracks(sp_tracks, requester)
        return ResolvedBatch(tracks=tracks, source_label="Spotify Album")

    async def _spotify_to_track(
        self, info: SpotifyTrackInfo, requester: str | None
    ) -> "Track | None":
        """Crée une Track lazy à partir de métadonnées Spotify (pas de résolution YouTube ici)."""
        from .player import Track

        return Track(
            title=f"{info.artist} - {info.title}",
            url="",  # à résoudre via ytsearch au moment de la lecture
            stream_url=None,
            duration=info.duration_s,
            uploader=info.artist,
            thumbnail=None,
            requester=requester,
            search_query=info.search_query,
            lazy=True,
        )

    async def _spotify_batch_to_tracks(
        self, sp_tracks: list[SpotifyTrackInfo], requester: str | None
    ) -> list["Track"]:
        out: list["Track"] = []
        for info in sp_tracks:
            t = await self._spotify_to_track(info, requester)
            if t:
                out.append(t)
        return out

    # ------------------------------------------------------------------
    # Recherche texte libre
    # ------------------------------------------------------------------

    async def _resolve_search(self, query: str, requester: str | None) -> ResolvedBatch:
        info = await asyncio.to_thread(self._ydl_extract, f"ytsearch1:{query}", flat=False)
        # ytsearch retourne un dict avec entries
        entries = (info or {}).get("entries") or [info]
        first = next((e for e in entries if e), None)
        track = self._info_to_track(first, requester)
        return ResolvedBatch(tracks=[track] if track else [], source_label="Recherche")

    # ------------------------------------------------------------------
    # Résolution lazy (appelée par GuildPlayer juste avant lecture)
    # ------------------------------------------------------------------

    async def resolve_stream(self, track: "Track") -> bool:
        """Remplit `stream_url` (et complète les métadonnées) pour une track lazy.

        Retourne True si succès, False sinon.
        """
        if track.stream_url and not track.lazy:
            return True

        query = track.search_query or track.url
        if not query:
            return False

        try:
            info = await asyncio.to_thread(self._ydl_extract, query, flat=False)
        except Exception as exc:
            log.warning("Échec résolution stream pour %r : %s", track.title, exc)
            return False

        # ytsearch wrap dans entries
        if info and "entries" in info:
            entries = info["entries"] or []
            info = next((e for e in entries if e), None)

        if not info:
            return False

        track.stream_url = info.get("url")
        track.http_headers = info.get("http_headers")
        track.url = info.get("webpage_url") or track.url
        track.duration = info.get("duration") or track.duration
        track.thumbnail = info.get("thumbnail") or track.thumbnail
        if not track.uploader:
            track.uploader = info.get("uploader")
        if not track.title or track.lazy:
            track.title = info.get("title") or track.title
        track.lazy = False
        return bool(track.stream_url)

    # ------------------------------------------------------------------
    # Internes yt-dlp
    # ------------------------------------------------------------------

    def _ydl_extract(self, query: str, *, flat: bool) -> dict | None:
        opts = dict(YDL_OPTS_BASE)
        if flat:
            opts["extract_flat"] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(query, download=False)

    def _info_to_track(
        self, info: dict | None, requester: str | None, *, lazy: bool = False
    ) -> "Track | None":
        from .player import Track

        if not info:
            return None
        title = info.get("title") or "Titre inconnu"
        url = info.get("webpage_url") or info.get("url") or ""
        stream_url = None if lazy else info.get("url")
        # Pour les entrées extract_flat, "url" est l'ID/URL de la page, pas un stream
        if lazy:
            stream_url = None
            # Si on a juste un ID, reconstruit l'URL
            if url and not url.startswith("http"):
                url = f"https://www.youtube.com/watch?v={url}"

        return Track(
            title=title,
            url=url,
            stream_url=stream_url,
            http_headers=info.get("http_headers") if stream_url else None,
            duration=info.get("duration"),
            uploader=info.get("uploader") or info.get("channel"),
            thumbnail=info.get("thumbnail"),
            requester=requester,
            search_query=url if lazy else None,
            lazy=lazy,
        )
