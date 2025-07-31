import os
import aiohttp
import datetime
import logging
import discord
from discord import app_commands, Embed, Interaction
from discord.ext import commands
from discord.ui import View, Button
from dotenv import load_dotenv
from typing import List, Optional, Dict, Any
from ui_components import UpcomingReleasesView, GameEmbedView

load_dotenv()
CLIENT_ID = os.getenv("IGDB_CLIENT_ID")
CLIENT_SECRET = os.getenv("IGDB_CLIENT_SECRET")

logger = logging.getLogger(__name__)

# Constants
IGDB_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
IGDB_GAMES_URL = "https://api.igdb.com/v4/games"
DAYS_AHEAD = 60
EMBED_COLOR = 0x5865F2
PAGINATION_TIMEOUT = 300  # 5 minutes

PLATFORMS = [
    {"id": 6, "name": "PC (Windows)"},
    {"id": 48, "name": "PlayStation 4"},
    {"id": 49, "name": "Xbox One"},
    {"id": 130, "name": "Nintendo Switch"},
    {"id": 167, "name": "PlayStation 5"},
    {"id": 169, "name": "Xbox Series X"},
    {"id": 184, "name": "Nintendo Switch 2"},
]

class IGDBError(Exception):
    """Custom exception for IGDB API errors"""
    pass

