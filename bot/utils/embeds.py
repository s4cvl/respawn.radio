"""Builders d'embeds Discord — style cohérent, accent orange."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .helpers import format_duration, progress_bar, truncate

if TYPE_CHECKING:
    from bot.core.player import GuildPlayer, Track


# Palette
COLOR_ACCENT = 0xFF7A00   # Orange Respawn Radio
COLOR_SUCCESS = 0x57F287
COLOR_ERROR = 0xED4245
COLOR_INFO = 0x5865F2

BOT_NAME = "Respawn Radio"


def _base(color: int, title: str | None = None, description: str | None = None) -> discord.Embed:
    embed = discord.Embed(color=color)
    if title:
        embed.title = title
    if description:
        embed.description = description
    embed.set_footer(text=BOT_NAME)
    return embed


def error(message: str, *, title: str = "Erreur") -> discord.Embed:
    return _base(COLOR_ERROR, title=f"❌ {title}", description=message)


def success(message: str, *, title: str | None = None) -> discord.Embed:
    return _base(COLOR_SUCCESS, title=f"✅ {title}" if title else None, description=message)


def info(message: str, *, title: str | None = None) -> discord.Embed:
    return _base(COLOR_INFO, title=title, description=message)


def track_added(track: "Track", position: int) -> discord.Embed:
    embed = _base(
        COLOR_ACCENT,
        title="➕ Ajouté à la queue",
        description=f"**[{truncate(track.title, 90)}]({track.url})**",
    )
    embed.add_field(name="Durée", value=format_duration(track.duration), inline=True)
    embed.add_field(name="Position", value=f"#{position}", inline=True)
    if track.requester:
        embed.add_field(name="Demandé par", value=track.requester, inline=True)
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    return embed


def playlist_added(count: int, source: str) -> discord.Embed:
    return _base(
        COLOR_ACCENT,
        title="📃 Playlist ajoutée",
        description=f"✅ **{count}** pistes ajoutées à la queue depuis **{source}**.",
    )


def now_playing(player: "GuildPlayer") -> discord.Embed:
    track = player.current
    if track is None:
        return info("Aucune piste en lecture.", title="🔇 Silence radio")

    elapsed = player.elapsed_seconds()
    bar = progress_bar(elapsed, track.duration or 0)
    elapsed_str = format_duration(elapsed)
    total_str = format_duration(track.duration)

    embed = _base(
        COLOR_ACCENT,
        title="🎵 En cours de lecture",
        description=f"**[{truncate(track.title, 90)}]({track.url})**",
    )
    embed.add_field(
        name="Progression",
        value=f"`{elapsed_str}` {bar} `{total_str}`",
        inline=False,
    )
    if track.uploader:
        embed.add_field(name="Artiste / Chaîne", value=track.uploader, inline=True)
    if track.requester:
        embed.add_field(name="Demandé par", value=track.requester, inline=True)

    flags = []
    if player.loop:
        flags.append("🔂 Loop track")
    if player.loop_queue:
        flags.append("🔁 Loop queue")
    if flags:
        embed.add_field(name="Modes", value=" • ".join(flags), inline=False)

    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    return embed


def queue(player: "GuildPlayer", page: int = 1, per_page: int = 10) -> discord.Embed:
    items = player.queue
    total_pages = max(1, (len(items) + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))

    start = (page - 1) * per_page
    end = start + per_page
    page_items = items[start:end]

    if not items and player.current is None:
        return info("La queue est vide.", title="🗒️ Queue")

    description_lines = []
    if player.current:
        description_lines.append(
            f"**En cours :** [{truncate(player.current.title, 70)}]({player.current.url})"
        )
        description_lines.append("")

    if page_items:
        description_lines.append("**À venir :**")
        for idx, track in enumerate(page_items, start=start + 1):
            dur = format_duration(track.duration)
            description_lines.append(
                f"`{idx:>2}.` [{truncate(track.title, 60)}]({track.url}) — `{dur}`"
            )
    else:
        description_lines.append("*(plus rien en attente)*")

    total_duration = sum((t.duration or 0) for t in items)
    embed = _base(COLOR_ACCENT, title="🗒️ Queue", description="\n".join(description_lines))
    embed.set_footer(
        text=(
            f"{BOT_NAME} • Page {page}/{total_pages} • "
            f"{len(items)} piste(s) • Durée totale : {format_duration(total_duration)}"
        )
    )
    return embed


def skip_summary(player: "GuildPlayer", *, skipped: "Track | None" = None) -> discord.Embed:
    """Affiché après un skip : nouvelle piste + 10 prochaines."""
    if player.current is None:
        embed = _base(
            COLOR_ACCENT,
            title="⏭️ Skip",
            description="*Plus rien dans la queue.*",
        )
        if skipped:
            embed.set_footer(text=f"{BOT_NAME} • Skip : {truncate(skipped.title, 80)}")
        return embed

    track = player.current
    elapsed = player.elapsed_seconds()
    bar = progress_bar(elapsed, track.duration or 0)
    embed = _base(
        COLOR_ACCENT,
        title="⏭️ Currently playing",
        description=f"**[{truncate(track.title, 90)}]({track.url})**",
    )
    embed.add_field(
        name="Progression",
        value=f"`{format_duration(elapsed)}` {bar} `{format_duration(track.duration)}`",
        inline=False,
    )
    if track.requester:
        embed.add_field(name="Demandé par", value=track.requester, inline=True)

    if player.queue:
        lines = []
        for idx, t in enumerate(player.queue[:10], start=1):
            lines.append(
                f"`{idx:>2}.` [{truncate(t.title, 55)}]({t.url}) `{format_duration(t.duration)}`"
            )
        more = len(player.queue) - 10
        if more > 0:
            lines.append(f"*… + {more} autres pistes*")
        embed.add_field(name="🗒️ À venir", value="\n".join(lines), inline=False)

    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)

    if skipped:
        embed.set_footer(text=f"{BOT_NAME} • Skip : {truncate(skipped.title, 80)}")
    return embed


def panel(player: "GuildPlayer") -> discord.Embed:
    """Panneau de contrôle persistant (commande !setup).

    Inclut now playing, barre de progression, queue résumée et statut des modes.
    """
    if player.current is None:
        embed = _base(
            COLOR_ACCENT,
            title="🎛️ Respawn Radio — Panneau de contrôle",
            description="*Aucune piste en lecture.*\n\nUtilise `!play <url|nom>` pour démarrer.",
        )
    else:
        track = player.current
        elapsed = player.elapsed_seconds()
        bar = progress_bar(elapsed, track.duration or 0)
        embed = _base(
            COLOR_ACCENT,
            title="🎛️ Respawn Radio — En lecture",
            description=f"**[{truncate(track.title, 90)}]({track.url})**",
        )
        embed.add_field(
            name="Progression",
            value=f"`{format_duration(elapsed)}` {bar} `{format_duration(track.duration)}`",
            inline=False,
        )
        info_lines = []
        if track.uploader:
            info_lines.append(f"**Artiste / Chaîne :** {track.uploader}")
        if track.requester:
            info_lines.append(f"**Demandé par :** {track.requester}")
        if info_lines:
            embed.add_field(name="​", value="\n".join(info_lines), inline=False)
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)

    if player.queue:
        lines = []
        for idx, t in enumerate(player.queue[:10], start=1):
            lines.append(
                f"`{idx:>2}.` {truncate(t.title, 55)} `{format_duration(t.duration)}`"
            )
        more = len(player.queue) - 10
        if more > 0:
            lines.append(f"*… + {more} autres pistes*")
        embed.add_field(name="🗒️ Queue", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="🗒️ Queue", value="*(vide)*", inline=False)

    flags = []
    if player.loop:
        flags.append("🔂 Loop")
    if player.loop_queue:
        flags.append("🔁 LoopQueue")
    flags.append(f"🔊 {int(player.volume * 100)}%")
    paused = bool(player.voice_client and player.voice_client.is_paused())
    if paused:
        flags.append("⏸ Pause")
    embed.set_footer(text=f"{BOT_NAME} • " + " • ".join(flags))
    return embed


def search_results(query: str, results: list[dict]) -> discord.Embed:
    embed = _base(
        COLOR_ACCENT,
        title=f"🔍 Résultats pour « {truncate(query, 60)} »",
        description="Réagis avec le numéro pour ajouter à la queue.",
    )
    for idx, r in enumerate(results, start=1):
        title = truncate(r.get("title") or "Titre inconnu", 70)
        url = r.get("url") or r.get("webpage_url") or ""
        dur = format_duration(r.get("duration"))
        uploader = r.get("uploader") or "—"
        embed.add_field(
            name=f"{idx}. {title}",
            value=f"{uploader} • `{dur}`\n[lien]({url})",
            inline=False,
        )
    return embed
