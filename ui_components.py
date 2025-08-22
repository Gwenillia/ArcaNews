import discord
from discord import Interaction
from datetime import datetime
from discord.ui import View, Button
from typing import List, Dict, Any, Optional
import logging

logger = logging.getLogger(__name__)

PAGINATION_TIMEOUT = 300  # 5 minutes

class GameEmbedView(View):
    """Reusable view for single game embeds with wishlist functionality"""
    
    def __init__(self, game: Dict[str, Any], wishlist_manager, show_wishlist_button: bool = True):
        super().__init__(timeout=PAGINATION_TIMEOUT)
        self.game = game
        self.wishlist_manager = wishlist_manager
        
        if show_wishlist_button and wishlist_manager:
            self.add_item(WishlistButton(game, wishlist_manager))

    @discord.ui.button(label="🗑️", style=discord.ButtonStyle.danger)
    async def delete_button(self, interaction: Interaction, button: Button):
        """Delete the message"""
        await interaction.response.edit_message(content="Message supprimé.", embed=None, view=None)


class PaginatorView(View):
    """Base reusable pagination view for embeds"""
    
    def __init__(self, embeds: List[discord.Embed]):
        super().__init__(timeout=PAGINATION_TIMEOUT)
        self.embeds = embeds
        self.current_page = 0
        self.max_page = len(embeds) - 1
        
        # Remove navigation if only one page
        if len(embeds) <= 1:
            self.remove_item(self.previous_button)
            self.remove_item(self.next_button)
        else:
            self._update_buttons()

    def _update_buttons(self):
        """Update button states based on current page"""
        self.previous_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page == self.max_page

    @discord.ui.button(label="◀️ Précédent", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction: Interaction, button: Button):
        """Go to previous page"""
        if self.current_page > 0:
            self.current_page -= 1
            self._update_buttons()
            await self._update_page(interaction)

    @discord.ui.button(label="Suivant ▶️", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: Interaction, button: Button):
        """Go to next page"""
        if self.current_page < self.max_page:
            self.current_page += 1
            self._update_buttons()
            await self._update_page(interaction)

    @discord.ui.button(label="🗑️", style=discord.ButtonStyle.danger)
    async def delete_button(self, interaction: Interaction, button: Button):
        """Delete the message"""
        await interaction.response.edit_message(content="Message supprimé.", embed=None, view=None)

    async def _update_page(self, interaction: Interaction):
        """Update the current page - can be overridden by subclasses"""
        await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)

    async def on_timeout(self):
        """Disable all buttons when view times out"""
        for item in self.children:
            item.disabled = True
        
        # Try to edit the message to show timeout, but don't fail if we can't
        try:
            if hasattr(self, 'message') and self.message:
                await self.message.edit(view=self)
        except discord.NotFound:
            pass  # Message was deleted
        except discord.HTTPException:
            pass  # Other HTTP error, ignore


class WishlistButton(discord.ui.Button):
    """Reusable button to add/remove games from wishlist - adapts per user"""
    
    def __init__(self, game: Dict[str, Any], wishlist_manager):
        self.game = game
        self.wishlist_manager = wishlist_manager
        
        # Use neutral label since multiple users will see this
        super().__init__(
            label="💝 Wishlist",
            style=discord.ButtonStyle.secondary,
            emoji="💝"
        )

    async def callback(self, interaction: Interaction):
        """Handle wishlist button click - creates personalized response"""
        if not self.wishlist_manager:
            await interaction.response.send_message(
                "❌ Service de wishlist non disponible.", ephemeral=True
            )
            return

        user_id = interaction.user.id
        game_id = self.game.get("id")
        game_name = self.game.get("name", "Jeu inconnu")
        
        if not game_id:
            await interaction.response.send_message(
                "❌ Impossible d'ajouter ce jeu à la wishlist.", ephemeral=True
            )
            return

        try:
            # Check if already in wishlist for THIS specific user
            is_in_wishlist = await self.wishlist_manager.is_in_wishlist(user_id, game_id)
            
            if is_in_wishlist:
                # Show personalized view to remove from wishlist
                view = PersonalWishlistView(self.game, self.wishlist_manager, user_id, is_in_wishlist=True)
                await interaction.response.send_message(
                    f"💖 **{game_name}** est dans votre wishlist !", 
                    view=view, 
                    ephemeral=True
                )
            else:
                # Show personalized view to add to wishlist
                view = PersonalWishlistView(self.game, self.wishlist_manager, user_id, is_in_wishlist=False)
                await interaction.response.send_message(
                    f"💝 **{game_name}** n'est pas dans votre wishlist.", 
                    view=view, 
                    ephemeral=True
                )
        
        except Exception as e:
            logger.error(f"Error handling wishlist button: {e}")
            await interaction.response.send_message(
                "❌ Une erreur s'est produite.", ephemeral=True
            )


    async def update_button_state(self, user_id: int) -> None:
        """Update label and style to reflect wishlist membership"""
        if not self.wishlist_manager:
            return

        game_id = self.game.get("id")
        if not game_id:
            return

        try:
            in_wishlist = await self.wishlist_manager.is_in_wishlist(user_id, game_id)
        except Exception as e:
            logger.error(f"Error updating wishlist button state: {e}")
            return

        if in_wishlist:
            self.style = discord.ButtonStyle.success
            self.label = "💖 Dans votre wishlist"
        else:
            self.style = discord.ButtonStyle.secondary
            self.label = "💝 Wishlist"

