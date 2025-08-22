import os
import logging
from typing import Literal, Optional

import discord
from discord import app_commands, Interaction
from discord.ext import commands

logger = logging.getLogger(__name__)


def owner_only_check():
    async def predicate(interaction: Interaction) -> bool:
        owner_env = os.getenv("BOT_OWNER_ID")
        try:
            owner_id = int(owner_env) if owner_env else None
        except Exception:
            owner_id = None
        try:
            return getattr(interaction.user, "id", None) == owner_id
        except Exception:
            return False

    return app_commands.check(predicate)


class CommandManager(commands.Cog):
    """Temporary owner-only management commands for command cleanup."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="manage_commands", description="Owner-only: clear/resync registered slash commands")
    @app_commands.describe(action="clear_guild | resync_guild | clear_global | resync_global")
    @owner_only_check()
    async def manage_commands(self, interaction: Interaction, action: Literal["clear_guild", "resync_guild", "clear_global", "resync_global"]):
        await interaction.response.defer(ephemeral=True)

        try:
            # Guild-scoped actions require being invoked inside a guild
            if action == "clear_guild":
                if not interaction.guild_id:
                    await interaction.followup.send("❌ This action must be run in a server (guild).", ephemeral=True)
                    return
                await self.bot.tree.clear_commands(guild=discord.Object(id=interaction.guild_id))
                await interaction.followup.send("✅ Cleared guild commands for this server.", ephemeral=True)
                return

            if action == "resync_guild":
                if not interaction.guild_id:
                    await interaction.followup.send("❌ This action must be run in a server (guild).", ephemeral=True)
                    return
                await self.bot.tree.sync(guild=discord.Object(id=interaction.guild_id))
                await interaction.followup.send("✅ Resynced guild commands to current code for this server.", ephemeral=True)
                return

            if action == "clear_global":
                # Dangerous: clears global commands
                await self.bot.tree.clear_commands(guild=None)
                await interaction.followup.send("✅ Cleared global commands (may take time to propagate).", ephemeral=True)
                return

            if action == "resync_global":
                await self.bot.tree.sync()
                await interaction.followup.send("✅ Resynced global commands to current code (may take a while).", ephemeral=True)
                return

            await interaction.followup.send("❌ Unknown action.", ephemeral=True)

        except Exception as e:
            logger.exception("Error managing commands")
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(CommandManager(bot))
