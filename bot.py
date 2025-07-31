import os
import asyncio
import logging
import discord
from discord.ext import commands
from dotenv import load_dotenv
from miniflux import run_miniflux_loop

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID"))  # Your test server ID

# Setup logger
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# Discord bot
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    logger.info(f"🤖 Logged in as {bot.user}")
    logger.info("📡 Lancement de la boucle Miniflux...")

    try:
        dev_guild = discord.Object(id=DEV_GUILD_ID)
        bot.tree.copy_global_to(guild=dev_guild)
        synced = await bot.tree.sync(guild=dev_guild)
        logger.info(f"🌐 Synced {len(synced)} slash command(s)")
    except Exception as e:
        logger.error(f"❌ Failed to sync commands: {e}")

    bot.loop.create_task(run_miniflux_loop(bot))

async def main():
    await bot.load_extension("wishlist")  # Ensure wishlist is loaded
    await bot.load_extension("igdb")  # flux/__init__.py → setup(bot)
    await bot.load_extension("search")
    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
