from email.mime import base
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

# Read DEV_GUILD_ID
_DEV_GUILD = os.getenv("DEV_GUILD_ID")
DEV_GUILD_ID: Optional[int] = int(_DEV_GUILD) if _DEV_GUILD else None

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

# ── Public subcommands (decorators OK) ─────────────────────────────────────────

@app_commands.describe(public="True = votre wishlist est publique, False = privée")
async def _wishlist_visibility(interaction: Interaction, public: bool):
    """Définit la visibilité de TA wishlist (publique/privée)."""
    cog: Optional["WishlistManager"] = interaction.client.get_cog("WishlistManager")
    if cog is None:
        await interaction.response.send_message("❌ Wishlist cog non chargé.", ephemeral=True)
        return

    ok = await cog.set_user_visibility(interaction.user.id, public)
    if not ok:
        await interaction.response.send_message("❌ Impossible de mettre à jour la visibilité.", ephemeral=True)
        return

    state = "publique 👀" if public else "privée 🔒"
    await interaction.response.send_message(f"✅ Ta wishlist est maintenant **{state}**.", ephemeral=True)

@app_commands.describe(member="Membre dont vous voulez voir la wishlist (optionnel)")
async def _wishlist_show(interaction: Interaction, member: Optional[discord.Member] = None):
    cog: Optional["WishlistManager"] = interaction.client.get_cog("WishlistManager")
    if cog is None:
        await interaction.response.send_message("❌ Wishlist cog non chargé.", ephemeral=True)
        return
    await cog.handle_show(interaction, member)

async def _wishlist_clear(interaction: Interaction):
    cog: Optional["WishlistManager"] = interaction.client.get_cog("WishlistManager")
    if cog is None:
        await interaction.response.send_message("❌ Wishlist cog non chargé.", ephemeral=True)
        return
    await cog.handle_clear(interaction)

@app_commands.describe(mois="Mois (1-12)", annee="Année")
async def _wishlist_calendar(interaction: Interaction, mois: int, annee: int):
    cog: Optional["WishlistManager"] = interaction.client.get_cog("WishlistManager")
    if cog is None:
        await interaction.response.send_message("❌ Wishlist cog non chargé.", ephemeral=True)
        return
    await cog.handle_calendar(interaction, mois, annee)

# ── Dev-only admin subcommands (PLAIN FUNCTIONS; added in setup()) ────────────

@owner_only_check()
async def _wishlist_refresh(interaction: Interaction):
    dev = os.getenv("DEV_GUILD_ID")
    dev_id = int(dev) if dev else None
    if dev_id and interaction.guild_id != dev_id:
        await interaction.response.send_message("❌ Cette commande est réservée au serveur de développement.", ephemeral=True)
        return

    cog: Optional["WishlistManager"] = interaction.client.get_cog("WishlistManager")
    if cog is None:
        await interaction.response.send_message("❌ Wishlist cog non chargé.", ephemeral=True)
        return

    await interaction.response.defer()
    try:
        result = await cog.refresh_all_wishlist_dates()
        updated = result.get("updated", 0)
        unchanged = result.get("unchanged", 0)
        missing = result.get("missing", 0)
        failed = result.get("failed", 0)
        await interaction.followup.send(
            f"✅ Rafraîchissement terminé — mises à jour: {updated}, inchangés: {unchanged}, manquants: {missing}, erreurs: {failed}",
            ephemeral=True,
        )
    except Exception as e:
        logger.error(f"Error refreshing wishlist dates: {e}")
        await interaction.followup.send("❌ Erreur lors du rafraîchissement.", ephemeral=True)

