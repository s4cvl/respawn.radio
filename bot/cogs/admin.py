"""Cog admin — commandes utilitaires réservées au propriétaire du bot."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from bot.utils import embeds

log = logging.getLogger(__name__)


class Admin(commands.Cog):
    """Commandes admin : ping, reload, sync, shutdown."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="ping")
    async def ping(self, ctx: commands.Context) -> None:
        """Mesure la latence WebSocket Discord."""
        latency_ms = round(self.bot.latency * 1000)
        await ctx.send(embed=embeds.info(f"🏓 Pong ! `{latency_ms} ms`"))

    @commands.command(name="reload")
    @commands.is_owner()
    async def reload(self, ctx: commands.Context, extension: str) -> None:
        """Recharge un cog. Ex : `!reload bot.cogs.music`."""
        try:
            # Pycord : reload_extension est synchrone
            self.bot.reload_extension(extension)
        except Exception as exc:
            await ctx.send(embed=embeds.error(f"Reload KO : `{exc}`"))
            return
        await ctx.send(embed=embeds.success(f"Cog rechargé : `{extension}`"))

    @commands.command(name="shutdown")
    @commands.is_owner()
    async def shutdown(self, ctx: commands.Context) -> None:
        """Arrête proprement le bot (owner only)."""
        await ctx.send(embed=embeds.info("👋 Extinction…"))
        await self.bot.close()


def setup(bot: commands.Bot) -> None:
    """Entrypoint du cog (Pycord : add_cog est synchrone)."""
    bot.add_cog(Admin(bot))