class PersonalWishlistView(discord.ui.View):
    """Personal view for wishlist actions - only visible to the user who clicked"""
    
    def __init__(self, game: Dict[str, Any], wishlist_manager, user_id: int, is_in_wishlist: bool):
        super().__init__(timeout=60)  # Shorter timeout for personal actions
        self.game = game
        self.wishlist_manager = wishlist_manager
        self.user_id = user_id
        self.is_in_wishlist = is_in_wishlist
        
        # Add appropriate button based on current state
        if is_in_wishlist:
            self.add_item(RemoveFromWishlistButton(game, wishlist_manager, user_id))
        else:
            self.add_item(AddToWishlistButton(game, wishlist_manager, user_id))


class AddToWishlistButton(discord.ui.Button):
    """Button to add game to wishlist"""
    
    def __init__(self, game: Dict[str, Any], wishlist_manager, user_id: int):
        self.game = game
        self.wishlist_manager = wishlist_manager
        self.user_id = user_id
        
        super().__init__(
            label="💝 Ajouter à ma wishlist",
            style=discord.ButtonStyle.success,
            emoji="💝"
        )

    async def callback(self, interaction: Interaction):
        """Add game to user's wishlist"""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ Vous ne pouvez pas utiliser ce bouton.", ephemeral=True
            )
            return
            
        game_name = self.game.get("name", "ce jeu")
        
        try:
            success = await self.wishlist_manager.add_to_wishlist(self.user_id, self.game)
            
            if success:
                # Update to removal button
                self.view.clear_items()
                self.view.add_item(RemoveFromWishlistButton(self.game, self.wishlist_manager, self.user_id))
                
                await interaction.response.edit_message(
                    content=f"✅ **{game_name}** ajouté à votre wishlist !",
                    view=self.view
                )
            else:
                await interaction.response.send_message(
                    "❌ Erreur lors de l'ajout à la wishlist.", ephemeral=True
                )
        
        except Exception as e:
            logger.error(f"Error adding to wishlist: {e}")
            await interaction.response.send_message(
                "❌ Une erreur s'est produite.", ephemeral=True
            )


class RemoveFromWishlistButton(discord.ui.Button):
    """Button to remove game from wishlist"""
    
    def __init__(self, game: Dict[str, Any], wishlist_manager, user_id: int):
        self.game = game
        self.wishlist_manager = wishlist_manager
        self.user_id = user_id
        
        super().__init__(
            label="💔 Retirer de ma wishlist",
            style=discord.ButtonStyle.danger,
            emoji="💔"
        )

    async def callback(self, interaction: Interaction):
        """Remove game from user's wishlist"""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ Vous ne pouvez pas utiliser ce bouton.", ephemeral=True
            )
            return
            
        game_id = self.game.get("id")
        game_name = self.game.get("name", "ce jeu")
        
        try:
            success = await self.wishlist_manager.remove_from_wishlist(self.user_id, game_id)
            
            if success:
                # Update to add button
                self.view.clear_items()
                self.view.add_item(AddToWishlistButton(self.game, self.wishlist_manager, self.user_id))
                
                await interaction.response.edit_message(
                    content=f"💔 **{game_name}** retiré de votre wishlist.",
                    view=self.view
                )
            else:
                await interaction.response.send_message(
                    "❌ Erreur lors de la suppression.", ephemeral=True
                )
        
        except Exception as e:
            logger.error(f"Error removing from wishlist: {e}")
            await interaction.response.send_message(
                "❌ Une erreur s'est produite.", ephemeral=True
            )

