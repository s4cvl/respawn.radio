"""Cog principal : toutes les commandes musique."""

from __future__ import annotations

import asyncio
import logging
import urllib.parse

import aiohttp
import discord
from discord.ext import commands

from bot.core.player import PlayerRegistry
from bot.core.resolver import Resolver
from bot.core.spotify import SpotifyClient
from bot.utils import embeds
from bot.utils.helpers import truncate

log = logging.getLogger(__name__)


# Réactions pour la pagination de la queue
PAGE_PREV = "◀️"
PAGE_NEXT = "▶️"
# Réactions pour la sélection de !search
DIGIT_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
CANCEL_EMOJI = "❌"


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
        """Vérifie que l'utilisateur est dans un vocal et que le bot peut le rejoindre.

        Retourne True si OK, False sinon (le message d'erreur est déjà envoyé).
        """
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
                # Pycord : self_deaf ne passe pas dans connect(),
                # on le fait via change_voice_state après coup.
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
            await msg.delete()
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
    # Contrôles : pause/resume/stop/skip
    # ==================================================================

    @commands.command(name="pause")
    async def pause(self, ctx: commands.Context) -> None:
        player = self.players.get_existing(ctx.guild.id)
        if player and player.pause():
            await ctx.send(embed=embeds.success("⏸️ En pause."))
        else:
            await ctx.send(embed=embeds.error("Rien à mettre en pause."))

    @commands.command(name="resume")
    async def resume(self, ctx: commands.Context) -> None:
        player = self.players.get_existing(ctx.guild.id)
        if player and player.resume():
            await ctx.send(embed=embeds.success("▶️ Reprise."))
        else:
            await ctx.send(embed=embeds.error("Rien à reprendre."))

    @commands.command(name="stop")
    async def stop(self, ctx: commands.Context) -> None:
        player = self.players.get_existing(ctx.guild.id)
        if player is None:
            await ctx.send(embed=embeds.error("Rien en lecture."))
            return
        await player.stop()
        await ctx.send(embed=embeds.success("⏹️ Stop, queue vidée."))

    @commands.command(name="skip", aliases=["s", "next"])
    async def skip(self, ctx: commands.Context) -> None:
        player = self.players.get_existing(ctx.guild.id)
        if player and player.skip():
            await ctx.send(embed=embeds.success("⏭️ Skip."))
        else:
            await ctx.send(embed=embeds.error("Rien à skipper."))

    @commands.command(name="skipto")
    async def skipto(self, ctx: commands.Context, position: int) -> None:
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
        player = self.players.get_existing(ctx.guild.id)
        if player is None or not player.queue:
            await ctx.send(embed=embeds.info("Queue déjà vide."))
            return
        n = len(player.queue)
        player.clear()
        await ctx.send(embed=embeds.success(f"🧹 {n} piste(s) supprimée(s) de la queue."))

    @commands.command(name="shuffle")
    async def shuffle_cmd(self, ctx: commands.Context) -> None:
        player = self.players.get_existing(ctx.guild.id)
        if player is None or len(player.queue) < 2:
            await ctx.send(embed=embeds.error("Pas assez de pistes pour mélanger."))
            return
        player.shuffle()
        await ctx.send(embed=embeds.success("🔀 Queue mélangée."))

    @commands.command(name="move")
    async def move_cmd(self, ctx: commands.Context, src: int, dst: int) -> None:
        player = self.players.get_existing(ctx.guild.id)
        if player is None or not player.queue:
            await ctx.send(embed=embeds.error("Queue vide."))
            return
        if not player.move(src, dst):
            await ctx.send(embed=embeds.error("Positions invalides."))
            return
        await ctx.send(embed=embeds.success(f"↔️ Piste déplacée de #{src} → #{dst}."))

    # ==================================================================
    # Infos : nowplaying / lyrics
    # ==================================================================

    @commands.command(name="nowplaying", aliases=["np", "current"])
    async def nowplaying(self, ctx: commands.Context) -> None:
        player = self.players.get_existing(ctx.guild.id)
        if player is None or player.current is None:
            await ctx.send(embed=embeds.info("Rien en lecture."))
            return
        await ctx.send(embed=embeds.now_playing(player))

    @commands.command(name="lyrics")
    async def lyrics(self, ctx: commands.Context, *, query: str | None = None) -> None:
        """Affiche les paroles de la piste en cours (ou d'une recherche).

        Source : lyrics.ovh (gratuit, sans clé). Format attendu : "Artiste - Titre".
        """
        artist: str | None = None
        title: str | None = None

        if query:
            if " - " in query:
                artist, title = (s.strip() for s in query.split(" - ", 1))
            else:
                artist, title = ("", query.strip())
        else:
            player = self.players.get_existing(ctx.guild.id)
            if player is None or player.current is None:
                await ctx.send(
                    embed=embeds.error(
                        "Rien en lecture. Essaie `!lyrics Artiste - Titre`."
                    )
                )
                return
            current = player.current
            # Format probable : "Artiste - Titre" (cas Spotify) ou juste un titre YouTube
            if " - " in current.title:
                artist, title = (s.strip() for s in current.title.split(" - ", 1))
            else:
                artist = current.uploader or ""
                title = current.title

        if not title:
            await ctx.send(embed=embeds.error("Titre introuvable."))
            return

        async with ctx.typing():
            try:
                lyrics_text = await _fetch_lyrics(artist or "", title)
            except Exception as exc:
                await ctx.send(embed=embeds.error(f"API lyrics indisponible : {exc}"))
                return

        if not lyrics_text:
            await ctx.send(
                embed=embeds.error(
                    f"Paroles introuvables pour **{artist or '?'} - {title}**."
                )
            )
            return

        # Discord limite la description d'un embed à 4096 chars
        chunks = _split_lyrics(lyrics_text, 1800)
        header = f"🎤 **{artist or '?'} — {title}**"
        await ctx.send(content=header, embed=embeds.info(chunks[0]))
        for extra in chunks[1:]:
            await ctx.send(embed=embeds.info(extra))

    # ==================================================================
    # Modes : loop / loopqueue / volume
    # ==================================================================

    @commands.command(name="loop")
    async def loop(self, ctx: commands.Context) -> None:
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

        # Quelqu'un a rejoint le vocal du bot → annule l'auto-leave
        if after.channel and after.channel == player.voice_client.channel:
            player.cancel_auto_leave()
            return

        # Quelqu'un a quitté → vérifie si le bot est seul
        if before.channel == player.voice_client.channel:
            if player._bot_alone():
                player.schedule_auto_leave_check()

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


# ----------------------------------------------------------------------
# Lyrics fetch (lyrics.ovh)
# ----------------------------------------------------------------------

LYRICS_OVH_URL = "https://api.lyrics.ovh/v1/{artist}/{title}"


async def _fetch_lyrics(artist: str, title: str) -> str | None:
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


def _split_lyrics(text: str, chunk_size: int) -> list[str]:
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


def setup(bot: commands.Bot) -> None:
    # Pycord : add_cog est synchrone (pas await)
    bot.add_cog(Music(bot))
