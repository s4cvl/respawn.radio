"""Entrypoint Respawn Radio — bootstrap du bot Discord."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import discord
from discord.ext import commands
from dotenv import load_dotenv

from bot.utils.help_command import RespawnHelp

# --- Configuration ---

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

INITIAL_COGS = (
    "bot.cogs.music",
    "bot.cogs.admin",
)


# --- Logging ---

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger("discord").setLevel(logging.WARNING)
log = logging.getLogger("respawn-radio")


# --- Bot ---


def build_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True  # PRIVILEGED — à activer dans le Dev Portal
    intents.voice_states = True
    intents.guilds = True
    # Pas besoin de `members` (privileged). Les membres du salon vocal du bot
    # sont remplis via les events voice_state_update.

    bot = commands.Bot(
        command_prefix=COMMAND_PREFIX,
        intents=intents,
        help_command=RespawnHelp(),
        description="Respawn Radio — music bot maison.",
    )

    @bot.event
    async def on_ready() -> None:
        log.info(
            "Connecté en tant que %s (id=%s)",
            bot.user,
            bot.user.id if bot.user else "?",
        )
        log.info("Préfixe : %s • Guilds : %d", COMMAND_PREFIX, len(bot.guilds))
        try:
            await bot.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.listening,
                    name=f"{COMMAND_PREFIX}play",
                )
            )
        except Exception as exc:
            log.warning("Échec set_presence : %s", exc)

    @bot.event
    async def on_command_error(
        ctx: commands.Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.NotOwner):
            await ctx.send("⛔ Réservé au propriétaire du bot.")
            return
        log.error("Erreur non gérée : %s", error, exc_info=error)

    return bot


# --- Run ---


async def _main() -> None:
    if not DISCORD_TOKEN:
        log.error("DISCORD_TOKEN manquant — remplis .env (cf. .env.example).")
        sys.exit(1)

    bot = build_bot()

    # Pycord : load_extension est synchrone (pas await)
    for cog in INITIAL_COGS:
        try:
            bot.load_extension(cog)
            log.info("Cog chargé : %s", cog)
        except Exception as exc:
            log.exception("Échec chargement cog %s : %s", cog, exc)
            sys.exit(1)

    try:
        await bot.start(DISCORD_TOKEN)
    except KeyboardInterrupt:
        log.info("Interruption clavier — arrêt.")
    finally:
        if not bot.is_closed():
            await bot.close()


def main() -> None:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