class EnhancedPaginatorView(PaginatorView):
    """Extended paginator with additional functionality for games"""
    
    def __init__(self, embeds: List[discord.Embed], games: List[Dict[str, Any]], 
                 wishlist_manager, show_wishlist_buttons: bool = True):
        super().__init__(embeds)
        self.games = games
        self.wishlist_manager = wishlist_manager
        self.show_wishlist_buttons = show_wishlist_buttons
        
        # Add wishlist button for the first game if enabled
        if show_wishlist_buttons and wishlist_manager and games:
            self.wishlist_button = WishlistButton(games[0], wishlist_manager)
            self.add_item(self.wishlist_button)

    async def _update_page(self, interaction: Interaction):
        """Update page and wishlist button state"""
        # Update wishlist button for current game
        if hasattr(self, 'wishlist_button') and self.games:
            current_game = self.games[self.current_page]
            self.wishlist_button.game = current_game
            await self.wishlist_button.update_button_state(interaction.user.id)
        
        await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)


class UpcomingReleasesView(PaginatorView):
    """Specialized view for upcoming releases with wishlist functionality"""
    
    def __init__(self, embeds: List[discord.Embed], games: List[Dict[str, Any]], 
                 wishlist_manager):
        super().__init__(embeds)
        self.games = games
        self.wishlist_manager = wishlist_manager
        
        # Add wishlist button if available
        if wishlist_manager and games:
            self.wishlist_button = WishlistButton(games[0], wishlist_manager)
            # Insert wishlist button before delete button
            self.add_item(self.wishlist_button)

    async def _update_page(self, interaction: Interaction):
        """Update page and wishlist button for upcoming releases"""
        # Update wishlist button for current game
        if hasattr(self, 'wishlist_button') and self.games:
            current_game = self.games[self.current_page]
            # Convert upcoming release format to search format for wishlist
            wishlist_game = {
                "id": current_game.get("id"),
                "name": current_game.get("name"),
                "slug": current_game.get("slug"),
                "cover": current_game.get("cover"),
                "first_release_date": None,  # Will be determined from release_dates
                "platforms": []  # Will be determined from release_dates
            }
            
            # Extract first release date and platforms from release_dates
            release_dates = current_game.get("release_dates", [])
            if release_dates:
                # Sort by date and get the earliest
                sorted_releases = sorted(
                    [r for r in release_dates if r.get("date")],
                    key=lambda r: r["date"]
                )
                if sorted_releases:
                    wishlist_game["first_release_date"] = sorted_releases[0]["date"]
                
                # Collect unique platforms
                platforms = []
                for rd in release_dates:
                    platform = rd.get("platform")
                    if platform:
                        if isinstance(platform, dict):
                            platform_name = platform.get("name")
                        else:
                            platform_name = str(platform)
                        
                        if platform_name and platform_name not in platforms:
                            platforms.append({"name": platform_name})
                
                wishlist_game["platforms"] = platforms
            
            self.wishlist_button.game = wishlist_game
            await self.wishlist_button.update_button_state(interaction.user.id)
        
        await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)


class GameSelectButton(discord.ui.Button):
    """Button shown in a list panel to open the detailed view for a game."""

    def __init__(self, index: int, label: str, parent_view: "WishlistListPanelView"):
        # Use a compact label (Discord limits apply) and secondary style
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self.index = index
        self.parent_view = parent_view

    async def callback(self, interaction: Interaction):
        # Only use the game data from the parent view
        try:
            games = getattr(self.parent_view, "games", [])
            if not games or self.index < 0 or self.index >= len(games):
                await interaction.response.send_message("❌ Jeu introuvable.", ephemeral=True)
                return

            game = games[self.index]

            # Use the existing GameEmbedView to display details and wishlist actions
            view = GameEmbedView(game, self.parent_view.wishlist_manager)

            # Build embed similar to GameEmbedView's expectations (the view will render full details)
            embed = discord.Embed(title=game.get("name", "Titre inconnu"), color=0xFF69B4)
            # Show release date if available
            fr_date = None
            ts = game.get("first_release_date") or game.get("first_release_date")
            if ts:
                try:
                    fr_date = datetime.fromtimestamp(int(ts)).strftime("%d %B %Y")
                except Exception:
                    fr_date = None

            if fr_date:
                embed.add_field(name="📅 Date de sortie", value=fr_date, inline=False)

            # Link to IGDB when slug available
            slug = game.get("slug") or game.get("slug")
            if slug:
                embed.add_field(name="🔗 Lien IGDB", value=f"[Voir sur IGDB](https://www.igdb.com/games/{slug})", inline=False)

            cover_url = game.get("cover_url") or (game.get("cover", {}) or {}).get("url")
            if cover_url:
                if cover_url.startswith("//"):
                    cover_url = f"https:{cover_url}"
                cover_url = cover_url.replace("t_thumb", "t_cover_big")
                embed.set_image(url=cover_url)

            # Send ephemeral detailed view so the invoker gets the interactive wishlist buttons
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        except Exception as e:
            logger.error(f"Error opening game detail from list: {e}")
            try:
                await interaction.response.send_message("❌ Une erreur est survenue.", ephemeral=True)
            except Exception:
                pass


