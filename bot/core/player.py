

"""GuildPlayer — état d'une session de lecture par guild Discord."""

from __future__ import annotations

import shlex
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

AUTO_LEAVE_DELAY = 5 * 60
WATCHDOG_INTERVAL = 5.0  # check toutes les 5s si voice_client tourne encore


def _before_opts(headers: dict | None) -> str:
    opts = FFMPEG_BEFORE_OPTS
    if headers:
        h = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        opts += f" -headers {shlex.quote(h)}"
    return opts


@dataclass(slots=True)
class Track:
    """Une piste dans la queue."""

    title: str
    url: str
    stream_url: str | None = None
    http_headers: dict | None = None
    duration: float | None = None
    uploader: str | None = None
    thumbnail: str | None = None
    requester: str | None = None
    search_query: str | None = None
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
        self.volume: float = 0.5

        self.voice_client: discord.VoiceClient | None = None
        self.text_channel: discord.TextChannel | None = None
        self.panel: "MusicPanel | None" = None
        self.panel_channel_id: int | None = None

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
        if self._player_task is None or self._player_task.done():
            self._player_task = self.bot.loop.create_task(self._player_loop())

    async def stop(self) -> None:
        self.queue.clear()
        self.loop = False
        self.loop_queue = False
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()
        self.current = None
        self._started_at = None

    async def disconnect(self) -> None:
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
        self.queue.append(track)
        return len(self.queue)

    def enqueue_many(self, tracks: list[Track]) -> None:
        self.queue.extend(tracks)

    def remove_at(self, position: int) -> Track | None:
        idx = position - 1
        if 0 <= idx < len(self.queue):
            return self.queue.pop(idx)
        return None

    def move(self, src: int, dst: int) -> bool:
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
            # En mode loop, le skip doit gagner contre la boucle
            self.loop = False
            self.voice_client.stop()
            return True
        return False

    def skip_to(self, position: int) -> bool:
        if not (1 <= position <= len(self.queue)):
            return False
        del self.queue[: position - 1]
        return self.skip()

    def set_volume(self, value: int) -> None:
        self.volume = max(0, min(100, value)) / 100.0
        if self.voice_client and self.voice_client.source:
            src = self.voice_client.source
            if isinstance(src, discord.PCMVolumeTransformer):
                src.volume = self.volume

    def elapsed_seconds(self) -> float:
        """Temps écoulé sur la piste courante, cappé par la durée si connue."""
        if self._started_at is None:
            return 0.0
        now = time.monotonic()
        if self._paused_at:
            elapsed = self._paused_at - self._started_at - self._paused_total
        else:
            elapsed = now - self._started_at - self._paused_total
        elapsed = max(0.0, elapsed)
        # Cap visuel : ne jamais afficher plus que la durée connue.
        if self.current is not None and self.current.duration:
            elapsed = min(elapsed, float(self.current.duration))
        return elapsed

    # ------------------------------------------------------------------
    # Boucle de lecture
    # ------------------------------------------------------------------

    async def _player_loop(self) -> None:
        """Tâche async qui consomme la queue piste par piste.

        IMPORTANT : `self.current` n'est pas reset à la fin d'une piste — c'est
        `_pick_next_track` qui décide de la garder (loop), la remettre en fin
        de queue (loop_queue) ou la jeter.
        """
        try:
            while True:
                self._play_next_event.clear()

                next_track = self._pick_next_track()
                if next_track is None:
                    # Rien à jouer — on attend qu'un enqueue + notify_play réveille
                    self.current = None
                    self._started_at = None
                    await self._notify_panel()
                    await self._play_next_event.wait()
                    continue

                self.current = next_track

                if next_track.lazy or not next_track.stream_url:
                    ok = await self.resolver.resolve_stream(next_track)
                    if not ok:
                        await self._announce_skip(next_track, reason="track indisponible")
                        # On laisse `current` set au track foiré, _pick_next_track
                        # passera au suivant (loop/loop_queue désactivés ou non).
                        # Pour éviter une boucle infinie avec loop=True sur une
                        # track foirée : force désactivation.
                        if self.loop:
                            self.loop = False
                        continue

                if not self.voice_client or not self.voice_client.is_connected():
                    log.warning("voice_client perdu en cours de lecture — arrêt boucle.")
                    return

                try:
                    source = discord.FFmpegPCMAudio(
                        next_track.stream_url,
                        before_options=_before_opts(next_track.http_headers),
                        options=FFMPEG_OPTS,
                    )
                    source = discord.PCMVolumeTransformer(source, volume=self.volume)
                except Exception as exc:
                    log.exception("Création source FFmpeg KO : %s", exc)
                    await self._announce_skip(next_track, reason="erreur audio")
                    if self.loop:
                        self.loop = False
                    continue

                self._started_at = time.monotonic()
                self._paused_at = None
                self._paused_total = 0.0

                try:
                    self.voice_client.play(
                        source,
                        after=lambda err: self.bot.loop.call_soon_threadsafe(
                            self._on_track_finished, err
                        ),
                    )
                except discord.ClientException as exc:
                    # Déjà en train de jouer ? Stop puis retry à la prochaine itération.
                    log.warning("voice_client.play() refusé : %s — on force stop.", exc)
                    try:
                        self.voice_client.stop()
                    except Exception:
                        pass
                    continue

                await self._notify_panel()

                # Attente fin de piste avec watchdog : si après 5s sans event
                # voice_client n'est plus actif, on déclenche l'event manuellement
                # (sécurité contre un after qui ne tire pas).
                await self._wait_track_end_with_watchdog()

                self._started_at = None
                await self._notify_panel()
        except asyncio.CancelledError:
            log.debug("Player loop cancelled for guild %s", self.guild.id)
            raise
        except Exception as exc:
            log.exception("Erreur fatale dans player_loop : %s", exc)

    async def _wait_track_end_with_watchdog(self) -> None:
        """Attend `_play_next_event` mais poll voice_client pour gérer un after KO."""
        while not self._play_next_event.is_set():
            try:
                await asyncio.wait_for(
                    self._play_next_event.wait(), timeout=WATCHDOG_INTERVAL
                )
                return
            except asyncio.TimeoutError:
                vc = self.voice_client
                if vc is None or not vc.is_connected():
                    log.warning("Watchdog : voice_client déconnecté, sortie forcée.")
                    return
                if not vc.is_playing() and not vc.is_paused():
                    log.warning(
                        "Watchdog : voice_client inactif sans after — fin forcée."
                    )
                    self._play_next_event.set()
                    return

    def _pick_next_track(self) -> Track | None:
        """Choisit la piste suivante en respectant loop / loop_queue.

        Appelé au début de chaque itération de _player_loop, donc :
        - `self.current` est la piste qui vient de finir (ou None au démarrage)
        - On décide : loop la même, remettre en queue, ou prendre la suivante
        """
        # Loop track : on rejoue la même
        if self.loop and self.current is not None:
            return self.current

        # Loop queue : la piste qui vient de finir part en fin de queue
        if self.loop_queue and self.current is not None:
            self.queue.append(self.current)

        if not self.queue:
            return None
        return self.queue.pop(0)

    def _on_track_finished(self, error: Exception | None) -> None:
        if error:
            log.warning("Erreur fin de piste : %s", error)
        self._play_next_event.set()

    def notify_play(self) -> None:
        """À appeler après un enqueue.

        Réveille la boucle UNIQUEMENT si elle attend (pas de piste en cours).
        Si une piste joue, la nouvelle entrée sera prise en fin de piste courante.
        """
        if self.current is None:
            self._play_next_event.set()

    async def _notify_panel(self) -> None:
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
