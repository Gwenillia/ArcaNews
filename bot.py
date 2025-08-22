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

# Extensions to load/reload (must be importable: wishlist.py, igdb.py, search.py, bookmarks.py)
EXTENSIONS: List[str] = ["wishlist", "igdb", "search", "bookmarks"]

# Helper: safe decorator for dev-only commands (no-op when DEV_GUILD_ID is not set)
def dev_guilds_decorator():
    if DEV_GUILD_OBJECT:
        return app_commands.guilds(DEV_GUILD_OBJECT)
    # identity decorator when no dev guild is configured
    def identity(f):
        return f
    return identity

# ──────────────────────────────────────────────────────────────────────────────
# Bot subclass with deterministic syncing
# ──────────────────────────────────────────────────────────────────────────────
class ArcaBot(commands.Bot):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    async def setup_hook(self):
        # 1) Load extensions first so all commands are registered
        for ext in EXTENSIONS:
            await self.load_extension(ext)

        try:
            # 2) If a dev guild is configured, overlay globals there for instant testing
            if DEV_GUILD_ID:
                dev = discord.Object(id=DEV_GUILD_ID)
                # Copy current global commands to dev guild and sync
                self.tree.copy_global_to(guild=dev)
                dev_synced = await self.tree.sync(guild=dev)
                logger.info(f"✅ Dev overlay: {len(dev_synced)} command(s) in guild {DEV_GUILD_ID}")

            # 3) Publish/refresh global commands (last)
            global_synced = await self.tree.sync()
            logger.info(f"✅ Global sync: {len(global_synced)} command(s) published")
        except Exception:
            logger.exception("setup_hook sync failed")

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
# /sync (dev guild only; safe, no destructive clear)
# ──────────────────────────────────────────────────────────────────────────────
@bot.tree.command(name="sync", description="Owner: sync commands")
@dev_guilds_decorator()
@is_owner()
@app_commands.describe(scope="dev | global")
async def sync_cmd(
    interaction: discord.Interaction,
    scope: Literal["dev", "global"] = "dev",
):
    # If a dev guild is configured, reject invocations outside it
    if DEV_GUILD_ID and interaction.guild_id != DEV_GUILD_ID:
        await interaction.response.send_message(
            "\u274c Cette commande est r\u00e9serv\u00e9e au serveur de d\u00e9veloppement.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    try:
        if scope == "dev":
            if not DEV_GUILD_ID:
                return await interaction.followup.send("No DEV_GUILD_ID set.", ephemeral=True)
            dev = discord.Object(id=DEV_GUILD_ID)
            # mirror current globals into dev and sync
            bot.tree.copy_global_to(guild=dev)
            synced = await bot.tree.sync(guild=dev)
            return await interaction.followup.send(f"✅ Synced {len(synced)} command(s) to dev guild.", ephemeral=True)

        if scope == "global":
            synced = await bot.tree.sync()
            return await interaction.followup.send(f"✅ Synced {len(synced)} global command(s).", ephemeral=True)

    except Exception as e:
        logger.exception("Sync failed")
        await interaction.followup.send(f"Sync failed: {e}", ephemeral=True)

# ──────────────────────────────────────────────────────────────────────────────
# /debug_commands (dev guild only)
# ──────────────────────────────────────────────────────────────────────────────
@bot.tree.command(name="debug_commands", description="Owner: list registered app commands")
@dev_guilds_decorator()
@is_owner()
async def debug_commands(interaction: discord.Interaction):
    if DEV_GUILD_ID and interaction.guild_id != DEV_GUILD_ID:
        await interaction.response.send_message(
            "\u274c Cette commande est r\u00e9serv\u00e9e au serveur de d\u00e9veloppement.", ephemeral=True
        )
        return

    lines = []
    try:
        for cmd in bot.tree.walk_commands():
            guild_ids = getattr(cmd, "_guild_ids", None)
            scope = "GLOBAL" if not guild_ids else f"GUILDS: {list(guild_ids)}"
            desc = getattr(cmd, "description", "") or "(no description)"
            lines.append(f"/{cmd.qualified_name} — {scope} — {desc}")
        text = "\n".join(lines) or "(none)"
    except Exception as e:
        text = f"(error collecting commands: {e})"

    # send as code block to avoid embed limits
    await interaction.response.send_message(f"```{text}```", ephemeral=True)

# ──────────────────────────────────────────────────────────────────────────────
# Aide / Help (global)
# ──────────────────────────────────────────────────────────────────────────────
@bot.tree.command(name="aide", description="Affiche l'aide et le statut du bot")
@app_commands.describe(detail="Afficher la liste complète des commandes")
async def aide_cmd(interaction: discord.Interaction, detail: bool = False):
    """Compact help embed + status. Uses slash commands only (global)."""
    usage = (
        "Vous pouvez utiliser le bot dans un serveur ou en message privé (DM).\n"
        "Utilisez les commandes slash (préfixe /)."
    )

    wishlist_help = (
        "• `/wishlist show [@membre]` — Affiche votre wishlist; si vous passez un membre, affiche la sienne si elle est publique\n"
        "• `/wishlist visibility <public: bool>` — Définir la visibilité de votre wishlist (True = publique, False = privée)\n"
        "• `/wishlist clear` — Vide votre wishlist\n"
        "• `/wishlist calendar <mois> <annee>` — Affiche les sorties du mois pour votre wishlist\n"
        "\n"
        "• `/sorties [platform_id]` — Affiche les prochaines sorties (optionnel: filtre par platform_id)\n"
        "• `/recherche <nom_du_jeu> [platform_id]` — Rechercher un jeu par nom (optionnel: platform_id)"
    )

    bookmarks_help = (
        "• `/news favoris` — Affiche vos favoris (news)\n"
        "• Pour ajouter un favori : cliquez sur le bouton \"🔖 Favori\" sous une news publiée.\n"
        "• Vous pouvez consulter, parcourir et publier vos favoris depuis le panneau interactif."
    )

    uptime_seconds = int(time.time() - START_TIME)
    uptime = f"{uptime_seconds // 3600}h {(uptime_seconds % 3600) // 60}m {uptime_seconds % 60}s"
    latency_ms = int(bot.latency * 1000) if bot.latency else None

    embed = discord.Embed(title="Aide — ArcaNews", color=0x2F3136)
    embed.description = usage
    embed.add_field(name="Commandes principales (wishlist)", value=wishlist_help, inline=False)
    embed.add_field(name="Favoris (news)", value=bookmarks_help, inline=False)
    status_value = f"Latency: {latency_ms} ms\nUptime: {uptime}"
    if OWNER_ID:
        status_value += f"\nOwner: <@{OWNER_ID}>"
    embed.add_field(name="Statut", value=status_value, inline=False)

    if detail:
        try:
            cmds = []
            for cmd in bot.tree.walk_commands():
                name = f"/{cmd.qualified_name}"
                desc = getattr(cmd, "description", "") or "(pas de description)"
                scope = "GLOBAL" if not getattr(cmd, "_guild_ids", None) else "DEV"
                cmds.append(f"{name} — {desc} [{scope}]")
            embed.add_field(
                name="Liste complète des commandes",
                value="\n".join(cmds) if cmds else "(Aucune commande enregistrée)",
                inline=False
            )
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