class GameSelect(discord.ui.Select):
    """Select menu for choosing a game from a page."""

    def __init__(self, options: List[discord.SelectOption], parent_view: "WishlistListPanelView"):
        super().__init__(placeholder="Choisir un jeu...", min_values=1, max_values=1, options=options)
        self.parent_view = parent_view

    async def callback(self, interaction: Interaction):
        try:
            # value is the absolute index in the games list
            selected = int(self.values[0])
            games = getattr(self.parent_view, "games", [])
            if selected < 0 or selected >= len(games):
                await interaction.response.send_message("❌ Jeu introuvable.", ephemeral=True)
                return

            game = games[selected]
            view = GameEmbedView(game, self.parent_view.wishlist_manager)

            embed = discord.Embed(title=game.get("name", "Titre inconnu"), color=0xFF69B4)
            ts = game.get("first_release_date")
            if ts:
                try:
                    date_str = datetime.fromtimestamp(int(ts)).strftime("%d %B %Y")
                    embed.add_field(name="📅 Date de sortie", value=date_str, inline=False)
                except Exception:
                    pass

            slug = game.get("slug")
            if slug:
                embed.add_field(name="🔗 Lien IGDB", value=f"[Voir sur IGDB](https://www.igdb.com/games/{slug})", inline=False)

            cover_url = game.get("cover_url") or (game.get("cover", {}) or {}).get("url")
            if cover_url:
                if cover_url.startswith("//"):
                    cover_url = f"https:{cover_url}"
                cover_url = cover_url.replace("t_thumb", "t_cover_big")
                embed.set_image(url=cover_url)

            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        except Exception as e:
            logger.error(f"Error in GameSelect callback: {e}")
            try:
                await interaction.response.send_message("❌ Une erreur est survenue.", ephemeral=True)
            except Exception:
                pass


