"""HelpCommand custom — embed sombre/orange, groupes logiques, alias visibles."""

from __future__ import annotations

import discord
from discord.ext import commands

from .embeds import BOT_NAME, COLOR_ACCENT, COLOR_ERROR


# Groupes affichés dans !help (ordre important pour la lecture)
# Format : { "Titre groupe": [list of command names] }
HELP_GROUPS: dict[str, list[str]] = {
    "▶️ Lecture": ["play", "search", "pause", "resume", "stop", "skip", "skipto"],
    "🗒️ Queue": ["queue", "remove", "clear", "shuffle", "move"],
    "🔁 Modes": ["loop", "loopqueue", "volume"],
    "🎙️ Vocal": ["join", "leave"],
    "📜 Infos": ["nowplaying"],
    "🎛️ Panneau": ["setup", "unsetup"],
    "⚙️ Admin": ["ping", "reload", "shutdown"],
}


class RespawnHelp(commands.HelpCommand):
    """!help — vue d'ensemble + !help <cmd> pour le détail."""

    def __init__(self) -> None:
        super().__init__(
            command_attrs={
                "help": "Affiche l'aide. Utilise `!help <commande>` pour le détail.",
                "aliases": ["h", "aide"],
            }
        )

    def _short_doc(self, command: commands.Command) -> str:
        if command.help:
            return command.help.strip().split("\n")[0]
        if command.brief:
            return command.brief
        return "—"

    # -- !help (sans args) -----------------------------------------------

    async def send_bot_help(self, mapping) -> None:
        ctx = self.context
        prefix = ctx.clean_prefix

        embed = discord.Embed(
            title=f"🎵 {BOT_NAME} — Aide",
            description=(
                f"Préfixe : `{prefix}`\n"
                f"Tape `{prefix}help <commande>` pour les détails d'une commande."
            ),
            color=COLOR_ACCENT,
        )

        for group_name, cmd_names in HELP_GROUPS.items():
            lines = []
            for name in cmd_names:
                cmd = ctx.bot.get_command(name)
                if cmd is None or cmd.hidden:
                    continue
                # Owner-only commands : on les cache aux non-owner
                if any(
                    getattr(c, "__qualname__", "").startswith("is_owner")
                    for c in cmd.checks
                ):
                    if not await ctx.bot.is_owner(ctx.author):
                        continue
                lines.append(f"`{prefix}{name}` — {self._short_doc(cmd)}")
            if lines:
                embed.add_field(name=group_name, value="\n".join(lines), inline=False)

        embed.set_footer(text=f"{BOT_NAME} • {sum(1 for _ in ctx.bot.walk_commands())} commandes")
        await self.get_destination().send(embed=embed)

    # -- !help <commande> ------------------------------------------------

    async def send_command_help(self, command: commands.Command) -> None:
        ctx = self.context
        prefix = ctx.clean_prefix

        embed = discord.Embed(
            title=f"`{prefix}{command.qualified_name}`",
            description=command.help or "*Pas de description.*",
            color=COLOR_ACCENT,
        )
        if command.aliases:
            embed.add_field(
                name="Alias",
                value=", ".join(f"`{prefix}{a}`" for a in command.aliases),
                inline=False,
            )
        signature = self.get_command_signature(command)
        embed.add_field(name="Syntaxe", value=f"`{signature}`", inline=False)
        embed.set_footer(text=BOT_NAME)
        await self.get_destination().send(embed=embed)

    # -- !help <cog> (pas utilisé chez nous mais on garde un fallback) ---

    async def send_cog_help(self, cog: commands.Cog) -> None:
        await self.send_bot_help(None)

    async def send_group_help(self, group: commands.Group) -> None:
        await self.send_command_help(group)

    # -- Erreurs ---------------------------------------------------------

    async def send_error_message(self, error: str) -> None:
        embed = discord.Embed(
            title="❌ Aide",
            description=error,
            color=COLOR_ERROR,
        )
        embed.set_footer(text=BOT_NAME)
        await self.get_destination().send(embed=embed)
