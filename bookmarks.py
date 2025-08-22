import os
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import aiosqlite
import discord
from discord import Interaction, app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)

DB_PATH = "data/news.db"


class BookmarkManager(commands.Cog):
    """Cog to manage per-user bookmarks for posted news entries."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await self._init_db()
        logger.info("🔖 Gestionnaire de favoris chargé")

    async def _init_db(self) -> None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        async with aiosqlite.connect(DB_PATH) as db:
            # news_entries: canonical storage for posted news
            await db.execute("""
                CREATE TABLE IF NOT EXISTS news_entries (
                    entry_id TEXT PRIMARY KEY,
                    source TEXT,
                    source_entry_id TEXT,
                    url TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    summary TEXT,
                    content TEXT,
                    image_url TEXT,
                    published_at INTEGER,
                    posted_at TEXT,
                    extra_json TEXT
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_news_published_at ON news_entries (published_at)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_news_source ON news_entries (source)")

            # bookmarks: per-user references to news_entries
            await db.execute("""
                CREATE TABLE IF NOT EXISTS bookmarks (
                    user_id INTEGER,
                    entry_id TEXT,
                    added_at TEXT,
                    note TEXT,
                    PRIMARY KEY (user_id, entry_id),
                    FOREIGN KEY (entry_id) REFERENCES news_entries(entry_id) ON DELETE RESTRICT
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_bookmarks_user ON bookmarks (user_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_bookmarks_entry ON bookmarks (entry_id)")
            await db.commit()

    async def add_bookmark(
        self,
        user_id: int,
        entry_id: str,
        title: str,
        url: str,
        feed_title: Optional[str] = None,
        image_url: Optional[str] = None,
    ) -> bool:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Ensure a minimal news_entries row exists for this entry_id so we can later join and display full content
                try:
                    await db.execute(
                        "INSERT OR IGNORE INTO news_entries (entry_id, url, title, image_url, summary, posted_at, extra_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            str(entry_id),
                            url or '',
                            title or '',
                            image_url,
                            None,
                            datetime.now().isoformat(),
                            None,
                        ),
                    )
                except Exception:
                    logger.debug("Could not ensure news_entries row for bookmark; proceeding")

                await db.execute(
                    "INSERT OR REPLACE INTO bookmarks (user_id, entry_id, added_at, note) VALUES (?, ?, ?, ?)",
                    (user_id, str(entry_id), datetime.now().isoformat(), None),
                )
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error adding bookmark: {e}")
            return False

    async def remove_bookmark(self, user_id: int, entry_id: str) -> bool:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM bookmarks WHERE user_id = ? AND entry_id = ?", (user_id, str(entry_id)))
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error removing bookmark: {e}")
            return False

    async def is_bookmarked(self, user_id: int, entry_id: str) -> bool:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT 1 FROM bookmarks WHERE user_id = ? AND entry_id = ?", (user_id, str(entry_id))) as cursor:
                    return bool(await cursor.fetchone())
        except Exception as e:
            logger.error(f"Error checking bookmark: {e}")
            return False

    async def get_user_bookmarks(self, user_id: int) -> List[Dict[str, Any]]:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Join bookmarks with canonical news_entries for display
                async with db.execute(
                    """
                    SELECT b.entry_id, n.title, n.url, n.summary, n.content, n.image_url, b.added_at, n.published_at
                    FROM bookmarks b
                    LEFT JOIN news_entries n ON b.entry_id = n.entry_id
                    WHERE b.user_id = ?
                    ORDER BY b.added_at DESC
                    """,
                    (user_id,),
                ) as cursor:
                    rows = await cursor.fetchall()
                    cols = [c[0] for c in cursor.description]
                    return [dict(zip(cols, r)) for r in rows]
        except Exception as e:
            logger.error(f"Error getting user bookmarks: {e}")
            return []

    async def handle_bookmarks(self, interaction: Interaction):
        """Handler called by the top-level /news favoris wrapper (always ephemeral)."""
        await interaction.response.defer(ephemeral=True)
        try:
            user_id = interaction.user.id
            bms = await self.get_user_bookmarks(user_id)
            if not bms:
                embed = discord.Embed(title="🔖 Vos favoris", description="Vous n'avez aucun favoris.", color=0x2F3136)
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            view = BookmarksListPanelView(bms, self)
            page_embed = view.build_page_embed(0)
            await interaction.followup.send(embed=page_embed, view=view, ephemeral=True)
        except Exception as e:
            logger.error(f"Error in /news favoris: {e}")
            try:
                await interaction.followup.send("❌ Une erreur est survenue.", ephemeral=True)
            except Exception:
                pass

    # --- UI helpers -------------------------------------------------
    def make_entry_view(self, entry: Dict[str, Any]) -> Optional[discord.ui.View]:
        """Return a per-entry view (opens an ephemeral per-user manager on click)."""
        try:
            return EntryBookmarkView(entry, self)
        except Exception as e:
            logger.error(f"Error building bookmark view: {e}")
            return None

    @staticmethod
    def canonical_entry_id_from_entry(entry: Dict[str, Any]) -> str:
        """Return the canonical entry_id used by the poster/db."""
        if not entry:
            return ""
        if entry.get('entry_id'):
            return str(entry.get('entry_id'))
        if entry.get('id') is not None:
            return f"miniflux:{entry.get('id')}"
        if entry.get('url'):
            return f"url:{entry.get('url')}"
        return ""


# ──────────────────────────────────────────────────────────────────────────────
# Buttons / Views
# ──────────────────────────────────────────────────────────────────────────────

class EntryBookmarkButton(discord.ui.Button):
    """Button placed on embeds the bot sends alongside news entries.
    Clicking opens an ephemeral, per-user panel that shows either Add or Remove — never both.
    """
    def __init__(self, entry: Dict[str, Any], manager: BookmarkManager):
        super().__init__(label="🔖 Favoris", style=discord.ButtonStyle.secondary)
        self.entry = entry
        self.manager = manager

    async def callback(self, interaction: Interaction):
        user_id = interaction.user.id
        entry_id = self.manager.canonical_entry_id_from_entry(self.entry)
        title = self.entry.get('title') or self.entry.get('url') or 'Article'
        try:
            is_bm = await self.manager.is_bookmarked(user_id, entry_id)
            pv = PersonalBookmarkView(self.entry, self.manager, user_id, is_bm)
            msg = f"🔖 **{title}** est déjà dans vos favoris." if is_bm else f"🔖 **{title}** n'est pas dans vos favoris."
            await interaction.response.send_message(msg, view=pv, ephemeral=True)
        except Exception as e:
            logger.error(f"Error in EntryBookmarkButton callback: {e}")
            await interaction.response.send_message("❌ Erreur lors de l'opération de favoris.", ephemeral=True)


class PersonalBookmarkView(discord.ui.View):
    """Ephemeral per-user panel that shows only the relevant action for that user."""
    def __init__(self, entry: Dict[str, Any], manager: BookmarkManager, user_id: int, is_bookmarked: bool, timeout: Optional[float] = 60):
        super().__init__(timeout=timeout)
        self.entry = entry
        self.manager = manager
        self.user_id = user_id
        self.is_bookmarked = is_bookmarked

        if is_bookmarked:
            self.add_item(RemoveBookmarkButton(entry, manager, user_id))
        else:
            self.add_item(AddBookmarkButton(entry, manager, user_id))


class AddBookmarkButton(discord.ui.Button):
    def __init__(self, entry: Dict[str, Any], manager: BookmarkManager, user_id: int):
        super().__init__(label="➕ Ajouter", style=discord.ButtonStyle.success)
        self.entry = entry
        self.manager = manager
        self.user_id = user_id

    async def callback(self, interaction: Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Vous ne pouvez pas utiliser ce bouton.", ephemeral=True)
            return

        entry_id = self.manager.canonical_entry_id_from_entry(self.entry)
        title = self.entry.get('title') or ''
        url = self.entry.get('url') or ''

        # safer image extraction
        image_url = None
        if isinstance(self.entry.get('image'), dict):
            image_url = self.entry['image'].get('url')
        elif isinstance(self.entry.get('enclosures'), list) and self.entry['enclosures']:
            first = self.entry['enclosures'][0]
            if isinstance(first, dict):
                image_url = first.get('url')

        try:
            ok = await self.manager.add_bookmark(
                self.user_id, entry_id, title, url,
                (self.entry.get('feed') or {}).get('title') if isinstance(self.entry.get('feed'), dict) else None,
                image_url
            )
            if ok:
                await interaction.response.edit_message(content=f"✅ **{title}** ajouté à vos favoris.", view=None)
            else:
                await interaction.response.send_message("❌ Impossible d'ajouter le favori.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error adding bookmark via button: {e}")
            await interaction.response.send_message("❌ Erreur interne.", ephemeral=True)


class RemoveBookmarkButton(discord.ui.Button):
    def __init__(self, entry: Dict[str, Any], manager: BookmarkManager, user_id: int):
        super().__init__(label="➖ Retirer", style=discord.ButtonStyle.danger)
        self.entry = entry
        self.manager = manager
        self.user_id = user_id

    async def callback(self, interaction: Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Vous ne pouvez pas utiliser ce bouton.", ephemeral=True)
            return
        entry_id = self.manager.canonical_entry_id_from_entry(self.entry)
        title = self.entry.get('title') or ''
        try:
            ok = await self.manager.remove_bookmark(self.user_id, entry_id)
            if ok:
                await interaction.response.edit_message(content=f"✅ **{title}** retiré de vos favoris.", view=None)
            else:
                await interaction.response.send_message("❌ Impossible de retirer le favori.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error removing bookmark via button: {e}")
            await interaction.response.send_message("❌ Erreur interne.", ephemeral=True)


class EntryBookmarkView(discord.ui.View):
    """Attached to any embed the bot posts itself. Clicking opens the per-user manager."""
    def __init__(self, entry: Dict[str, Any], manager: BookmarkManager):
        super().__init__(timeout=None)
        self.entry = entry
        self.manager = manager
        self.add_item(EntryBookmarkButton(entry, manager))


# ── Public view for channel posts ─────────────────────────────────────────────
class PublicManageBookmarkButton(discord.ui.Button):
    """Single public button that adapts per-user."""

    def __init__(self, entry: Dict[str, Any], manager: BookmarkManager):
        super().__init__(label="🔖 Favoris", style=discord.ButtonStyle.secondary)
        self.entry = entry
        self.manager = manager

    async def callback(self, interaction: Interaction):
        user_id = interaction.user.id
        entry_id = self.manager.canonical_entry_id_from_entry(self.entry)
        title = self.entry.get('title') or self.entry.get('url') or 'Article'

        try:
            is_bm = await self.manager.is_bookmarked(user_id, entry_id)
            # Build per-user view with either Ajouter or Retirer
            pv = PersonalBookmarkView(self.entry, self.manager, user_id, is_bm)

            if is_bm:
                msg = f"🔖 **{title}** est déjà dans vos favoris."
            else:
                msg = f"🔖 **{title}** n'est pas encore dans vos favoris."

            await interaction.response.send_message(msg, view=pv, ephemeral=True)

        except Exception as e:
            logger.error(f"Error in PublicManageBookmarkButton callback: {e}")
            await interaction.response.send_message("❌ Erreur interne.", ephemeral=True)


class PublicBookmarkView(discord.ui.View):
    """View for public channel messages. Always shows the neutral 'Favoris' button."""
    def __init__(self, entry: Dict[str, Any], manager: BookmarkManager, timeout: Optional[float] = None):
        super().__init__(timeout=timeout)
        self.entry = entry
        self.manager = manager
        self.add_item(PublicManageBookmarkButton(entry, manager))

# ── Bookmarks list panel (ephemeral) ─────────────────────────────────────────
class BookmarksListPanelView(discord.ui.View):
    """Panel to list a user's bookmarks paginated, allow selecting one to display full news."""
    def __init__(self, bookmarks: List[Dict[str, Any]], manager: "BookmarkManager", page_size: int = 6):
        super().__init__(timeout=300)
        self.bookmarks = bookmarks
        self.page_size = max(1, page_size)
        self.current_page = 0
        self.max_page = max(0, (len(self.bookmarks) - 1) // self.page_size)
        self.manager = manager
        self._build_page_components()

    def _build_page_components(self):
        """Rebuild select menu and nav buttons for current page"""
        self.clear_items()
        start = self.current_page * self.page_size
        page_items = self.bookmarks[start:start + self.page_size]

        options = []
        for idx, bm in enumerate(page_items, start=start):
            title = bm.get('title') or bm.get('url') or 'Article'
            label = f"{idx+1}. {title}"
            if len(label) > 100:
                label = label[:97] + '...'
            options.append(discord.SelectOption(label=label, value=str(idx)))

        if options:
            select = discord.ui.Select(placeholder="Choisir un favori...", min_values=1, max_values=1, options=options)

            async def _on_select(interaction: Interaction):
                try:
                    sel = int(select.values[0])
                    bm = self.bookmarks[sel]
                    embed = discord.Embed(title=bm.get('title') or 'Article', color=0x2F3136)
                    if bm.get('summary'):
                        embed.description = bm.get('summary')
                    if bm.get('image_url'):
                        embed.set_image(url=bm.get('image_url'))
                    if bm.get('url'):
                        embed.add_field(name='🔗 Lien', value=bm.get('url'), inline=False)
                    if bm.get('content'):
                        content = bm.get('content')
                        if len(content) > 4000:
                            content = content[:3997] + '...'
                        embed.add_field(name='📝 Contenu', value=content, inline=False)

                    user_id = interaction.user.id
                    personal_view = PersonalBookmarkView(bm, self.manager, user_id, True)  # selected from user's own list → True

                    if interaction.guild_id and interaction.channel:
                        # Post publicly with a single "manage" button (per-user ephemeral chooser)
                        public_view = PublicBookmarkView(bm, self.manager, timeout=None)
                        await interaction.channel.send(embed=embed, view=public_view)
                        await interaction.response.send_message("✅ Article publié.", ephemeral=True)
                    else:
                        await interaction.response.send_message(embed=embed, view=personal_view, ephemeral=True)
                except Exception as e:
                    logger.error(f"Error showing bookmark detail: {e}")
                    try:
                        await interaction.response.send_message("❌ Impossible d'afficher le bookmark.", ephemeral=True)
                    except Exception:
                        pass

            select.callback = _on_select
            self.add_item(select)

        # navigation buttons
        self.add_item(discord.ui.Button(label='◀ Précédent', style=discord.ButtonStyle.secondary, custom_id='prev'))
        self.add_item(discord.ui.Button(label='Suivant ▶', style=discord.ButtonStyle.secondary, custom_id='next'))
        self.add_item(discord.ui.Button(label='❌ Fermer', style=discord.ButtonStyle.danger, custom_id='close'))

        # bind callbacks for nav buttons
        for item in list(self.children):
            if isinstance(item, discord.ui.Button) and item.custom_id == 'prev':
                async def _prev(interaction: Interaction, button=item):
                    if self.current_page > 0:
                        self.current_page -= 1
                        self._build_page_components()
                        await interaction.response.edit_message(embed=self.build_page_embed(self.current_page), view=self)
                item.callback = _prev

            if isinstance(item, discord.ui.Button) and item.custom_id == 'next':
                async def _next(interaction: Interaction, button=item):
                    if self.current_page < self.max_page:
                        self.current_page += 1
                        self._build_page_components()
                        await interaction.response.edit_message(embed=self.build_page_embed(self.current_page), view=self)
                item.callback = _next

            if isinstance(item, discord.ui.Button) and item.custom_id == 'close':
                async def _close(interaction: Interaction, button=item):
                    await interaction.response.edit_message(content='Panel fermé.', embed=None, view=None)
                item.callback = _close

    def build_page_embed(self, page: int) -> discord.Embed:
        page = max(0, min(page, self.max_page))
        start = page * self.page_size
        page_items = self.bookmarks[start:start + self.page_size]
        lines = []
        for idx, bm in enumerate(page_items, start=start):
            title = bm.get('title') or bm.get('url') or 'Article'
            url = bm.get('url') or ''
            added = bm.get('added_at') or ''
            lines.append(f"**{idx+1}.** [{title}]({url}) — ajouté: {added}")

        embed = discord.Embed(title='🔖 Vos favoris', description='\n'.join(lines), color=0x2F3136)
        embed.set_footer(text=f'Page {page+1}/{self.max_page+1} — {len(self.bookmarks)} bookmarks')
        return embed


async def setup(bot: commands.Bot):
    await bot.add_cog(BookmarkManager(bot))

    # Register the /news group with favoris subcommand
    try:
        bot.tree.remove_command("news")
    except Exception:
        pass

    async def _news_bookmarks(interaction: Interaction):
        cog: Optional["BookmarkManager"] = interaction.client.get_cog("BookmarkManager")
        if cog is None:
            await interaction.response.send_message("❌ Bookmark cog non chargé.", ephemeral=True)
            return
        await cog.handle_bookmarks(interaction)

    news_group = app_commands.Group(name="news", description="Commandes liées aux news")
    news_group.add_command(app_commands.Command(name="favoris", description="🔖 Affiche vos favoris (news)", callback=_news_bookmarks))
    bot.tree.add_command(news_group, override=True)