@owner_only_check()
async def _wishlist_refresh_status(interaction: Interaction):
    dev = os.getenv("DEV_GUILD_ID")
    dev_id = int(dev) if dev else None
    if dev_id and interaction.guild_id != dev_id:
        await interaction.response.send_message("❌ Cette commande est réservée au serveur de développement.", ephemeral=True)
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Permission refusée: administrateur requis.", ephemeral=True)
        return

    cog: Optional["WishlistManager"] = interaction.client.get_cog("WishlistManager")
    if cog is None:
        await interaction.response.send_message("❌ Wishlist cog non chargé.", ephemeral=True)
        return

    last_time = getattr(cog, "_last_refresh_time", None)
    last_summary = getattr(cog, "_last_refresh_summary", None)
    task_running = bool(getattr(cog, "_refresh_task", None) and not getattr(cog, "_refresh_task").done())

    if last_time and last_summary:
        msg = (
            f"Dernier rafraîchissement: {last_time}\n"
            f"Résumé: mises à jour={last_summary.get('updated',0)}, "
            f"inchangés={last_summary.get('unchanged',0)}, "
            f"manquants={last_summary.get('missing',0)}, erreurs={last_summary.get('failed',0)}\n"
            f"Tâche background active: {'oui' if task_running else 'non'}"
        )
    else:
        msg = f"Aucun rafraîchissement enregistré yet. Tâche background active: {'oui' if task_running else 'non'}"
    await interaction.response.send_message(msg, ephemeral=True)

