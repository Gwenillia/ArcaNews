import discord
from discord import Interaction
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
