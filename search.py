import logging
import discord
from discord import app_commands, Interaction
from discord.ext import commands
from typing import List, Dict, Any, Optional
from igdb import IGDB, IGDBError
from wishlist import WishlistManager
from ui_components import GameEmbedView, EnhancedPaginatorView

logger = logging.getLogger(__name__)

class GameSearch(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.igdb_cog: Optional[IGDB] = None
        self.wishlist_manager: Optional[WishlistManager] = None

    async def cog_load(self):
        """Initialize dependencies"""
        self.igdb_cog = self.bot.get_cog('IGDB')
        self.wishlist_manager = self.bot.get_cog('WishlistManager')
        logger.info("🔍 Game Search Cog loaded")

    def _build_search_query(self, game_name: str, platform_id: Optional[int] = None) -> str:
        base_fields = (
            'fields name, slug, cover.url, first_release_date, platforms.name, '
            'platforms.id, summary, rating, genres.name, involved_companies.company.name;'
        )
        
        search_part = f'search "{game_name}";'
        limit_part = 'limit 25;'

        if platform_id:
            # Syntaxe correcte pour filtrer par plateforme
            where_part = f'where platforms = [{platform_id}];'
            query = f'{base_fields}{search_part}{where_part}{limit_part}'
        else:
            query = f'{base_fields}{search_part}{limit_part}'

        return query

    async def _search_games_api(self, game_name: str, platform_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Search for games using IGDB API"""
        if not self.igdb_cog:
            raise IGDBError("IGDB service not available")

        query = self._build_search_query(game_name, platform_id)
        return await self.igdb_cog._fetch_games_from_api(query)

    def _build_search_embed(self, game: Dict[str, Any]) -> discord.Embed:
        name = game.get("name", "Titre inconnu")
        slug = game.get("slug")

        embed = discord.Embed(
            title=name,
            color=0x5865F2
        )

        # 📅 Date de sortie
        release_date = game.get("first_release_date")
        if release_date:
            formatted_date = self.igdb_cog._format_date(release_date)
            embed.add_field(
                name="📅 Date de sortie",
                value=formatted_date,
                inline=False
            )

        # 🕹️ Plateformes
        platforms = game.get("platforms", [])
        if platforms:
            platform_names = [p.get("name", "Inconnu") for p in platforms[:5]]
            platform_text = ", ".join(platform_names)
            if len(platforms) > 5:
                platform_text += f" (+{len(platforms) - 5} autres)"
            embed.add_field(
                name="🕹️ Plateforme(s)",
                value=platform_text,
                inline=False
            )

        # 🏢 Développeur
        companies = game.get("involved_companies", [])
        if companies:
            company_names = [c.get("company", {}).get("name", "Inconnu") for c in companies[:2]]
            embed.add_field(
                name="🏢 Développeur",
                value=", ".join(company_names),
                inline=False
            )

        # 🔗 IGDB
        if slug:
            igdb_url = f"https://www.igdb.com/games/{slug}"
            embed.add_field(
                name="🔗 Lien IGDB",
                value=f"[Voir sur IGDB]({igdb_url})",
                inline=False
            )

        # 🖼️ Cover
        cover_url = game.get("cover", {}).get("url")
        if cover_url:
            if cover_url.startswith("//"):
                cover_url = f"https:{cover_url}"
            cover_url = cover_url.replace("t_thumb", "t_cover_big")
            embed.set_image(url=cover_url)

        return embed

    @app_commands.command(
        name="recherche",
        description="🔍 Rechercher un jeu vidéo par nom (et plateforme optionnelle)"
    )
    @app_commands.describe(
        platform_id="Filtrer par plateforme (optionnel)",
        game_name="Nom du jeu à rechercher"
    )
    async def recherche(self, interaction: Interaction, game_name: str, platform_id: Optional[int] = None):
        """Search for video games by name"""
        await interaction.response.defer()
        
        try:
            games = await self._search_games_api(game_name, platform_id)
            
            if not games:
                embed = discord.Embed(
                    title="❌ Aucun résultat",
                    description=f"Aucun jeu trouvé pour **{game_name}**",
                    color=0xFF5555
                )
                await interaction.followup.send(embed=embed)
                return

            # Build embeds with wishlist buttons
            embeds = []
            for game in games:
                embed = self._build_search_embed(game)
                embeds.append(embed)
            
            if len(embeds) == 1:
                # Single result with wishlist button
                view = GameEmbedView(games[0], self.wishlist_manager)
                await interaction.followup.send(embed=embeds[0], view=view)
            else:
                # Multiple results with pagination and wishlist buttons
                view = EnhancedPaginatorView(embeds, games, self.wishlist_manager)
                await interaction.followup.send(embed=embeds[0], view=view)
                
        except IGDBError as e:
            logger.error(f"IGDB error in search command: {e}")
            await interaction.followup.send(
                "❌ Erreur lors de la recherche. Veuillez réessayer plus tard."
            )
        except Exception as e:
            logger.error(f"Unexpected error in search command: {e}")
            await interaction.followup.send(
                "❌ Une erreur inattendue s'est produite. Veuillez réessayer."
            )

    @recherche.autocomplete("platform_id")
    async def platform_autocomplete(self, interaction: Interaction, current: str) -> List[app_commands.Choice[int]]:
        from igdb import PLATFORMS  # Ou place le tableau dans un fichier partagé si besoin
        current_lower = current.lower()
        return [
            app_commands.Choice(name=p["name"], value=p["id"])
            for p in PLATFORMS
            if current_lower in p["name"].lower()
        ][:25]


async def setup(bot: commands.Bot):
    """Setup function for the cog"""
    await bot.add_cog(GameSearch(bot))
