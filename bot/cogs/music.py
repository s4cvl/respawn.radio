"""Cog principal : toutes les commandes musique."""

from __future__ import annotations

import asyncio
import logging
import time as _time

import discord
from discord.ext import commands

from bot.core.panel import MusicPanel
from bot.core.player import PlayerRegistry
from bot.core.resolver import Resolver
from bot.core.spotify import SpotifyClient
from bot.utils import embeds
from bot.utils.helpers import truncate

log = logging.getLogger(__name__)


# Réactions pagination de la queue
PAGE_PREV = "◀️"
PAGE_NEXT = "▶️"
# Réactions sélection !search
DIGIT_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
CANCEL_EMOJI = "❌"

# Délai avant auto-delete d'un message dans le panel channel (secondes)
PANEL_CHANNEL_CLEAN_DELAY = 5.0


class Music(commands.Cog):
    """Toutes les commandes musique du bot."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.spotify = SpotifyClient()
        self.resolver = Resolver(self.spotify)
        self.players = PlayerRegistry(bot, self.resolver)

    # ==================================================================
    # Helpers internes
    # ==================================================================

    async def _ensure_voice(self, ctx: commands.Context) -> bool:
        if ctx.author.voice is None or ctx.author.voice.channel is None:
            await ctx.send(embed=embeds.error("Tu dois être dans un salon vocal."))
            return False

        target = ctx.author.voice.channel
        player = self.players.get(ctx.guild)
        player.text_channel = ctx.channel  # type: ignore[assignment]

        vc = ctx.guild.voice_client
        if vc and vc.channel and vc.channel.id != target.id:
            await ctx.send(
                embed=embeds.error(
                    f"Je suis déjà dans **{vc.channel.name}**. Utilise `!leave` d'abord."
                )
            )
            return False

        if vc is None or not vc.is_connected():
            try:
                vc = await target.connect()
                await ctx.guild.change_voice_state(channel=target, self_deaf=True)
            except Exception as exc:
                log.exception("Connexion vocal KO sur %s", target)
                await ctx.send(embed=embeds.error(f"Connexion au vocal impossible : {exc}"))
                return False

        player.voice_client = vc
        player.start()
        return True

    # ==================================================================
    # !join / !leave
    # ==================================================================

    @commands.command(name="join", aliases=["connect"])
    async def join(self, ctx: commands.Context) -> None:
        """Rejoint le salon vocal de l'utilisateur."""
        if await self._ensure_voice(ctx):
            await ctx.send(embed=embeds.success("Connecté au vocal."))

    @commands.command(name="leave", aliases=["disconnect", "dc"])
    async def leave(self, ctx: commands.Context) -> None:
        """Quitte le vocal et vide la queue."""
        player = self.players.get_existing(ctx.guild.id)
        if player is None or player.voice_client is None:
            await ctx.send(embed=embeds.error("Je ne suis pas connecté."))
            return
        await player.disconnect()
        await ctx.send(embed=embeds.success("À plus."))

    # ==================================================================
    # !play / !search
    # ==================================================================

    @commands.command(name="play", aliases=["p"])
    async def play(self, ctx: commands.Context, *, query: str) -> None:
        """Joue ou ajoute à la queue : URL YouTube/Spotify ou texte libre."""
        if not await self._ensure_voice(ctx):
            return

        player = self.players.get(ctx.guild)
        async with ctx.typing():
            try:
                batch = await self.resolver.resolve(query, requester=ctx.author.display_name)
            except RuntimeError as exc:
                await ctx.send(embed=embeds.error(str(exc)))
                return
            except Exception as exc:
                log.exception("resolve KO")
                await ctx.send(embed=embeds.error(f"Résolution impossible : {exc}"))
                return

        if not batch.tracks:
            await ctx.send(embed=embeds.error("Aucun résultat trouvé."))
            return

        if batch.is_playlist:
            player.enqueue_many(batch.tracks)
            await ctx.send(embed=embeds.playlist_added(len(batch.tracks), batch.source_label))
        else:
            track = batch.tracks[0]
            position = player.enqueue(track)
            await ctx.send(embed=embeds.track_added(track, position))

        player.notify_play()

    @commands.command(name="search")
    async def search(self, ctx: commands.Context, *, query: str) -> None:
        """Affiche les 5 premiers résultats YouTube, choix via réaction."""
        if not await self._ensure_voice(ctx):
            return

        async with ctx.typing():
            try:
                results = await self.resolver.search_youtube(query, limit=5)
            except Exception as exc:
                await ctx.send(embed=embeds.error(f"Recherche KO : {exc}"))
                return

        if not results:
            await ctx.send(embed=embeds.error("Aucun résultat."))
            return

        msg = await ctx.send(embed=embeds.search_results(query, results))
        for emoji in DIGIT_EMOJIS[: len(results)]:
            await msg.add_reaction(emoji)
        await msg.add_reaction(CANCEL_EMOJI)

        def check(reaction: discord.Reaction, user: discord.User) -> bool:
            return (
                user.id == ctx.author.id
                and reaction.message.id == msg.id
                and str(reaction.emoji) in DIGIT_EMOJIS + [CANCEL_EMOJI]
            )

        try:
            reaction, _ = await self.bot.wait_for("reaction_add", timeout=30.0, check=check)
        except asyncio.TimeoutError:
            try:
                await msg.clear_reactions()
            except Exception:
                pass
            return

        if str(reaction.emoji) == CANCEL_EMOJI:
            try:
                await msg.delete()
            except Exception:
                pass
            return

        choice = DIGIT_EMOJIS.index(str(reaction.emoji))
        chosen = results[choice]
        url = chosen.get("url") or chosen.get("webpage_url")
        if url and not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={url}"

        try:
            batch = await self.resolver.resolve(url, requester=ctx.author.display_name)
        except Exception as exc:
            await ctx.send(embed=embeds.error(f"Impossible d'ajouter : {exc}"))
            return

        if not batch.tracks:
            await ctx.send(embed=embeds.error("Impossible de récupérer la piste."))
            return

        player = self.players.get(ctx.guild)
        track = batch.tracks[0]
        position = player.enqueue(track)
        player.notify_play()
        await ctx.send(embed=embeds.track_added(track, position))

    # ==================================================================
    # Contrôles : pause/resume/stop/skip/skipto
    # ==================================================================

    @commands.command(name="pause")
    async def pause(self, ctx: commands.Context) -> None:
        """Met la lecture en pause."""
        player = self.players.get_existing(ctx.guild.id)
        if player and player.pause():
            await ctx.send(embed=embeds.success("⏸️ En pause."))
        else:
            await ctx.send(embed=embeds.error("Rien à mettre en pause."))

    @commands.command(name="resume")
    async def resume(self, ctx: commands.Context) -> None:
        """Reprend la lecture après une pause."""
        player = self.players.get_existing(ctx.guild.id)
        if player and player.resume():
            await ctx.send(embed=embeds.success("▶️ Reprise."))
        else:
            await ctx.send(embed=embeds.error("Rien à reprendre."))

    @commands.command(name="stop")
    async def stop(self, ctx: commands.Context) -> None:
        """Arrête la lecture et vide la queue."""
        player = self.players.get_existing(ctx.guild.id)
        if player is None:
            await ctx.send(embed=embeds.error("Rien en lecture."))
            return
        await player.stop()
        await ctx.send(embed=embeds.success("⏹️ Stop, queue vidée."))

    @commands.command(name="skip", aliases=["s", "next"])
    async def skip(self, ctx: commands.Context) -> None:
        """Passe à la piste suivante."""
        player = self.players.get_existing(ctx.guild.id)
        if not player or not player.skip():
            await ctx.send(embed=embeds.error("Rien à skipper."))
            return
        skipped = player.current
        await self._wait_for_track_change(player, previous=skipped, timeout=6.0)
        await ctx.send(embed=embeds.skip_summary(player, skipped=skipped))

    async def _wait_for_track_change(
        self,
        player,
        *,
        previous,
        timeout: float = 5.0,
    ) -> None:
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            await asyncio.sleep(0.15)
            current = player.current
            if current is not None and current is not previous:
                return
            if (
                current is None
                and not player.queue
                and not player.loop
                and not player.loop_queue
            ):
                return

    @commands.command(name="skipto")
    async def skipto(self, ctx: commands.Context, position: int) -> None:
        """Saute directement à la piste #N de la queue. Ex : `!skipto 5`."""
        player = self.players.get_existing(ctx.guild.id)
        if player is None:
            await ctx.send(embed=embeds.error("Rien en lecture."))
            return
        if not player.skip_to(position):
            await ctx.send(embed=embeds.error(f"Position invalide (1-{len(player.queue)})."))
            return
        await ctx.send(embed=embeds.success(f"⏭️ Saut à la piste #{position}."))

    # ==================================================================
    # Queue : queue / remove / clear / shuffle / move
    # ==================================================================

    @commands.command(name="queue", aliases=["q"])
    async def queue_cmd(self, ctx: commands.Context, page: int = 1) -> None:
        """Affiche la queue (10 pistes par page). Navigation via ◀️ ▶️."""
        player = self.players.get_existing(ctx.guild.id)
        if player is None:
            await ctx.send(embed=embeds.info("Queue vide."))
            return

        per_page = 10
        total_pages = max(1, (len(player.queue) + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))

        msg = await ctx.send(embed=embeds.queue(player, page=page, per_page=per_page))

        if total_pages <= 1:
            return

        await msg.add_reaction(PAGE_PREV)
        await msg.add_reaction(PAGE_NEXT)

        def check(reaction: discord.Reaction, user: discord.User) -> bool:
            return (
                user.id == ctx.author.id
                and reaction.message.id == msg.id
                and str(reaction.emoji) in (PAGE_PREV, PAGE_NEXT)
            )

        current_page = page
        while True:
            try:
                reaction, user = await self.bot.wait_for(
                    "reaction_add", timeout=60.0, check=check
                )
            except asyncio.TimeoutError:
                try:
                    await msg.clear_reactions()
                except Exception:
                    pass
                return

            if str(reaction.emoji) == PAGE_NEXT and current_page < total_pages:
                current_page += 1
            elif str(reaction.emoji) == PAGE_PREV and current_page > 1:
                current_page -= 1

            try:
                await msg.edit(embed=embeds.queue(player, page=current_page, per_page=per_page))
                await msg.remove_reaction(reaction.emoji, user)
            except Exception:
                pass

    @commands.command(name="remove", aliases=["rm"])
    async def remove(self, ctx: commands.Context, position: int) -> None:
        """Retire la piste #N de la queue. Ex : `!remove 3`."""
        player = self.players.get_existing(ctx.guild.id)
        if player is None:
            await ctx.send(embed=embeds.error("Queue vide."))
            return
        track = player.remove_at(position)
        if track is None:
            await ctx.send(embed=embeds.error(f"Position invalide (1-{len(player.queue)})."))
            return
        await ctx.send(embed=embeds.success(f"🗑️ Retiré : **{truncate(track.title, 80)}**"))

    @commands.command(name="clear")
    async def clear_cmd(self, ctx: commands.Context) -> None:
        """Vide la queue sans arrêter la piste en cours."""
        player = self.players.get_existing(ctx.guild.id)
        if player is None or not player.queue:
            await ctx.send(embed=embeds.info("Queue déjà vide."))
            return
        n = len(player.queue)
        player.clear()
        await ctx.send(embed=embeds.success(f"🧹 {n} piste(s) supprimée(s) de la queue."))

    @commands.command(name="shuffle")
    async def shuffle_cmd(self, ctx: commands.Context) -> None:
        """Mélange aléatoirement l'ordre de la queue."""
        player = self.players.get_existing(ctx.guild.id)
        if player is None or len(player.queue) < 2:
            await ctx.send(embed=embeds.error("Pas assez de pistes pour mélanger."))
            return
        player.shuffle()
        await ctx.send(embed=embeds.success("🔀 Queue mélangée."))

    @commands.command(name="move")
    async def move_cmd(self, ctx: commands.Context, src: int, dst: int) -> None:
        """Déplace la piste #src vers la position #dst. Ex : `!move 5 1`."""
        player = self.players.get_existing(ctx.guild.id)
        if player is None or not player.queue:
            await ctx.send(embed=embeds.error("Queue vide."))
            return
        if not player.move(src, dst):
            await ctx.send(embed=embeds.error("Positions invalides."))
            return
        await ctx.send(embed=embeds.success(f"↔️ Piste déplacée de #{src} → #{dst}."))

    # ==================================================================
    # Infos : nowplaying
    # (lyrics est devenu un bouton ephemeral du panneau)
    # ==================================================================

    @commands.command(name="nowplaying", aliases=["np", "current"])
    async def nowplaying(self, ctx: commands.Context) -> None:
        """Affiche la piste en cours avec barre de progression."""
        player = self.players.get_existing(ctx.guild.id)
        if player is None or player.current is None:
            await ctx.send(embed=embeds.info("Rien en lecture."))
            return
        await ctx.send(embed=embeds.now_playing(player))

    # ==================================================================
    # Modes : loop / loopqueue / volume
    # ==================================================================

    @commands.command(name="loop")
    async def loop(self, ctx: commands.Context) -> None:
        """Active/désactive la boucle sur la piste en cours 🔂."""
        player = self.players.get_existing(ctx.guild.id)
        if player is None:
            await ctx.send(embed=embeds.error("Rien en lecture."))
            return
        player.loop = not player.loop
        if player.loop:
            player.loop_queue = False
        state = "activée 🔂" if player.loop else "désactivée"
        await ctx.send(embed=embeds.success(f"Boucle piste {state}."))

    @commands.command(name="loopqueue", aliases=["loopq"])
    async def loop_queue(self, ctx: commands.Context) -> None:
        """Active/désactive la boucle sur toute la queue 🔁."""
        player = self.players.get_existing(ctx.guild.id)
        if player is None:
            await ctx.send(embed=embeds.error("Rien en lecture."))
            return
        player.loop_queue = not player.loop_queue
        if player.loop_queue:
            player.loop = False
        state = "activée 🔁" if player.loop_queue else "désactivée"
        await ctx.send(embed=embeds.success(f"Boucle queue {state}."))

    @commands.command(name="volume", aliases=["vol"])
    async def volume(self, ctx: commands.Context, value: int) -> None:
        """Règle le volume du bot de 0 à 100 (défaut : 50)."""
        player = self.players.get_existing(ctx.guild.id)
        if player is None:
            await ctx.send(embed=embeds.error("Pas connecté."))
            return
        if not 0 <= value <= 100:
            await ctx.send(embed=embeds.error("Volume entre 0 et 100."))
            return
        player.set_volume(value)
        await ctx.send(embed=embeds.success(f"🔊 Volume réglé à **{value}%**."))

    # ==================================================================
    # Panneau persistant (!setup / !unsetup)
    # ==================================================================

    @commands.command(name="setup")
    async def setup_panel(self, ctx: commands.Context) -> None:
        """Installe le panneau de contrôle dans ce channel (mode music-only)."""
        player = self.players.get(ctx.guild)
        if player.panel is not None:
            await player.panel.remove()
            player.panel = None

        panel = MusicPanel(player, ctx.channel, self.bot, self)
        try:
            await panel.install()
        except discord.Forbidden:
            await ctx.send(
                embed=embeds.error(
                    "Permissions insuffisantes (besoin de Send Messages + Embed Links + "
                    "Manage Messages pour le mode music-only)."
                )
            )
            return
        except Exception as exc:
            log.exception("setup panel KO")
            await ctx.send(embed=embeds.error(f"Installation panneau KO : {exc}"))
            return
        player.panel = panel
        player.panel_channel_id = ctx.channel.id
        try:
            await ctx.message.add_reaction("🎛️")
        except Exception:
            pass

    @commands.command(name="unsetup")
    async def unsetup_panel(self, ctx: commands.Context) -> None:
        """Retire le panneau de contrôle persistant."""
        player = self.players.get_existing(ctx.guild.id)
        if player is None or player.panel is None:
            await ctx.send(embed=embeds.error("Pas de panneau actif."))
            return
        await player.panel.remove()
        player.panel = None
        player.panel_channel_id = None
        await ctx.send(embed=embeds.success("Panneau retiré."))

    # ==================================================================
    # Listeners
    # ==================================================================

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Auto-leave si le bot reste seul 5 minutes."""
        if member.bot:
            return
        guild = member.guild
        player = self.players.get_existing(guild.id)
        if player is None or player.voice_client is None:
            return

        if after.channel and after.channel == player.voice_client.channel:
            player.cancel_auto_leave()
            return

        if before.channel == player.voice_client.channel:
            if player._bot_alone():
                player.schedule_auto_leave_check()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Mode music-only : auto-delete tous les messages du panel channel
        (sauf le panneau lui-même). Délai 5s pour laisser lire."""
        if message.guild is None or message.channel is None:
            return
        player = self.players.get_existing(message.guild.id)
        if player is None or player.panel_channel_id is None:
            return
        if message.channel.id != player.panel_channel_id:
            return
        # Ne touche pas le panneau lui-même
        panel_msg = player.panel.message if player.panel else None
        if panel_msg is not None and message.id == panel_msg.id:
            return
        try:
            await asyncio.sleep(PANEL_CHANNEL_CLEAN_DELAY)
            await message.delete()
        except (discord.NotFound, discord.Forbidden):
            pass
        except Exception as exc:
            log.warning("auto-delete panel channel msg KO : %s", exc)

    # ==================================================================
    # Error handler global du cog
    # ==================================================================

    async def cog_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(
                embed=embeds.error(
                    f"Argument manquant : `{error.param.name}`. Tape `!help {ctx.command}`."
                )
            )
            return
        if isinstance(error, commands.BadArgument):
            await ctx.send(embed=embeds.error(f"Argument invalide : {error}"))
            return
        if isinstance(error, commands.CommandInvokeError):
            log.exception("Erreur dans commande %s", ctx.command, exc_info=error.original)
            await ctx.send(embed=embeds.error(f"Erreur interne : {error.original}"))
            return
        log.error("Erreur non gérée dans %s : %s", ctx.command, error)
        await ctx.send(embed=embeds.error(f"Erreur : {error}"))


def setup(bot: commands.Bot) -> None:
    """Entrypoint du cog (Pycord : add_cog est synchrone)."""
    bot.add_cog(Music(bot))
