"""Wrapper lyrics.ovh — gratuit, sans clé."""

from __future__ import annotations

import urllib.parse

import aiohttp

LYRICS_OVH_URL = "https://api.lyrics.ovh/v1/{artist}/{title}"


async def fetch_lyrics(artist: str, title: str) -> str | None:
    """Récupère les paroles via lyrics.ovh. None si introuvable ou erreur."""
    artist_enc = urllib.parse.quote(artist, safe="")
    title_enc = urllib.parse.quote(title, safe="")
    url = LYRICS_OVH_URL.format(artist=artist_enc, title=title_enc)
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            return (data.get("lyrics") or "").strip() or None


def split_lyrics(text: str, chunk_size: int) -> list[str]:
    """Découpe les paroles par lignes, sans casser au milieu d'un mot."""
    lines = text.split("\n")
    chunks: list[str] = []
    buf: list[str] = []
    current = 0
    for line in lines:
        if current + len(line) + 1 > chunk_size and buf:
            chunks.append("\n".join(buf))
            buf = []
            current = 0
        buf.append(line)
        current += len(line) + 1
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def guess_artist_title(track) -> tuple[str, str]:
    """À partir d'une Track, devine (artist, title) pour la requête lyrics."""
    if " - " in track.title:
        artist, title = (s.strip() for s in track.title.split(" - ", 1))
        return artist, title
    artist = track.uploader or ""
    return artist, track.title
