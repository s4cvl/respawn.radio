"""Heartbeat + liveness watchdog pour healthcheck Docker.

Deux tâches async indépendantes lancées depuis main.py :

* `heartbeat_loop` : touche `HEARTBEAT_PATH` toutes les `HEARTBEAT_INTERVAL`
  secondes tant que le bot est ready. Le HEALTHCHECK Docker check le mtime
  de ce fichier — si plus rien ne le touche, le container passe `unhealthy`.

* `liveness_loop` : surveille `bot.is_ready()` ; si le bot reste non-ready
  pendant `MAX_UNREADY` secondes, force `os._exit(1)` pour que Docker
  redémarre proprement le container (avec `restart: unless-stopped`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

import discord

log = logging.getLogger(__name__)

# Chemin du fichier heartbeat — synchronisé avec le HEALTHCHECK du Dockerfile.
HEARTBEAT_PATH = Path(os.getenv("HEARTBEAT_PATH", "/tmp/respawn_radio_alive"))
HEARTBEAT_INTERVAL = 30.0  # secondes

# Si le bot reste non-ready aussi longtemps, on quitte pour laisser Docker
# restart proprement. 180s = 3min, suffit pour absorber des reconnects WS.
MAX_UNREADY = 180.0
LIVENESS_CHECK_INTERVAL = 30.0


async def heartbeat_loop(bot: discord.Client) -> None:
    """Touche le fichier heartbeat tant que le bot est ready."""
    log.info("Heartbeat actif → %s (interval %.0fs)", HEARTBEAT_PATH, HEARTBEAT_INTERVAL)
    try:
        while True:
            try:
                if bot.is_ready() and not bot.is_closed():
                    HEARTBEAT_PATH.touch()
            except Exception as exc:
                log.warning("heartbeat touch KO : %s", exc)
            await asyncio.sleep(HEARTBEAT_INTERVAL)
    except asyncio.CancelledError:
        log.debug("heartbeat_loop cancelled")
        raise


async def liveness_loop(bot: discord.Client) -> None:
    """Force `os._exit(1)` si le bot reste non-ready trop longtemps.

    Pourquoi `os._exit` et pas `sys.exit` : on est dans une task async, et on
    veut court-circuiter tout cleanup pour que Docker restart sans délai.
    """
    last_ready_at: float | None = None
    log.info("Liveness watchdog actif (seuil %ds)", int(MAX_UNREADY))
    try:
        while True:
            await asyncio.sleep(LIVENESS_CHECK_INTERVAL)
            now = time.monotonic()
            if bot.is_ready() and not bot.is_closed():
                last_ready_at = now
                continue
            # Bot pas ready : on démarre/met à jour le compteur
            if last_ready_at is None:
                last_ready_at = now
                continue
            unready_for = now - last_ready_at
            if unready_for >= MAX_UNREADY:
                log.error(
                    "Bot non-ready depuis %.0fs (seuil %.0fs) — exit pour restart Docker.",
                    unready_for,
                    MAX_UNREADY,
                )
                # _exit court-circuite atexit et finalizers : on veut juste partir
                os._exit(1)
    except asyncio.CancelledError:
        log.debug("liveness_loop cancelled")
        raise
