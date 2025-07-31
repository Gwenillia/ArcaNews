import os
import logging
import calendar
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from datetime import datetime
import aiosqlite
import discord
from discord import app_commands, Interaction
from discord.ext import commands
from typing import List, Dict, Any, Optional
from ui_components import GameEmbedView, EnhancedPaginatorView
from discord.ui import View, Button

logger = logging.getLogger(__name__)

DB_PATH = "data/wishlist.db"

class WishlistManager(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await self._init_db()
        logger.info("💝 Wishlist Manager loaded")

    async def _init_db(self):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS wishlists (
                    user_id INTEGER,
                    game_id INTEGER,
                    name TEXT,
                    slug TEXT,
                    cover_url TEXT,
                    first_release_date INTEGER,
                    platforms TEXT,
                    added_at TEXT,
                    PRIMARY KEY (user_id, game_id)
                )
            """)
            await db.commit()

    async def add_to_wishlist(self, user_id: int, game: Dict[str, Any]) -> bool:
        try:
            game_id = game.get("id")
            if not game_id:
                return False

            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT 1 FROM wishlists WHERE user_id = ? AND game_id = ?", (user_id, game_id)) as cursor:
                    if await cursor.fetchone():
                        return False

                platforms_field = game.get("platforms", [])
                if isinstance(platforms_field, str):
                    platforms = platforms_field
                else:
                    platforms = ", ".join(p.get("name") for p in platforms_field)

                cover_url = game.get("cover", {}).get("url") or game.get("cover_url")

                await db.execute(
                    """
                    INSERT INTO wishlists (user_id, game_id, name, slug, cover_url, first_release_date, platforms, added_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        game_id,
                        game.get("name", "Titre inconnu"),
                        game.get("slug"),
                        cover_url,
                        game.get("first_release_date"),
                        platforms,
                        datetime.now().isoformat(),
                    ),
                )
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error adding game to wishlist: {e}")
            return False

    async def remove_from_wishlist(self, user_id: int, game_id: int) -> bool:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM wishlists WHERE user_id = ? AND game_id = ?", (user_id, game_id))
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error removing game from wishlist: {e}")
            return False

    async def clear_user_wishlist(self, user_id: int) -> bool:
        """Remove all games from a user's wishlist."""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM wishlists WHERE user_id = ?", (user_id,))
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error clearing wishlist: {e}")
            return False

    async def is_in_wishlist(self, user_id: int, game_id: int) -> bool:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT 1 FROM wishlists WHERE user_id = ? AND game_id = ?", (user_id, game_id)) as cursor:
                return bool(await cursor.fetchone())

    async def get_user_wishlist(self, user_id: int) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT * FROM wishlists WHERE user_id = ?", (user_id,)) as cursor:
                rows = await cursor.fetchall()
                columns = [column[0] for column in cursor.description]
                wishlist = []
                for row in rows:
                    game = dict(zip(columns, row))
                    # Provide alias for compatibility with other views
                    game["id"] = game.get("game_id")
                    wishlist.append(game)
                return wishlist

    def _format_date(self, timestamp: Optional[int]) -> str:
        if not timestamp:
            return "Date inconnue"
        try:
            date_obj = datetime.fromtimestamp(timestamp)
            months = [
                "janvier", "février", "mars", "avril", "mai", "juin",
                "juillet", "août", "septembre", "octobre", "novembre", "décembre"
            ]
            month_name = months[date_obj.month - 1]
            return f"{date_obj.day} {month_name} {date_obj.year}"
        except (ValueError, IndexError, OSError):
            return "Date invalide"

    def _build_wishlist_embed(self, game: Dict[str, Any], index: int, total: int) -> discord.Embed:
        embed = discord.Embed(
            title=game.get("name", "Titre inconnu"),
            color=0xFF69B4
        )
        embed.set_author(name=f"💝 Wishlist • {index + 1} / {total}")
        embed.add_field(
            name="📅 Date de sortie",
            value=self._format_date(game.get("first_release_date")),
            inline=False
        )
        embed.add_field(
            name="🕹️ Plateforme(s)",
            value=game.get("platforms", "Plateforme inconnue"),
            inline=False
        )
        if game.get("added_at"):
            try:
                added_date = datetime.fromisoformat(game["added_at"])
                embed.add_field(
                    name="💝 Ajouté le",
                    value=added_date.strftime("%d/%m/%Y"),
                    inline=False
                )
            except ValueError:
                pass
        if game.get("slug"):
            embed.add_field(
                name="🔗 Lien IGDB",
                value=f"[Voir sur IGDB](https://www.igdb.com/games/{game['slug']})",
                inline=False
            )
        cover_url = game.get("cover_url")
        if cover_url:
            if cover_url.startswith("//"):
                cover_url = f"https:{cover_url}"
            cover_url = cover_url.replace("t_thumb", "t_cover_big")
            embed.set_image(url=cover_url)
        return embed

    def _generate_calendar_image(self, month: int, year: int, events: Dict[int, List[str]]) -> str:
        """Generate a PNG calendar highlighting release events."""
        cal = calendar.Calendar(firstweekday=0)
        month_matrix = cal.monthdayscalendar(year, month)

        # Build the text and color matrix for the table
        table_text: List[List[str]] = []
        cell_colors: List[List[str]] = []
        for week in month_matrix:
            row_text = []
            row_colors = []
            for idx, day in enumerate(week):
                if day == 0:
                    row_text.append("")
                    row_colors.append("#FFFFFF")
                    continue

                lines = [str(day)]
                if day in events:
                    for name in events[day][:3]:
                        line = name[:20] + ("..." if len(name) > 20 else "")
                        lines.append(line)
                row_text.append("\n".join(lines))
                row_colors.append("#F0F0F0" if idx >= 5 else "#FFFFFF")

            table_text.append(row_text)
            cell_colors.append(row_colors)

        fig, ax = plt.subplots(figsize=(10, 1 + len(month_matrix) * 1.5))
        ax.set_axis_off()

        table = ax.table(
            cellText=table_text,
            cellColours=cell_colors,
            colLabels=["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"],
            cellLoc="left",
            loc="center",
        )

        table.auto_set_font_size(False)
        table.set_fontsize(10)

        # Adjust cell dimensions for readability
        for (row, col), cell in table.get_celld().items():
            cell.set_height(0.15)
            cell.set_width(0.14)

        month_name = calendar.month_name[month]
        ax.set_title(f"{month_name} {year}", fontsize=16, pad=20)

        output_path = "/tmp/calendar.png"
        plt.savefig(output_path, bbox_inches="tight")
        plt.close(fig)
        return output_path

    @app_commands.command(name="wishlist", description="💝 Affiche votre wishlist de jeux")
    async def wishlist_command(self, interaction: Interaction):
        await interaction.response.defer()
        try:
            user_wishlist = await self.get_user_wishlist(interaction.user.id)
            if not user_wishlist:
                embed = discord.Embed(
                    title="💝 Votre Wishlist",
                    description="Votre wishlist est vide !\n\nUtilisez `/recherche` pour trouver des jeux à ajouter.",
                    color=0xFF69B4
                )
                await interaction.followup.send(embed=embed)
                return

            embeds = [self._build_wishlist_embed(game, i, len(user_wishlist)) for i, game in enumerate(user_wishlist)]

            if len(embeds) == 1:
                view = GameEmbedView(user_wishlist[0], self)
                await interaction.followup.send(embed=embeds[0], view=view)
            else:
                view = EnhancedPaginatorView(embeds, user_wishlist, self)
                await interaction.followup.send(embed=embeds[0], view=view)

        except Exception as e:
            logger.error(f"Error displaying wishlist: {e}")
            await interaction.followup.send("❌ Une erreur s'est produite lors de l'affichage de votre wishlist.")

    @app_commands.command(name="wishlist-clear", description="🗑️ Vide votre wishlist")
    async def clear_wishlist(self, interaction: Interaction):
        await interaction.response.defer()
        try:
            user_wishlist = await self.get_user_wishlist(interaction.user.id)
            if not user_wishlist:
                await interaction.followup.send("💝 Votre wishlist est déjà vide !")
                return

            view = ClearWishlistView(self, interaction.user.id)
            embed = discord.Embed(
                title="⚠️ Confirmation",
                description=f"Êtes-vous sûr de vouloir supprimer tous les **{len(user_wishlist)} jeux** de votre wishlist ?\n\n**Cette action est irréversible !**",
                color=0xFF5555
            )
            await interaction.followup.send(embed=embed, view=view)
        except Exception as e:
            logger.error(f"Error clearing wishlist: {e}")
            await interaction.followup.send("❌ Une erreur s'est produite.")

    @app_commands.command(name="calendar", description="🗓️ Affiche les sorties de votre wishlist pour un mois donné")
    @app_commands.describe(mois="Mois (1-12)", annee="Année")
    async def calendar_command(self, interaction: Interaction, mois: int, annee: int):
        await interaction.response.defer()
        if mois < 1 or mois > 12:
            await interaction.followup.send("❌ Mois invalide. Utilisez un nombre entre 1 et 12.")
            return

        try:
            start = datetime(annee, mois, 1)
            if mois == 12:
                end = datetime(annee + 1, 1, 1)
            else:
                end = datetime(annee, mois + 1, 1)

            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT name, first_release_date FROM wishlists WHERE user_id = ? AND first_release_date >= ? AND first_release_date < ?",
                    (interaction.user.id, int(start.timestamp()), int(end.timestamp())),
                ) as cursor:
                    rows = await cursor.fetchall()

            events: Dict[int, List[str]] = {}
            for name, ts in rows:
                try:
                    day = datetime.fromtimestamp(ts).day
                except (ValueError, OSError):
                    continue
                events.setdefault(day, [])
                if name not in events[day]:
                    events[day].append(name)

            image_path = self._generate_calendar_image(mois, annee, events)
            await interaction.followup.send(
                "Voici les sorties du mois de ta wishlist !",
                file=discord.File(image_path, filename="calendar.png"),
            )
        except Exception as e:
            logger.error(f"Error generating calendar: {e}")
            await interaction.followup.send("❌ Une erreur s'est produite lors de la génération du calendrier.")

class ClearWishlistView(View):
    """Confirmation view to clear the entire wishlist."""

    def __init__(self, wishlist_manager: WishlistManager, user_id: int):
        super().__init__(timeout=60)
        self.wishlist_manager = wishlist_manager
        self.user_id = user_id

    @discord.ui.button(label="✅ Supprimer", style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction: Interaction, button: Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ Vous ne pouvez pas utiliser ce bouton.", ephemeral=True
            )
            return

        success = await self.wishlist_manager.clear_user_wishlist(self.user_id)
        if success:
            await interaction.response.edit_message(
                content="✅ Wishlist vidée !", embed=None, view=None
            )
        else:
            await interaction.response.edit_message(
                content="❌ Erreur lors de la suppression.", view=None
            )

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: Interaction, button: Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ Vous ne pouvez pas utiliser ce bouton.", ephemeral=True
            )
            return
        await interaction.response.edit_message(
            content="Opération annulée.", embed=None, view=None
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(WishlistManager(bot))