class WishlistListPanelView(View):
    """Panel view that shows a list of wishlist items paginated.

    The view shows `page_size` game buttons per page plus Prev/Next/Close controls.
    Clicking a game button opens the item's detailed ephemeral view.
    """

    def __init__(self, games: List[Dict[str, Any]], wishlist_manager, page_size: int = 10, owner_name: Optional[str] = None):
        super().__init__(timeout=PAGINATION_TIMEOUT)
        # Keep an original copy so we can re-sort on demand without losing the source order
        self.original_games = list(games)
        self.games = list(games)
        self.wishlist_manager = wishlist_manager
        self.page_size = max(1, int(page_size))
        self.current_page = 0
        # Optional owner display name used when showing someone else's wishlist
        self.owner_name = owner_name
        # compute max page index
        self.max_page = max(0, (len(self.games) - 1) // self.page_size)

        # Default sort direction: True == descending (most recent/unknown first) to match previous behaviour
        self.sort_descending = True

        # Initialize buttons for the first page and add navigational buttons (decorated methods exist below)
        self._build_page_buttons()

    def _release_ts(self, g: Dict[str, Any]) -> int:
        """Normalize release timestamp for sorting.

        Missing or invalid dates return a large sentinel so they sort consistently.
        """
        ts = g.get("first_release_date")
        try:
            return int(ts) if ts else 9999999999
        except Exception:
            return 9999999999

    def _sort_games(self) -> None:
        """Sort self.games from the original list according to current sort direction."""
        self.games = sorted(self.original_games, key=self._release_ts, reverse=bool(self.sort_descending))
        self.max_page = max(0, (len(self.games) - 1) // self.page_size)

    def _build_page_buttons(self):
        """(Re)build the GameSelectButtons for the current page and attach nav buttons."""
        # Remove all items and re-add page-specific buttons + nav controls
        self.clear_items()

        # Ensure games are sorted according to current toggle before building page
        self._sort_games()

        start = self.current_page * self.page_size
        end = start + self.page_size

        # Build a single select menu for the page to reduce UI clutter
        options = []
        for i, game in enumerate(self.games[start:end], start=start):
            name = game.get("name", "Jeu")
            label = f"{i+1}. {name}"
            if len(label) > 100:
                label = label[:97] + "..."
            # value will be the absolute index so callback can open the right game
            options.append(discord.SelectOption(label=label, value=str(i)))

        # Create and add the Select component (single-select)
        if options:
            select = GameSelect(options=options, parent_view=self)
            self.add_item(select)

        # Add the sort toggle button and navigational buttons (these are bound to instance attributes by decorator)
        # Make sure the sort button label matches current state
        try:
            self.sort_button.label = "Trier: Décroissant" if self.sort_descending else "Trier: Croissant"
        except Exception:
            pass

        try:
            # decorated buttons are attributes on the instance
            self.add_item(self.sort_button)
            self.add_item(self.previous_button)
            self.add_item(self.next_button)
            self.add_item(self.close_button)
        except Exception:
            # If for some reason decorated buttons aren't present, it's non-fatal
            pass

        # Update nav button disabled state
        if hasattr(self, 'previous_button'):
            self.previous_button.disabled = self.current_page == 0
        if hasattr(self, 'next_button'):
            self.next_button.disabled = self.current_page == self.max_page
    # end _build_page_buttons

    def build_page_embed(self, page: int) -> discord.Embed:
        """Return an embed representing the given page of games."""
        page = max(0, min(page, self.max_page))
        start = page * self.page_size
        end = start + self.page_size
        page_games = self.games[start:end]

        description_lines = []
        now_ts = int(datetime.now().timestamp())
        for idx, game in enumerate(page_games, start=start):
            name = game.get("name", "Titre inconnu")
            ts = game.get("first_release_date")
            if ts:
                try:
                    rel = "(à venir)" if int(ts) >= now_ts else "(déjà sorti)"
                    date_str = datetime.fromtimestamp(int(ts)).strftime("%d/%m/%Y")
                except Exception:
                    date_str = "Date inconnue"
                    rel = ""
            else:
                date_str = "Date inconnue"
                rel = ""

            slug = game.get("slug")
            if slug:
                link = f"https://www.igdb.com/games/{slug}"
                line = f"**{idx+1}. [{name}]({link})** — {date_str} {rel}"
            else:
                line = f"**{idx+1}. {name}** — {date_str} {rel}"

            description_lines.append(line)

        if self.owner_name:
            title = f"💝 Wishlist de {self.owner_name} (page {page+1}/{self.max_page+1})"
        else:
            title = f"💝 Votre Wishlist (page {page+1}/{self.max_page+1})"

        embed = discord.Embed(
            title=title,
            description="\n".join(description_lines) if description_lines else "(Aucun jeu sur cette page)",
            color=0xFF69B4,
        )

        if len(self.games) > self.page_size:
            embed.set_footer(text=f"Affichage {start+1}-{min(end, len(self.games))} sur {len(self.games)} jeux")

        return embed

    @discord.ui.button(label="◀️ Précédent", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction: Interaction, button: Button):
        if self.current_page > 0:
            self.current_page -= 1
            self._build_page_buttons()
            embed = self.build_page_embed(self.current_page)
            await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Suivant ▶️", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: Interaction, button: Button):
        if self.current_page < self.max_page:
            self.current_page += 1
            self._build_page_buttons()
            embed = self.build_page_embed(self.current_page)
            await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Trier: Décroissant", style=discord.ButtonStyle.primary)
    async def sort_button(self, interaction: Interaction, button: Button):
        """Toggle sort order between descending and ascending by release date."""
        # Only toggle and rebuild; preserve current page index where reasonable
        self.sort_descending = not bool(self.sort_descending)
        # Update label to reflect new state
        button.label = "Trier: Décroissant" if self.sort_descending else "Trier: Croissant"
        # Rebuild page buttons which will re-sort and refresh navigation
        self._build_page_buttons()
        embed = self.build_page_embed(self.current_page)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="🗑️ Fermer", style=discord.ButtonStyle.danger)
    async def close_button(self, interaction: Interaction, button: Button):
        await interaction.response.edit_message(content="Panel fermé.", embed=None, view=None)
