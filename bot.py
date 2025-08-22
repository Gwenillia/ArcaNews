import os
import asyncio
import logging
import time
from typing import Literal, List

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from miniflux import run_miniflux_loop

# ──────────────────────────────────────────────────────────────────────────────
# Env & Logging
# ──────────────────────────────────────────────────────────────────────────────
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
_dev_guild = os.getenv("DEV_GUILD_ID")
DEV_GUILD_ID = int(_dev_guild) if _dev_guild else None  # Optional dev server for instant sync
DEV_GUILD_OBJECT = discord.Object(id=DEV_GUILD_ID) if DEV_GUILD_ID else None

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# Owner config
_owner_env = os.getenv("BOT_OWNER_ID")
if _owner_env:
    try:
        OWNER_ID = int(_owner_env)
    except ValueError:
        logger.warning("BOT_OWNER_ID set but is not an integer; ignoring.")
        OWNER_ID = None
else:
    OWNER_ID = None

# Extensions to load/reload
EXTENSIONS: List[str] = ["wishlist", "igdb", "search"]

# ──────────────────────────────────────────────────────────────────────────────
# Bot subclass with deterministic syncing
# ──────────────────────────────────────────────────────────────────────────────
class ArcaBot(commands.Bot):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._synced_once = False

    async def setup_hook(self):
        for ext in EXTENSIONS:
            await self.load_extension(ext)

        if self._synced_once:
            return

        try:
            if DEV_GUILD_ID:
                guild = discord.Object(id=DEV_GUILD_ID)
                #self.tree.clear_commands(guild=guild)
                #self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                logger.info(f"✅ Synced {len(synced)} command(s) to dev guild {DEV_GUILD_ID}")
            else:
                synced = await self.tree.sync()
                logger.info(f"✅ Synced {len(synced)} global command(s)")
            self._synced_once = True
        except Exception as e:
            logger.exception(f"Failed to sync: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# Instantiate bot
# ──────────────────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
bot = ArcaBot(command_prefix="!", intents=intents)
START_TIME = time.time()

# ──────────────────────────────────────────────────────────────────────────────
# Owner check
# ──────────────────────────────────────────────────────────────────────────────
def is_owner():
    async def predicate(interaction: discord.Interaction) -> bool:
        if OWNER_ID is not None:
            return getattr(interaction.user, "id", None) == OWNER_ID
        return await bot.is_owner(interaction.user)
    return app_commands.check(predicate)

# ──────────────────────────────────────────────────────────────────────────────
# Events
# ──────────────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    logger.info(f"🤖 Logged in as {bot.user}")
    logger.info("📡 Lancement de la boucle Miniflux…")
    asyncio.create_task(run_miniflux_loop(bot))

# ──────────────────────────────────────────────────────────────────────────────
# /sync (dev guild only)
# ──────────────────────────────────────────────────────────────────────────────
@bot.tree.command(name="sync", description="Owner: sync or clear slash commands")
@app_commands.guilds(DEV_GUILD_OBJECT)   # register only in dev guild
@is_owner()
@app_commands.describe(scope="dev | global | clear_dev | clear_global")
async def sync_cmd(
    interaction: discord.Interaction,
    scope: Literal["dev", "global", "clear_dev", "clear_global"] = "dev",
):
    # Runtime safety: reject if invoked outside the dev guild when DEV_GUILD_ID is set
    _dev = os.getenv("DEV_GUILD_ID")
    _dev_id = int(_dev) if _dev else None
    if _dev_id and interaction.guild_id != _dev_id:
        await interaction.response.send_message(
            "\u274c Cette commande est r\u00e9serv\u00e9e au serveur de d\u00e9veloppement.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    try:
        if scope == "dev":
            if not DEV_GUILD_ID:
                return await interaction.followup.send("No DEV_GUILD_ID set.", ephemeral=True)
            guild = discord.Object(id=DEV_GUILD_ID)
            bot.tree.clear_commands(guild=guild)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            return await interaction.followup.send(
                f"Synced {len(synced)} command(s) to dev guild.", ephemeral=True
            )

        if scope == "global":
            synced = await bot.tree.sync()
            return await interaction.followup.send(
                f"Synced {len(synced)} global command(s).", ephemeral=True
            )

        if scope == "clear_dev":
            # Destructive operation: require administrator in the guild
            if not interaction.user.guild_permissions.administrator:
                return await interaction.followup.send(
                    "\u274c Permission refus\u00e9e: administrateur requis.", ephemeral=True
                )
            if not DEV_GUILD_ID:
                return await interaction.followup.send("Aucun DEV_GUILD_ID configur\u00e9.", ephemeral=True)
            guild = discord.Object(id=DEV_GUILD_ID)
            bot.tree.clear_commands(guild=guild)
            await bot.tree.sync(guild=guild)
            return await interaction.followup.send("\u2705 Toutes les commandes du serveur de d\u00e9veloppement ont \u00e9t\u00e9 effac\u00e9es.", ephemeral=True)

        if scope == "clear_global":
            # Destructive operation: require administrator in the guild
            if not interaction.user.guild_permissions.administrator:
                return await interaction.followup.send(
                    "\u274c Permission refus\u00e9e: administrateur requis.", ephemeral=True
                )
            bot.tree.clear_commands(guild=None)
            await bot.tree.sync()
            return await interaction.followup.send("\u2705 Toutes les commandes globales ont \u00e9t\u00e9 effac\u00e9es.", ephemeral=True)

    except Exception as e:
        logger.exception("Sync failed")
        await interaction.followup.send(f"Sync failed: {e}", ephemeral=True)

# ──────────────────────────────────────────────────────────────────────────────
# Aide / Help
# ──────────────────────────────────────────────────────────────────────────────
@bot.tree.command(name="aide", description="Affiche l'aide et le statut du bot")
@app_commands.describe(detail="Afficher la liste complète des commandes")
async def aide_cmd(interaction: discord.Interaction, detail: bool = False):
    """Send a compact, friendly help embed. If `detail` is True, include the full command list.

    This replaces the previous plain-text help message with an embed that shows:
    - short usage notes
    - important wishlist commands
    - bot status (latency, uptime)
    - owner contact (when configured)
    - optionally a full command list
    """
    # Basic usage text
    usage = (
        "Vous pouvez utiliser le bot dans un serveur ou en message privé (DM).\n"
        "Utilisez les commandes slash (préfixe /)."
    )

    # Wishlist quick help
    wishlist_help = (
        "• `/wishlist show [@membre]` — Affiche votre wishlist; si vous passez un membre, affiche la sienne si elle est publique\n"
        "• `/wishlist visibility <public: bool>` — Définir la visibilité de votre wishlist (True = publique, False = privée)\n"
        "• `/wishlist clear` — Vide votre wishlist\n"
        "• `/wishlist calendar <mois> <annee>` — Affiche les sorties du mois pour votre wishlist\n"
        "\n"
        "• `/sorties [platform_id]` — Affiche les prochaines sorties (optionnel: filtre par platform_id)\n"
        "• `/recherche <nom_du_jeu> [platform_id]` — Rechercher un jeu par nom (optionnel: platform_id)"
    )

    # Status
    uptime_seconds = int(time.time() - START_TIME)
    uptime = f"{uptime_seconds // 3600}h {(uptime_seconds % 3600) // 60}m {uptime_seconds % 60}s"
    latency_ms = int(bot.latency * 1000) if bot.latency else None

    embed = discord.Embed(title="Aide — ArcaNews", color=0x2F3136)
    embed.description = usage
    embed.add_field(name="Commandes principales (wishlist)", value=wishlist_help, inline=False)
    status_value = f"Latency: {latency_ms} ms\nUptime: {uptime}"
    if OWNER_ID:
        status_value += f"\nOwner: <@{OWNER_ID}>"
    embed.add_field(name="Statut", value=status_value, inline=False)

    if detail:
        # Build a full list of registered commands
        try:
            cmds = []
            for cmd in bot.tree.walk_commands():
                name = f"/{cmd.qualified_name}"
                desc = getattr(cmd, "description", "") or "(pas de description)"
                cmds.append(f"{name} — {desc}")
            if cmds:
                # join with newlines but keep embed field reasonably sized
                embed.add_field(name="Liste complète des commandes", value="\n".join(cmds), inline=False)
            else:
                embed.add_field(name="Liste complète des commandes", value="(Aucune commande enregistrée)", inline=False)
        except Exception:
            embed.add_field(name="Liste complète des commandes", value="(Impossible de lister les commandes)", inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)

# ──────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────────────────────────────────────
async def main():
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set")
    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
