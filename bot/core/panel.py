"""Panneau de contrôle persistant — embed live + boutons + modals + ephemeral.

Mode music-only : posé via `!setup`, retiré via `!unsetup`.
Le channel devient un panel-only (auto-delete des autres messages).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord

from bot.utils import embeds
from bot.utils.help_command import HELP_GROUPS
from bot.utils.lyrics import fetch_lyrics, guess_artist_title, split_lyrics

if TYPE_CHECKING:
    from bot.cogs.music import Music
    from .player import GuildPlayer

log = logging.getLogger(__name__)

REFRESH_INTERVAL_PLAYING = 5.0
REFRESH_INTERVAL_IDLE = 30.0
EPHEMERAL_CHUNK = 1800  # taille de chunk pour les lyrics ephemeral


# ======================================================================
# Modals
# ======================================================================


class AddTrackModal(discord.ui.Modal):
    """Modal ➕ : ajout d'une piste par URL ou recherche libre."""

    def __init__(self, panel: "MusicPanel") -> None:
        super().__init__(title="Ajouter une piste à la queue")
        self.panel = panel
        self.add_item(
            discord.ui.InputText(
                label="URL ou recherche libre",
                placeholder="https://… ou « daft punk get lucky »",
                required=True,
                max_length=500,
            )
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        query = (self.children[0].value or "").strip()
        if not query:
            await interaction.response.send_message("Requête vide.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self.panel.handle_add_query(interaction, query)


class SearchModal(discord.ui.Modal):
    """Modal 🔍 : recherche YT, présente 5 résultats en ephemeral."""

    def __init__(self, panel: "MusicPanel") -> None:
        super().__init__(title="Rechercher sur YouTube")
        self.panel = panel
        self.add_item(
            discord.ui.InputText(
                label="Recherche",
                placeholder="« lofi hip hop », « daft punk », …",
                required=True,
                max_length=200,
            )
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        query = (self.children[0].value or "").strip()
        if not query:
            await interaction.response.send_message("Requête vide.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self.panel.handle_search_query(interaction, query)


# ======================================================================
# Vue des résultats de recherche (5 boutons numérotés)
# ======================================================================


class SearchResultsView(discord.ui.View):
    """5 boutons numérotés pour choisir un résultat de recherche."""

    def __init__(self, panel: "MusicPanel", results: list[dict]) -> None:
        super().__init__(timeout=60)
        self.panel = panel
        self.results = results
        for idx in range(min(5, len(results))):
            self.add_item(_SearchChoiceButton(panel, results[idx], idx + 1))


class _SearchChoiceButton(discord.ui.Button):
    def __init__(self, panel: "MusicPanel", result: dict, position: int) -> None:
        super().__init__(
            label=str(position),
            style=discord.ButtonStyle.primary,
            custom_id=f"search_choice:{position}",
        )
        self.panel = panel
        self.result = result

    async def callback(self, interaction: discord.Interaction) -> None:
        url = self.result.get("url") or self.result.get("webpage_url") or ""
        if url and not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={url}"
        await interaction.response.defer(ephemeral=True)
        await self.panel.handle_add_query(interaction, url, source_label="Recherche")


# ======================================================================
# Vue principale du panneau (contrôles + modals + infos)
# ======================================================================


class MusicPanelView(discord.ui.View):
    """View persistante (timeout=None) avec tous les contrôles musicaux."""

    def __init__(self, panel: "MusicPanel") -> None:
        super().__init__(timeout=None)
        self.panel = panel

    @property
    def player(self) -> "GuildPlayer":
        return self.panel.player

    # --- helpers internes -----------------------------------------------

    async def _need_voice(self, interaction: discord.Interaction) -> bool:
        if self.player.voice_client is None or not self.player.voice_client.is_connected():
            await interaction.response.send_message(
                "❌ Le bot n'est pas dans un salon vocal.", ephemeral=True
            )
            return False
        return True

    async def _ack_and_refresh(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer()
        await self.panel.refresh()

    # ============ ligne 0 : contrôles de lecture ========================

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.primary,
                       custom_id="panel:playpause", row=0)
    async def play_pause(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        if not await self._need_voice(interaction):
            return
        vc = self.player.voice_client
        if vc.is_paused():
            self.player.resume()
        elif vc.is_playing():
            self.player.pause()
        await self._ack_and_refresh(interaction)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary,
                       custom_id="panel:skip", row=0)
    async def skip(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        if not await self._need_voice(interaction):
            return
        self.player.skip()
        await interaction.response.defer()
        await asyncio.sleep(0.6)
        await self.panel.refresh()

    @discord.ui.button(emoji="🔂", style=discord.ButtonStyle.secondary,
                       custom_id="panel:loop", row=0)
    async def loop(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        self.player.loop = not self.player.loop
        if self.player.loop:
            self.player.loop_queue = False
        await self._ack_and_refresh(interaction)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary,
                       custom_id="panel:loopqueue", row=0)
    async def loop_queue(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        self.player.loop_queue = not self.player.loop_queue
        if self.player.loop_queue:
            self.player.loop = False
        await self._ack_and_refresh(interaction)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary,
                       custom_id="panel:shuffle", row=0)
    async def shuffle(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        if not self.player.queue:
            await interaction.response.send_message(
                "Queue vide, rien à mélanger.", ephemeral=True
            )
            return
        self.player.shuffle()
        await self._ack_and_refresh(interaction)

    # ============ ligne 1 : ajout / recherche / infos ====================

    @discord.ui.button(label="Ajouter", emoji="➕", style=discord.ButtonStyle.success,
                       custom_id="panel:add", row=1)
    async def add(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        modal = AddTrackModal(self.panel)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Search", emoji="🔍", style=discord.ButtonStyle.success,
                       custom_id="panel:search", row=1)
    async def search(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        modal = SearchModal(self.panel)
        await interaction.response.send_modal(modal)

    @discord.ui.button(emoji="📜", style=discord.ButtonStyle.secondary,
                       custom_id="panel:lyrics", row=1)
    async def lyrics(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.panel.send_lyrics_ephemeral(interaction)

    @discord.ui.button(emoji="❓", style=discord.ButtonStyle.secondary,
                       custom_id="panel:help", row=1)
    async def help(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.panel.send_help_ephemeral(interaction)

    # ============ ligne 2 : actions destructives + sortie ================

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger,
                       custom_id="panel:stop", row=2)
    async def stop(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        if not await self._need_voice(interaction):
            return
        await self.player.stop()
        await self._ack_and_refresh(interaction)

    @discord.ui.button(emoji="🚪", style=discord.ButtonStyle.danger,
                       custom_id="panel:leave", row=2)
    async def leave(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        if self.player.voice_client is None:
            await interaction.response.send_message("❌ Pas connecté.", ephemeral=True)
            return
        await self.player.disconnect()
        await self._ack_and_refresh(interaction)


# ======================================================================
# Panel
# ======================================================================


class MusicPanel:
    """Gère le message-panneau persistant + dispatch des actions des boutons."""

    def __init__(
        self,
        player: "GuildPlayer",
        channel: discord.TextChannel,
        bot: discord.Client,
        music_cog: "Music",
    ) -> None:
        self.player = player
        self.channel = channel
        self.bot = bot
        self.music = music_cog
        self.message: discord.Message | None = None
        self.view: MusicPanelView | None = None
        self._refresh_task: asyncio.Task | None = None
        self._refresh_lock = asyncio.Lock()

    # --- Cycle de vie -------------------------------------------------

    async def install(self) -> discord.Message:
        self.view = MusicPanelView(self)
        embed = embeds.panel(self.player)
        self.message = await self.channel.send(embed=embed, view=self.view)
        self.start_refresh()
        return self.message

    async def remove(self) -> None:
        self.stop_refresh()
        if self.message:
            try:
                await self.message.delete()
            except discord.NotFound:
                pass
            except Exception as exc:
                log.warning("Suppression panel KO : %s", exc)
        self.message = None
        self.view = None

    # --- Refresh ------------------------------------------------------

    async def refresh(self) -> None:
        if self.message is None:
            return
        async with self._refresh_lock:
            try:
                embed = embeds.panel(self.player)
                await self.message.edit(embed=embed, view=self.view)
            except discord.NotFound:
                log.info("Panel message supprimé, arrêt du refresh.")
                self.message = None
                self.stop_refresh()
            except discord.HTTPException as exc:
                log.warning("Panel refresh HTTP KO : %s", exc)

    def start_refresh(self) -> None:
        if self._refresh_task and not self._refresh_task.done():
            return
        self._refresh_task = self.bot.loop.create_task(self._refresh_loop())

    def stop_refresh(self) -> None:
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()

    async def _refresh_loop(self) -> None:
        try:
            while self.message is not None:
                interval = (
                    REFRESH_INTERVAL_PLAYING
                    if self.player.current is not None
                    else REFRESH_INTERVAL_IDLE
                )
                await asyncio.sleep(interval)
                await self.refresh()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.exception("Refresh loop panel KO : %s", exc)

    # ==================================================================
    # Handlers d'actions (appelés par les modals et boutons)
    # ==================================================================

    async def _ensure_voice_for_interaction(
        self, interaction: discord.Interaction
    ) -> bool:
        """Si le bot n'est pas en vocal, tente de rejoindre celui de l'user."""
        if self.player.voice_client and self.player.voice_client.is_connected():
            return True
        member = (
            interaction.guild.get_member(interaction.user.id)
            if interaction.guild
            else None
        )
        if not member or not member.voice or not member.voice.channel:
            await interaction.followup.send(
                "❌ Tu dois être dans un salon vocal d'abord.", ephemeral=True
            )
            return False
        target = member.voice.channel
        try:
            vc = await target.connect()
            await interaction.guild.change_voice_state(channel=target, self_deaf=True)
            self.player.voice_client = vc
            self.player.text_channel = self.channel
            self.player.start()
        except Exception as exc:
            log.exception("Connexion vocale via panel KO")
            await interaction.followup.send(
                f"❌ Connexion vocale impossible : {exc}", ephemeral=True
            )
            return False
        return True

    async def handle_add_query(
        self,
        interaction: discord.Interaction,
        query: str,
        *,
        source_label: str | None = None,
    ) -> None:
        """Logique partagée entre AddTrackModal et SearchResultsView."""
        if not await self._ensure_voice_for_interaction(interaction):
            return
        try:
            batch = await self.music.resolver.resolve(
                query, requester=interaction.user.display_name
            )
        except RuntimeError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        except Exception as exc:
            log.exception("resolve via panel KO")
            await interaction.followup.send(
                f"❌ Résolution impossible : {exc}", ephemeral=True
            )
            return

        if not batch.tracks:
            await interaction.followup.send("❌ Aucun résultat.", ephemeral=True)
            return

        if batch.is_playlist:
            self.player.enqueue_many(batch.tracks)
            msg = (
                f"✅ **{len(batch.tracks)}** pistes ajoutées depuis "
                f"**{source_label or batch.source_label}**."
            )
        else:
            track = batch.tracks[0]
            pos = self.player.enqueue(track)
            msg = f"✅ Ajouté à la queue (#{pos}) : **{track.title}**"

        self.player.notify_play()
        await self.refresh()
        await interaction.followup.send(msg, ephemeral=True)

    async def handle_search_query(
        self,
        interaction: discord.Interaction,
        query: str,
    ) -> None:
        try:
            results = await self.music.resolver.search_youtube(query, limit=5)
        except Exception as exc:
            await interaction.followup.send(f"❌ Recherche KO : {exc}", ephemeral=True)
            return
        if not results:
            await interaction.followup.send("❌ Aucun résultat.", ephemeral=True)
            return
        view = SearchResultsView(self, results)
        embed = embeds.search_results(query, results)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    async def send_lyrics_ephemeral(self, interaction: discord.Interaction) -> None:
        if self.player.current is None:
            await interaction.followup.send(
                "❌ Aucune piste en lecture.", ephemeral=True
            )
            return
        artist, title = guess_artist_title(self.player.current)
        try:
            lyrics_text = await fetch_lyrics(artist, title)
        except Exception as exc:
            await interaction.followup.send(
                f"❌ API lyrics indisponible : {exc}", ephemeral=True
            )
            return
        if not lyrics_text:
            await interaction.followup.send(
                f"❌ Paroles introuvables pour **{artist or '?'} — {title}**.",
                ephemeral=True,
            )
            return
        header = f"🎤 **{artist or '?'} — {title}**"
        chunks = split_lyrics(lyrics_text, EPHEMERAL_CHUNK)
        # Premier chunk avec header
        first_embed = embeds.info(chunks[0])
        await interaction.followup.send(
            content=header, embed=first_embed, ephemeral=True
        )
        for extra in chunks[1:]:
            await interaction.followup.send(
                embed=embeds.info(extra), ephemeral=True
            )

    async def send_help_ephemeral(self, interaction: discord.Interaction) -> None:
        bot = self.bot
        prefix = "!"
        embed = discord.Embed(
            title="🎵 Respawn Radio — Aide",
            description=(
                "Tu peux tout faire via les **boutons du panneau** "
                f"OU en tapant les commandes (`{prefix}help <cmd>` pour le détail)."
            ),
            color=embeds.COLOR_ACCENT,
        )
        for group_name, cmd_names in HELP_GROUPS.items():
            lines = []
            for name in cmd_names:
                cmd = bot.get_command(name)
                if cmd is None or cmd.hidden:
                    continue
                short = (cmd.help or "").strip().split("\n")[0] or "—"
                lines.append(f"`{prefix}{name}` — {short}")
            if lines:
                embed.add_field(name=group_name, value="\n".join(lines), inline=False)
        embed.set_footer(text="Respawn Radio")
        await interaction.followup.send(embed=embed, ephemeral=True)
