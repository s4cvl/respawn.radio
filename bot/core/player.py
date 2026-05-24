"""GuildPlayer — état d'une session de lecture par guild Discord."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from .resolver import FFMPEG_BEFORE_OPTS, FFMPEG_OPTS, Resolver

if TYPE_CHECKING:
    from .panel import MusicPanel

log = logging.getLogger(__name__)

# Délai d'auto-leave si le bot est seul dans le vocal (secondes)
AUTO_LEAVE_DELAY = 5 * 60


@dataclass(slots=True)
class Track:
    """Une piste dans la queue.

    `lazy=True` signifie que `stream_url` n'est pas encore résolu (cas Spotify ou
    extraction flat de playlist YouTube). Le resolver remplira juste avant lecture.
    """

    title: str
    url: str
    stream_url: str | None = None
    duration: float | None = None
    uploader: str | None = None
    thumbnail: str | None = None
    requester: str | None = None
    search_query: str | None = None  # utilisé si lazy=True
    lazy: bool = False


class GuildPlayer:
    """Player musique pour une guild Discord. Une instance par serveur."""

    def __init__(
        self,
        guild: discord.Guild,
        resolver: Resolver,
        bot: discord.Client,
    ) -> None:
        self.guild = guild
        self.bot = bot
        self.resolver = resolver

        self.queue: list[Track] = []
        self.current: Track | None = None
        self.loop: bool = False
        self.loop_queue: bool = False
        self.volume: float = 0.5  # 0.0 → 1.0

        self.voice_client: discord.VoiceClient | None = None
        self.text_channel: discord.TextChannel | None = None  # pour annonces auto
        self.panel: "MusicPanel | None" = None  # panneau persistant optionnel

        self._play_next_event = asyncio.Event()
        self._player_task: asyncio.Task | None = None
        self._auto_leave_task: asyncio.Task | None = None
        self._started_at: float | None = None
        self._paused_at: float | None = None
        self._paused_total: float = 0.0

    # ------------------------------------------------------------------
    # Cycle de vie
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Démarre la boucle de lecture si pas déjà active."""
        if self._player_task is None or self._player_task.done():
            self._player_task = self.bot.loop.create_task(self._player_loop())

    async def stop(self) -> None:
        """Arrête la lecture et vide la queue (le bot reste connecté)."""
        self.queue.clear()
        self.loop = False
        self.loop_queue = False
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()
        self.current = None
        self._started_at = None

    async def disconnect(self) -> None:
        """Quitte le salon vocal et nettoie l'état."""
        await self.stop()
        if self._player_task and not self._player_task.done():
            self._player_task.cancel()
        if self._auto_leave_task and not self._auto_leave_task.done():
            self._auto_leave_task.cancel()
        if self.voice_client:
            try:
                await self.voice_client.disconnect(force=True)
            except Exception as exc:
                log.warning("Erreur disconnect : %s", exc)
        self.voice_client = None

    # ------------------------------------------------------------------
    # Queue
    # ------------------------------------------------------------------

    def enqueue(self, track: Track) -> int:
        """Ajoute une piste, retourne sa position 1-indexed dans la queue."""
        self.queue.append(track)
        return len(self.queue)

    def enqueue_many(self, tracks: list[Track]) -> None:
        self.queue.extend(tracks)

    def remove_at(self, position: int) -> Track | None:
        """`position` 1-indexed."""
        idx = position - 1
        if 0 <= idx < len(self.queue):
            return self.queue.pop(idx)
        return None

    def move(self, src: int, dst: int) -> bool:
        """Déplace une piste de la position src vers dst (1-indexed)."""
        if not (1 <= src <= len(self.queue)) or not (1 <= dst <= len(self.queue)):
            return False
        track = self.queue.pop(src - 1)
        self.queue.insert(dst - 1, track)
        return True

    def shuffle(self) -> None:
        import random

        random.shuffle(self.queue)

    def clear(self) -> None:
        self.queue.clear()

    # ------------------------------------------------------------------
    # Contrôles
    # ------------------------------------------------------------------

    def pause(self) -> bool:
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.pause()
            self._paused_at = time.monotonic()
            return True
        return False

    def resume(self) -> bool:
        if self.voice_client and self.voice_client.is_paused():
            self.voice_client.resume()
            if self._paused_at:
                self._paused_total += time.monotonic() - self._paused_at
                self._paused_at = None
            return True
        return False

    def skip(self) -> bool:
        if self.voice_client and (
            self.voice_client.is_playing() or self.voice_client.is_paused()
        ):
            self.voice_client.stop()
            return True
        return False

    def skip_to(self, position: int) -> bool:
        """Saute directement à la position N dans la queue (drop ce qui précède)."""
        if not (1 <= position <= len(self.queue)):
            return False
        del self.queue[: position - 1]
        return self.skip()

    def set_volume(self, value: int) -> None:
        """`value` entre 0 et 100."""
        self.volume = max(0, min(100, value)) / 100.0
        if self.voice_client and self.voice_client.source:
            src = self.voice_client.source
            if isinstance(src, discord.PCMVolumeTransformer):
                src.volume = self.volume

    def elapsed_seconds(self) -> float:
        if self._started_at is None:
            return 0.0
        now = time.monotonic()
        if self._paused_at:
            return self._paused_at - self._started_at - self._paused_total
        return now - self._started_at - self._paused_total

    # ------------------------------------------------------------------
    # Boucle de lecture
    # ------------------------------------------------------------------

    async def _player_loop(self) -> None:
        """Tâche async qui consomme la queue piste par piste."""
        try:
            while True:
                self._play_next_event.clear()

                next_track = self._choose_next()
                if next_track is None:
                    await self._play_next_event.wait()
                    continue

                self.current = next_track

                if next_track.lazy or not next_track.stream_url:
                    ok = await self.resolver.resolve_stream(next_track)
                    if not ok:
                        await self._announce_skip(next_track, reason="track indisponible")
                        self.current = None
                        continue

                if not self.voice_client or not self.voice_client.is_connected():
                    log.warning("voice_client perdu en cours de lecture — arrêt boucle.")
                    return

                try:
                    source = discord.FFmpegPCMAudio(
                        next_track.stream_url,
                        before_options=FFMPEG_BEFORE_OPTS,
                        options=FFMPEG_OPTS,
                    )
                    source = discord.PCMVolumeTransformer(source, volume=self.volume)
                except Exception as exc:
                    log.exception("Création source FFmpeg KO : %s", exc)
                    await self._announce_skip(next_track, reason="erreur audio")
                    self.current = None
                    continue

                self._started_at = time.monotonic()
                self._paused_at = None
                self._paused_total = 0.0

                self.voice_client.play(
                    source,
                    after=lambda err: self.bot.loop.call_soon_threadsafe(
                        self._on_track_finished, err
                    ),
                )

                # Notifie le panneau (nouvelle piste)
                await self._notify_panel()

                await self._play_next_event.wait()
                self.current = None
                self._started_at = None
                # Notifie le panneau (piste terminée)
                await self._notify_panel()
        except asyncio.CancelledError:
            log.debug("Player loop cancelled for guild %s", self.guild.id)
            raise
        except Exception as exc:
            log.exception("Erreur fatale dans player_loop : %s", exc)

    def _choose_next(self) -> Track | None:
        """Sélectionne la prochaine piste selon les modes loop / loop_queue."""
        if self.loop and self.current is not None:
            return self.current
        if not self.queue:
            return None
        track = self.queue.pop(0)
        if self.loop_queue and self.current is not None:
            self.queue.append(self.current)
        return track

    def _on_track_finished(self, error: Exception | None) -> None:
        if error:
            log.warning("Erreur fin de piste : %s", error)
        self._play_next_event.set()

    def notify_play(self) -> None:
        """À appeler après un enqueue pour réveiller la boucle si elle dort."""
        self._play_next_event.set()

    async def _notify_panel(self) -> None:
        """Demande un refresh immédiat du panneau (no-op si pas de panneau)."""
        if self.panel is None:
            return
        try:
            await self.panel.refresh()
        except Exception as exc:
            log.warning("notify_panel KO : %s", exc)

    # ------------------------------------------------------------------
    # Auto-leave
    # ------------------------------------------------------------------

    def schedule_auto_leave_check(self) -> None:
        """Lance un check d'auto-leave (à appeler après un voice_state_update)."""
        if self._auto_leave_task and not self._auto_leave_task.done():
            return
        self._auto_leave_task = self.bot.loop.create_task(self._auto_leave_loop())

    async def _auto_leave_loop(self) -> None:
        try:
            await asyncio.sleep(AUTO_LEAVE_DELAY)
            if self._bot_alone():
                log.info("Auto-leave : seul depuis %ds, déconnexion.", AUTO_LEAVE_DELAY)
                if self.text_channel:
                    try:
                        await self.text_channel.send(
                            "🚪 Personne dans le vocal depuis 5 min, je me déconnecte."
                        )
                    except Exception:
                        pass
                await self.disconnect()
        except asyncio.CancelledError:
            pass

    def cancel_auto_leave(self) -> None:
        if self._auto_leave_task and not self._auto_leave_task.done():
            self._auto_leave_task.cancel()

    def _bot_alone(self) -> bool:
        if not self.voice_client or not self.voice_client.channel:
            return False
        members = [m for m in self.voice_client.channel.members if not m.bot]
        return len(members) == 0

    # ------------------------------------------------------------------
    # Annonces
    # ------------------------------------------------------------------

    async def _announce_skip(self, track: Track, *, reason: str) -> None:
        if not self.text_channel:
            return
        try:
            await self.text_channel.send(
                f"⚠️ Skip de **{track.title}** ({reason})."
            )
        except Exception:
            pass


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------


class PlayerRegistry:
    """Stocke un GuildPlayer par guild."""

    def __init__(self, bot: discord.Client, resolver: Resolver) -> None:
        self.bot = bot
        self.resolver = resolver
        self._players: dict[int, GuildPlayer] = {}

    def get(self, guild: discord.Guild) -> GuildPlayer:
        player = self._players.get(guild.id)
        if player is None:
            player = GuildPlayer(guild, self.resolver, self.bot)
            self._players[guild.id] = player
        return player

    def get_existing(self, guild_id: int) -> GuildPlayer | None:
        return self._players.get(guild_id)

    async def shutdown_all(self) -> None:
        for player in list(self._players.values()):
            await player.disconnect()
        self._players.clear()
