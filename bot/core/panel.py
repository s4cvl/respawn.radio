"""Panneau de contrôle persistant — un embed live + 7 boutons par guild.

Posé via `!setup`, retiré via `!unsetup`. Auto-refresh toutes les 5s
quand une piste joue, plus refresh immédiat sur clic ou changement de piste.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord

from bot.utils import embeds

if TYPE_CHECKING:
    from .player import GuildPlayer

log = logging.getLogger(__name__)

REFRESH_INTERVAL_PLAYING = 5.0  # secondes quand une piste joue
REFRESH_INTERVAL_IDLE = 30.0    # secondes quand rien ne joue


# ----------------------------------------------------------------------
# View (boutons)
# ----------------------------------------------------------------------


class MusicPanelView(discord.ui.View):
    """Vue persistante avec les contrôles musicaux."""

    def __init__(self, player: "GuildPlayer", panel: "MusicPanel") -> None:
        super().__init__(timeout=None)  # persistante
        self.player = player
        self.panel = panel

    # --- Helpers ------------------------------------------------------

    async def _ensure_connected(self, interaction: discord.Interaction) -> bool:
        if self.player.voice_client is None or not self.player.voice_client.is_connected():
            await interaction.response.send_message(
                "❌ Pas connecté à un vocal.", ephemeral=True
            )
            return False
        return True

    async def _ack_and_refresh(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer()
        await self.panel.refresh()

    # --- Boutons (ligne 1 : contrôles) --------------------------------

    @discord.ui.button(
        emoji="⏯️", style=discord.ButtonStyle.primary, custom_id="panel:playpause", row=0
    )
    async def play_pause(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        if not await self._ensure_connected(interaction):
            return
        vc = self.player.voice_client
        if vc.is_paused():
            self.player.resume()
        elif vc.is_playing():
            self.player.pause()
        await self._ack_and_refresh(interaction)

    @discord.ui.button(
        emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="panel:skip", row=0
    )
    async def skip(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        if not await self._ensure_connected(interaction):
            return
        self.player.skip()
        await interaction.response.defer()
        await asyncio.sleep(0.6)  # laisse la nouvelle track se sélectionner
        await self.panel.refresh()

    @discord.ui.button(
        emoji="🔂", style=discord.ButtonStyle.secondary, custom_id="panel:loop", row=0
    )
    async def loop(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        self.player.loop = not self.player.loop
        if self.player.loop:
            self.player.loop_queue = False
        await self._ack_and_refresh(interaction)

    @discord.ui.button(
        emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="panel:loopqueue", row=0
    )
    async def loop_queue(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        self.player.loop_queue = not self.player.loop_queue
        if self.player.loop_queue:
            self.player.loop = False
        await self._ack_and_refresh(interaction)

    @discord.ui.button(
        emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="panel:shuffle", row=0
    )
    async def shuffle(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        if not self.player.queue:
            await interaction.response.send_message(
                "ℹ️ Queue vide, rien à mélanger.", ephemeral=True
            )
            return
        self.player.shuffle()
        await self._ack_and_refresh(interaction)

    # --- Boutons (ligne 2 : actions destructives + sortie) ------------

    @discord.ui.button(
        emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="panel:stop", row=1
    )
    async def stop(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        if not await self._ensure_connected(interaction):
            return
        await self.player.stop()
        await self._ack_and_refresh(interaction)

    @discord.ui.button(
        emoji="🚪", style=discord.ButtonStyle.danger, custom_id="panel:leave", row=1
    )
    async def leave(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        if self.player.voice_client is None:
            await interaction.response.send_message("❌ Pas connecté.", ephemeral=True)
            return
        await self.player.disconnect()
        await self._ack_and_refresh(interaction)


# ----------------------------------------------------------------------
# Panel
# ----------------------------------------------------------------------


class MusicPanel:
    """Gère un message-panneau persistant pour une guild."""

    def __init__(
        self,
        player: "GuildPlayer",
        channel: discord.TextChannel,
        bot: discord.Client,
    ) -> None:
        self.player = player
        self.channel = channel
        self.bot = bot
        self.message: discord.Message | None = None
        self.view: MusicPanelView | None = None
        self._refresh_task: asyncio.Task | None = None
        self._refresh_lock = asyncio.Lock()

    # --- Cycle de vie -------------------------------------------------

    async def install(self) -> discord.Message:
        """Crée le message panneau dans le channel."""
        self.view = MusicPanelView(self.player, self)
        embed = embeds.panel(self.player)
        self.message = await self.channel.send(embed=embed, view=self.view)
        self.start_refresh()
        return self.message

    async def remove(self) -> None:
        """Stoppe le refresh et supprime le message."""
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
        """Réédite le message avec un embed à jour. Thread-safe."""
        if self.message is None:
            return
        async with self._refresh_lock:
            try:
                embed = embeds.panel(self.player)
                await self.message.edit(embed=embed, view=self.view)
            except discord.NotFound:
                # Message supprimé par un humain → on stoppe
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
        """Boucle de fond : refresh régulier pour faire avancer la barre de progression."""
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