class IGDB(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.token: Optional[str] = None
        self.token_expires_at: Optional[datetime.datetime] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self.wishlist_manager: Optional[Any] = None

    async def cog_load(self):
        """Initialize the cog with a persistent session"""
        self.session = aiohttp.ClientSession()
        # Get wishlist manager reference
        self.wishlist_manager = self.bot.get_cog('WishlistManager')
        logger.info("🎮 IGDB Cog loaded")

    async def cog_unload(self):
        """Clean up resources when unloading"""
        if self.session and not self.session.closed:
            await self.session.close()
        logger.info("🎮 IGDB Cog unloaded")

    def _is_token_valid(self) -> bool:
        """Check if the current token is still valid"""
        return (
            self.token is not None and 
            self.token_expires_at is not None and 
            datetime.datetime.now() < self.token_expires_at
        )

    async def _get_token(self) -> None:
        """Fetch a new OAuth token from Twitch"""
        if self._is_token_valid():
            return

        if not CLIENT_ID or not CLIENT_SECRET:
            raise IGDBError("IGDB credentials not configured")

        try:
            async with self.session.post(
                IGDB_TOKEN_URL,
                params={
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "grant_type": "client_credentials"
                }
            ) as resp:
                if resp.status != 200:
                    raise IGDBError(f"Failed to get token: {resp.status}")
                
                data = await resp.json()
                self.token = data.get("access_token")
                expires_in = data.get("expires_in", 3600)  # Default 1 hour
                
                # Set expiration 5 minutes before actual expiration for safety
                self.token_expires_at = datetime.datetime.now() + datetime.timedelta(
                    seconds=expires_in - 300
                )
                
                logger.info("🎟️ IGDB token refreshed")
                
        except aiohttp.ClientError as e:
            raise IGDBError(f"Network error getting token: {e}")

    def _build_query(self, platform_id: Optional[int]) -> str:
        """Build the IGDB query string"""
        now = int(datetime.datetime.now().timestamp())
        max_timestamp = now + (60 * 60 * 24 * DAYS_AHEAD)

        where_conditions = [
            f"release_dates.date >= {now}",
            f"release_dates.date < {max_timestamp}"
        ]
        
        if platform_id:
            where_conditions.append(f"release_dates.platform = {platform_id}")

        where_clause = " & ".join(where_conditions)
        
        return (
            f"fields id, name, slug, cover.url, release_dates.date, release_dates.platform.name, release_dates.platform.id;"
            f"where {where_clause};"
            f"sort release_dates.date asc;"
            f"limit 50;"
        )

    async def _fetch_games_from_api(self, query: str) -> List[Dict[str, Any]]:
        """Make the actual API request to IGDB"""
        await self._get_token()
        
        headers = {
            "Client-ID": CLIENT_ID,
            "Authorization": f"Bearer {self.token}"
        }

        try:
            async with self.session.post(IGDB_GAMES_URL, headers=headers, data=query) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"IGDB API error {resp.status}: {error_text}")
                    raise IGDBError(f"API request failed: {resp.status}")
                
                return await resp.json()
                
        except aiohttp.ClientError as e:
            raise IGDBError(f"Network error: {e}")

    def _filter_games_by_platform(self, games: List[Dict[str, Any]], platform_id: int) -> List[Dict[str, Any]]:
        """Filter games to only show releases for the specified platform"""
        now = int(datetime.datetime.now().timestamp())
        max_timestamp = now + (60 * 60 * 24 * DAYS_AHEAD)
        
        filtered_games = []
        
        for game in games:
            # Filter release_dates to only include the specified platform within our time window
            filtered_releases = []
            
            for rd in game.get("release_dates", []):
                platform_matches = (
                    rd.get("platform", {}).get("id") == platform_id or 
                    rd.get("platform") == platform_id
                )
                date_in_range = (
                    now <= rd.get("date", 0) < max_timestamp
                )
                
                if platform_matches and date_in_range:
                    filtered_releases.append(rd)
            
            if filtered_releases:
                game["release_dates"] = filtered_releases
                filtered_games.append(game)
        
        return filtered_games

    async def fetch_upcoming_games(self, platform_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Fetch upcoming games, optionally filtered by platform"""
        try:
            query = self._build_query(platform_id)
            games = await self._fetch_games_from_api(query)
            
            # Additional filtering for platform-specific queries
            if platform_id and games:
                games = self._filter_games_by_platform(games, platform_id)
            
            return games
            
        except IGDBError:
            raise
        except Exception as e:
            logger.error(f"Unexpected error fetching games: {e}")
            raise IGDBError(f"Unexpected error: {e}")

    def _format_date(self, timestamp: Optional[int]) -> str:
        """Format a timestamp to a readable date string"""
        if not timestamp:
            return "Date inconnue"
        
        try:
            date_obj = datetime.datetime.fromtimestamp(timestamp)
            # French month names
            months = [
                "janvier", "février", "mars", "avril", "mai", "juin",
                "juillet", "août", "septembre", "octobre", "novembre", "décembre"
            ]
            month_name = months[date_obj.month - 1]
            return f"{date_obj.day} {month_name} {date_obj.year}"
        except (ValueError, IndexError, OSError):
            return "Date invalide"

    def _build_game_embed(self, game: Dict[str, Any]) -> Embed:
        """Build a Discord embed for a single game"""
        name = game.get("name", "Titre inconnu")
        slug = game.get("slug")
        
        # Get the earliest release date
        releases = sorted(
            [r for r in game.get("release_dates", []) if r.get("date")],
            key=lambda r: r["date"]
        )
        
        if releases:
            release_date = releases[0]["date"]
            platform_name = releases[0].get("platform", {}).get("name", "Plateforme inconnue")
        else:
            release_date = game.get("first_release_date")
            platform_name = "Plateforme inconnue"
        
        # Build embed
        embed = Embed(title=name, color=EMBED_COLOR)
        embed.add_field(
            name="📅 Date de sortie", 
            value=self._format_date(release_date), 
            inline=False
        )
        embed.add_field(
            name="🕹️ Plateforme", 
            value=platform_name, 
            inline=False
        )
        
        # Add IGDB link if available
        if slug:
            igdb_url = f"https://www.igdb.com/games/{slug}"
            embed.add_field(
                name="🔗 Lien IGDB", 
                value=f"[Voir sur IGDB]({igdb_url})", 
                inline=False
            )
        
        # Add cover image if available
        cover_url = game.get("cover", {}).get("url")
        if cover_url:
            # Ensure HTTPS and higher resolution
            if cover_url.startswith("//"):
                cover_url = f"https:{cover_url}"
            # Replace thumb with cover_big for better quality
            cover_url = cover_url.replace("t_thumb", "t_cover_big")
            embed.set_image(url=cover_url)
        
        return embed

    def build_embeds(self, games: List[Dict[str, Any]]) -> List[Embed]:
        """Build Discord embeds for a list of games"""
        return [self._build_game_embed(game) for game in games]

    def _get_platform_name(self, platform_id: int) -> str:
        """Get platform name by ID"""
        return next(
            (p["name"] for p in PLATFORMS if p["id"] == platform_id), 
            "plateforme inconnue"
        )

    @app_commands.command(name="sorties", description="🎮 Affiche les prochaines sorties de jeux vidéo")
    @app_commands.describe(platform_id="Filtrer par plateforme (optionnel)")
    async def sorties(self, interaction: Interaction, platform_id: Optional[int] = None):
        """Show upcoming game releases, optionally filtered by platform"""
        await interaction.response.defer()
        
        try:
            games = await self.fetch_upcoming_games(platform_id)
            
            if not games:
                platform_name = self._get_platform_name(platform_id) if platform_id else "toutes les plateformes"
                await interaction.followup.send(
                    f"❌ Aucune sortie trouvée pour **{platform_name}** dans les {DAYS_AHEAD} prochains jours."
                )
                return

            embeds = self.build_embeds(games)
            
            if len(embeds) == 1:
                # Single result with wishlist functionality
                view = GameEmbedView(games[0], self.wishlist_manager)
                await interaction.followup.send(embed=embeds[0], view=view)
            else:
                # Multiple results with pagination and wishlist functionality
                view = UpcomingReleasesView(embeds, games, self.wishlist_manager)
                await interaction.followup.send(embed=embeds[0], view=view)
                
        except IGDBError as e:
            logger.error(f"IGDB error in sorties command: {e}")
            await interaction.followup.send(
                "❌ Erreur lors de la récupération des données IGDB. Veuillez réessayer plus tard."
            )
        except Exception as e:
            logger.error(f"Unexpected error in sorties command: {e}")
            await interaction.followup.send(
                "❌ Une erreur inattendue s'est produite. Veuillez réessayer."
            )

    @sorties.autocomplete("platform_id")
    async def platform_autocomplete(self, interaction: Interaction, current: str) -> List[app_commands.Choice]:
        """Provide autocomplete for platform selection"""
        current_lower = current.lower()
        return [
            app_commands.Choice(name=platform["name"], value=platform["id"])
            for platform in PLATFORMS 
            if current_lower in platform["name"].lower()
        ][:25]  # Discord limit


async def setup(bot: commands.Bot):
    """Setup function for the cog"""
    await bot.add_cog(IGDB(bot))
