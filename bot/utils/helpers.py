"""Fonctions utilitaires : formatage durée, barres de progression, etc."""

from __future__ import annotations


def format_duration(seconds: float | int | None) -> str:
    """Formate une durée en secondes vers `HH:MM:SS` ou `MM:SS`.

    Retourne `--:--` si la durée est inconnue (None, 0, négative).
    """
    if seconds is None or seconds <= 0:
        return "--:--"

    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def progress_bar(current: float, total: float, length: int = 20) -> str:
    """Construit une barre de progression ASCII.

    Exemple : `▬▬▬▬🔘▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬`
    """
    if total <= 0:
        return "🔘" + "▬" * (length - 1)

    ratio = max(0.0, min(1.0, current / total))
    position = int(ratio * (length - 1))
    bar_chars = ["▬"] * length
    bar_chars[position] = "🔘"
    return "".join(bar_chars)


def chunk_list(items: list, size: int) -> list[list]:
    """Découpe une liste en sous-listes de taille `size`."""
    return [items[i : i + size] for i in range(0, len(items), size)]


def truncate(text: str, max_length: int = 80) -> str:
    """Tronque une chaîne avec ellipse si elle dépasse `max_length`."""
    if len(text) <= max_length:
        return text
    return text[: max_length - 1].rstrip() + "…"
