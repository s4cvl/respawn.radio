"""Wrapper minimal autour de spotipy pour résoudre tracks et playlists Spotify."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SpotifyTrackInfo:
    """Métadonnées extraites d'une track Spotify pour recherche YouTube."""

    title: str
    artist: str
    duration_s: float | None = None

    @property
    def search_query(self) -> str:
        """Requête à envoyer à yt-dlp (`ytsearch:`)."""
        return f"{self.artist} - {self.title}"


class SpotifyClient:
    """Client Spotify (Client Credentials flow — lecture publique uniquement)."""

    def __init__(self, client_id: str | None = None, client_secret: str | None = None) -> None:
        client_id = client_id or os.getenv("SPOTIFY_CLIENT_ID")
        client_secret = client_secret or os.getenv("SPOTIFY_CLIENT_SECRET")

        if not client_id or not client_secret:
            log.warning("SPOTIFY_CLIENT_ID/SECRET non configurés — résolution Spotify désactivée.")
            self._sp: spotipy.Spotify | None = None
            return

        auth = SpotifyClientCredentials(client_id=client_id, client_secret=client_secret)
        self._sp = spotipy.Spotify(auth_manager=auth)

    @property
    def enabled(self) -> bool:
        return self._sp is not None

    def get_track(self, track_id: str) -> SpotifyTrackInfo | None:
        if not self._sp:
            return None
        try:
            data = self._sp.track(track_id)
        except Exception as exc:  # spotipy.SpotifyException, network, etc.
            log.error("Échec récupération track Spotify %s : %s", track_id, exc)
            return None
        return _track_from_payload(data)

    def get_playlist_tracks(self, playlist_id: str, limit: int | None = None) -> list[SpotifyTrackInfo]:
        """Récupère toutes les tracks d'une playlist (pagine si > 100)."""
        if not self._sp:
            return []
        out: list[SpotifyTrackInfo] = []
        try:
            results = self._sp.playlist_items(playlist_id, additional_types=("track",))
        except Exception as exc:
            log.error("Échec récupération playlist Spotify %s : %s", playlist_id, exc)
            return []

        while results:
            for item in results.get("items", []):
                track = item.get("track")
                if not track:
                    continue
                info = _track_from_payload(track)
                if info:
                    out.append(info)
                    if limit and len(out) >= limit:
                        return out
            if results.get("next"):
                results = self._sp.next(results)
            else:
                break
        return out

    def get_album_tracks(self, album_id: str) -> list[SpotifyTrackInfo]:
        if not self._sp:
            return []
        try:
            album = self._sp.album(album_id)
        except Exception as exc:
            log.error("Échec récupération album Spotify %s : %s", album_id, exc)
            return []
        artist = (album.get("artists") or [{}])[0].get("name", "")
        out: list[SpotifyTrackInfo] = []
        for item in album.get("tracks", {}).get("items", []):
            title = item.get("name") or ""
            dur_ms = item.get("duration_ms")
            duration = (dur_ms / 1000) if dur_ms else None
            if title:
                out.append(SpotifyTrackInfo(title=title, artist=artist, duration_s=duration))
        return out


def _track_from_payload(track: dict | None) -> SpotifyTrackInfo | None:
    if not track:
        return None
    title = track.get("name")
    artists = track.get("artists") or []
    artist = ", ".join(a.get("name", "") for a in artists if a.get("name"))
    dur_ms = track.get("duration_ms")
    duration = (dur_ms / 1000) if dur_ms else None
    if not title or not artist:
        return None
    return SpotifyTrackInfo(title=title, artist=artist, duration_s=duration)