class WishlistManager(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._refresh_task = None
        self._last_refresh_time = None
        self._last_refresh_summary = None

    async def cog_load(self):
        await self._init_db()
        logger.info("💝 Wishlist Manager loaded")
        # Start background refresh task (once a day).
        try:
            import asyncio
            self._refresh_task = asyncio.create_task(self._daily_refresh_loop())
        except Exception:
            logger.exception("Failed to start background wishlist refresh task")

    async def cog_unload(self):
        # Cancel background task if running
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()

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
            # Per-user settings (visibility, future options)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id INTEGER PRIMARY KEY,
                    public INTEGER DEFAULT 0
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
                # Normalize cover_url: prefer game['cover_url'] then nested cover.url
                cover_url = game.get("cover_url") or (game.get("cover") or {}).get("url")

                # Normalize first_release_date: prefer top-level key, otherwise look into release_dates
                first_release_date = game.get("first_release_date")
                if not first_release_date:
                    rds = game.get("release_dates") or []
                    try:
                        dates = [int(rd.get("date")) for rd in rds if rd and rd.get("date")]
                        if dates:
                            first_release_date = min(dates)
                    except Exception:
                        first_release_date = None

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
                        first_release_date,
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

    async def get_user_visibility(self, user_id: int) -> bool:
        """Return True if the user's wishlist is public, False otherwise (default False)."""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT public FROM user_settings WHERE user_id = ?", (user_id,)) as cursor:
                    row = await cursor.fetchone()
                    if not row:
                        return False
                    return bool(row[0])
        except Exception:
            return False

    async def set_user_visibility(self, user_id: int, public: bool) -> bool:
        """Set the user's wishlist visibility. public=True makes it visible to others."""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Upsert semantics
                await db.execute(
                    "INSERT INTO user_settings (user_id, public) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET public=excluded.public",
                    (user_id, 1 if public else 0),
                )
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error setting user visibility: {e}")
            return False

    async def refresh_all_wishlist_dates(self) -> Dict[str, int]:
        """Refresh first_release_date for all wishlist entries using IGDB where possible.

        Returns a summary dict with counts: updated, unchanged, missing, failed
        """
        updated = unchanged = missing = failed = 0

        # Collect all unique game ids from DB
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT DISTINCT game_id, slug FROM wishlists") as cursor:
                    rows = await cursor.fetchall()

            # Build a map of game_id -> slug (slug may be None)
            games = [{"game_id": r[0], "slug": r[1]} for r in rows]

            # Query IGDB in batches using slug when possible, else by id
            ids = [str(g["game_id"]) for g in games if g.get("game_id")]

            igdb = self.bot.get_cog('IGDB')

            async def _update_game(game_id: int, new_ts: Optional[int]):
                nonlocal updated, unchanged, failed
                try:
                    if new_ts is None:
                        unchanged += 1
                        return

                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("UPDATE wishlists SET first_release_date = ? WHERE game_id = ?", (new_ts, game_id))
                        await db.commit()
                        updated += 1
                except Exception:
                    failed += 1

            if igdb:
                batch_size = 200
                for i in range(0, len(ids), batch_size):
                    batch = ids[i:i+batch_size]
                    if not batch:
                        continue
                    query = f'fields id, first_release_date; where id = ({",".join(batch)}); limit {len(batch)};'
                    try:
                        results = await igdb._fetch_games_from_api(query)
                    except Exception:
                        failed += len(batch)
                        continue

                    byid = {str(r.get('id')): r for r in results}
                    for gid in batch:
                        r = byid.get(gid)
                        if r and r.get('first_release_date'):
                            await _update_game(int(gid), int(r.get('first_release_date')))
                        else:
                            missing += 1
            else:
                return {"updated": 0, "unchanged": 0, "missing": len(games), "failed": 0}

            summary = {"updated": updated, "unchanged": unchanged, "missing": missing, "failed": failed}
            try:
                self._last_refresh_summary = summary
                self._last_refresh_time = datetime.now().isoformat()
            except Exception:
                pass

            return summary

        except Exception as e:
            logger.error(f"Error during bulk wishlist refresh: {e}")
            summary = {"updated": updated, "unchanged": unchanged, "missing": missing, "failed": failed}
            try:
                self._last_refresh_summary = summary
                self._last_refresh_time = datetime.now().isoformat()
            except Exception:
                pass
            return summary

    async def _daily_refresh_loop(self):
        """Background task that refreshes wishlist dates once a day."""
        import asyncio
        try:
            while True:
                try:
                    logger.info("🔁 Daily wishlist refresh starting")
                    await self.refresh_all_wishlist_dates()
                    logger.info("🔁 Daily wishlist refresh completed")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Error during daily wishlist refresh loop")
                # Sleep for 24 hours
                await asyncio.sleep(60 * 60 * 24)
        except asyncio.CancelledError:
            logger.info("🔁 Daily wishlist refresh task cancelled")

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

        for (row, col), cell in table.get_celld().items():
            cell.set_height(0.15)
            cell.set_width(0.14)

        month_name = calendar.month_name[month]
        ax.set_title(f"{month_name} {year}", fontsize=16, pad=20)

        output_path = "/tmp/calendar.png"
        plt.savefig(output_path, bbox_inches="tight")
        plt.close(fig)
        return output_path

    async def handle_show(self, interaction: Interaction, member: Optional[discord.Member] = None):
        try:
            target = member or interaction.user

            # Check visibility BEFORE deferring so we can deny privately without creating a public "thinking" message
            real_visible = await self.get_user_visibility(target.id)
            if target.id != interaction.user.id and not real_visible:
                # Another user trying to see a private wishlist -> deny (ephemeral)
                await interaction.response.send_message("❌ Cette wishlist est privée.", ephemeral=True)
                return

            # Determine ephemeral behavior for the owner BEFORE deferring
            ephemeral_for_owner = (target.id == interaction.user.id and not real_visible)

            # Defer with the appropriate visibility (ephemeral when owner views their private wishlist)
            await interaction.response.defer(ephemeral=ephemeral_for_owner)

            user_wishlist = await self.get_user_wishlist(target.id)
            if not user_wishlist:
                title = f"💝 Wishlist de {target.display_name}" if target.id != interaction.user.id else "💝 Votre Wishlist"
                desc = "Cette wishlist est vide !\n\nUtilisez `/recherche` pour trouver des jeux à ajouter." if target.id == interaction.user.id else "Cette wishlist est vide."
                embed = discord.Embed(
                    title=title,
                    description=desc,
                    color=0xFF69B4
                )
                # If the wishlist belongs to a user who has visibility off but is viewing their own wishlist, keep the response ephemeral
                ephemeral_for_owner = (target.id == interaction.user.id and not real_visible)
                await interaction.followup.send(embed=embed, ephemeral=ephemeral_for_owner)
                return

            def _release_ts(g):
                ts = g.get("first_release_date") or g.get("first_release_date")
                try:
                    return int(ts) if ts else 9999999999
                except Exception:
                    return 9999999999

            sorted_wishlist = sorted(user_wishlist, key=_release_ts, reverse=True)

            display_games = sorted_wishlist[:10]
            description_lines = []
            now_ts = int(datetime.now().timestamp())
            for i, game in enumerate(display_games):
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
                    line = f"**{i+1}. [{name}]({link})** — {date_str} {rel}"
                else:
                    line = f"**{i+1}. {name}** — {date_str} {rel}"

                description_lines.append(line)

            title = f"💝 Wishlist de {target.display_name} (10 premiers)" if target.id != interaction.user.id else "💝 Votre Wishlist (10 premiers)"
            embed = discord.Embed(
                title=title,
                description="\n".join(description_lines),
                color=0xFF69B4
            )

            if len(sorted_wishlist) > 10:
                embed.set_footer(text=f"Affichage de 10 sur {len(sorted_wishlist)} jeux • Utilisez la recherche pour voir plus")

            from ui_components import WishlistListPanelView
            owner_name = target.display_name if target.id != interaction.user.id else None
            view = WishlistListPanelView(sorted_wishlist, self, page_size=10, owner_name=owner_name)
            page_embed = view.build_page_embed(0)
            # Send the paginated wishlist. followup will respect the ephemeral flag matching the initial defer.
            await interaction.followup.send(embed=page_embed, view=view, ephemeral=ephemeral_for_owner)

        except Exception as e:
            logger.error(f"Error displaying wishlist: {e}")
            await interaction.followup.send("❌ Une erreur s'est produite lors de l'affichage de votre wishlist.")

    async def handle_clear(self, interaction: Interaction):
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

    async def handle_update(self, interaction: Interaction, game: str, date: str):
        await interaction.response.defer(ephemeral=True)

        try:
            user_wishlist = await self.get_user_wishlist(interaction.user.id)

            if not user_wishlist:
                await interaction.followup.send("💝 Votre wishlist est vide.", ephemeral=True)
                return

            def matches_query(entry: Dict[str, Any], q: str) -> bool:
                if not q:
                    return False
                try:
                    if int(q) == int(entry.get("game_id") or entry.get("id") or 0):
                        return True
                except Exception:
                    pass

                slug = entry.get("slug") or ""
                if q.lower() == str(slug).lower():
                    return True

                name = entry.get("name") or ""
                if q.lower() in str(name).lower():
                    return True

                return False

            matches = [g for g in user_wishlist if matches_query(g, game)]

            if not matches:
                await interaction.followup.send("❌ Aucun jeu dans votre wishlist ne correspond à cette requête.", ephemeral=True)
                return

            def parse_date_to_ts(s: str) -> Optional[int]:
                s = str(s).strip()
                try:
                    return int(s)
                except Exception:
                    pass

                from datetime import datetime as _dt
                for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y"):
                    try:
                        dt = _dt.strptime(s, fmt)
                        return int(dt.timestamp())
                    except Exception:
                        continue

                try:
                    dt = _dt.fromisoformat(s)
                    return int(dt.timestamp())
                except Exception:
                    return None

            ts = parse_date_to_ts(date)
            if ts is None:
                await interaction.followup.send("❌ Format de date invalide. Utilisez YYYY-MM-DD ou un timestamp Unix.", ephemeral=True)
                return

            if len(matches) > 1:
                class _SelectUpdate(discord.ui.Select):
                    def __init__(self, options, parent: "WishlistManager"):
                        super().__init__(placeholder="Sélectionnez le jeu à mettre à jour...", min_values=1, max_values=1, options=options)
                        self.parent = parent

                    async def callback(self, select_interaction: Interaction):
                        selected_index = int(self.values[0])
                        entry = matches[selected_index]
                        await _do_update(entry)
                        try:
                            await select_interaction.response.edit_message(content=f"✅ Date de sortie mise à jour pour **{entry.get('name')}**.", embed=None, view=None)
                        except Exception:
                            try:
                                await select_interaction.response.send_message(f"✅ Date de sortie mise à jour pour **{entry.get('name')}**.", ephemeral=True)
                            except Exception:
                                pass

                class _UpdateView(discord.ui.View):
                    def __init__(self, options):
                        super().__init__(timeout=60)
                        self.add_item(_SelectUpdate(options, self))

                options = []
                for idx, g in enumerate(matches):
                    label = f"{g.get('name', 'Titre')}"
                    if len(label) > 100:
                        label = label[:97] + "..."
                    options.append(discord.SelectOption(label=label, value=str(idx)))

                view = _UpdateView(options)
                await interaction.followup.send("Plusieurs jeux correspondent — choisissez celui à mettre à jour :", view=view, ephemeral=True)
                return

            entry = matches[0]

            async def _do_update(entry_to_update: Dict[str, Any]):
                game_id = entry_to_update.get("game_id") or entry_to_update.get("id")
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("UPDATE wishlists SET first_release_date = ? WHERE user_id = ? AND game_id = ?", (ts, interaction.user.id, game_id))
                    await db.commit()

            await _do_update(entry)
            await interaction.followup.send(f"✅ Date de sortie mise à jour pour **{entry.get('name')}** ({datetime.fromtimestamp(ts).strftime('%d/%m/%Y')}).", ephemeral=True)

        except Exception as e:
            logger.error(f"Error updating wishlist date: {e}")
            await interaction.followup.send("❌ Une erreur s'est produite lors de la mise à jour.", ephemeral=True)

    async def handle_calendar(self, interaction: Interaction, mois: int, annee: int):
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

    def __init__(self, wishlist_manager: "WishlistManager", user_id: int):
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

    # Always start from a clean slate for 'wishlist' to avoid stale signatures
    try:
        bot.tree.remove_command("wishlist")
    except Exception:
        pass

    # 1) Build the GLOBAL base group (public only)
    base = app_commands.Group(name="wishlist", description="💝 Commands liés à la wishlist")
    base.add_command(app_commands.Command(
    name="visibility", description="Définir la visibilité de votre wishlist (publique/privée)", callback=_wishlist_visibility
    ))
    base.add_command(app_commands.Command(
        name="show", description="💝 Affiche votre wishlist de jeux", callback=_wishlist_show
    ))
    base.add_command(app_commands.Command(
        name="clear", description="🗑️ Vide votre wishlist", callback=_wishlist_clear
    ))
    base.add_command(app_commands.Command(
        name="calendar", description="🗓️ Affiche les sorties de votre wishlist pour un mois donné", callback=_wishlist_calendar
    ))

    # Register the base group GLOBALLY
    bot.tree.add_command(base, override=True)

    # 2) If DEV_GUILD_ID is set, register a DEV-ONLY overlay with extra admin cmds
    if DEV_GUILD_ID:
        dev_group = app_commands.Group(name="wishlist", description="💝 Commands liés à la wishlist (dev)")
        # same public commands
        dev_group.add_command(app_commands.Command(
            name="visibility", description="Définir la visibilité de votre wishlist (publique/privée)", callback=_wishlist_visibility
        ))
        dev_group.add_command(app_commands.Command(
            name="show", description="💝 Affiche votre wishlist de jeux", callback=_wishlist_show
        ))
        dev_group.add_command(app_commands.Command(
            name="clear", description="🗑️ Vide votre wishlist", callback=_wishlist_clear
        ))
        dev_group.add_command(app_commands.Command(
            name="calendar", description="🗓️ Affiche les sorties de votre wishlist pour un mois donné", callback=_wishlist_calendar
        ))
        # + admin-only commands
        dev_group.add_command(app_commands.Command(
            name="refresh", description="🔁 Rafraîchir les dates de sortie de la wishlist (IGDB)", callback=_wishlist_refresh
        ))
        dev_group.add_command(app_commands.Command(
            name="refresh-status", description="ℹ️ Statut du rafraîchissement de la wishlist (admin only)", callback=_wishlist_refresh_status
        ))

        # Register the overlay ONLY in the dev guild
        bot.tree.add_command(dev_group, guild=discord.Object(id=DEV_GUILD_ID), override=True)