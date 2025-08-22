import os
import aiohttp
import asyncio
import logging
import datetime
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass
from contextlib import asynccontextmanager
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from discord import Embed, Color
from PIL import Image, UnidentifiedImageError
from io import BytesIO
from collections import Counter
import re

load_dotenv()

# Configuration
@dataclass
class Config:
    miniflux_api_token: str = os.getenv("MINIFLUX_API_TOKEN", "")
    miniflux_api_url: str = os.getenv("MINIFLUX_API_URL", "").rstrip("/")
    discord_channel_id: int = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
    
    # Timing configuration
    fetch_interval_with_entries: int = 30  # seconds
    fetch_interval_no_entries: int = 60    # seconds
    request_delay: int = 2                 # seconds between posts
    
    # Content configuration
    max_entries_per_batch: int = 5
    max_description_length: int = 400
    min_paragraph_length: int = 50
    image_resize_dimensions: Tuple[int, int] = (50, 50)
    request_timeout: int = 15

    def __post_init__(self):
        if not self.miniflux_api_token or not self.miniflux_api_url:
            raise ValueError("MINIFLUX_API_TOKEN and MINIFLUX_API_URL must be set")
        if not self.discord_channel_id:
            raise ValueError("DISCORD_CHANNEL_ID must be set")

logger = logging.getLogger(__name__)
config = Config()

# Custom Exceptions
class MinifluxError(Exception):
    """Base exception for Miniflux operations"""
    pass

class ScrapingError(Exception):
    """Exception for web scraping errors"""
    pass

# --- HTTP SESSION MANAGER ---------------------------------------------------

class HTTPSessionManager:
    """Manages a persistent HTTP session with proper cleanup"""
    
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
    
    @asynccontextmanager
    async def get_session(self):
        """Get or create HTTP session"""
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=20,  # Total connection pool size
                limit_per_host=5,  # Max connections per host
                ttl_dns_cache=300,  # DNS cache TTL
                use_dns_cache=True,
            )
            timeout = aiohttp.ClientTimeout(total=config.request_timeout)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers={'User-Agent': 'Discord-RSS-Bot/1.0'}
            )
        
        try:
            yield self._session
        except Exception:
            # Don't close session on error, just re-raise
            raise
    
    async def close(self):
        """Close the HTTP session"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

# Global session manager
session_manager = HTTPSessionManager()

# --- IMAGE COLOR UTILS -------------------------------------------------------

class ColorExtractor:
    """Utility class for extracting dominant colors from images"""
    
    @staticmethod
    def _is_valid_color(rgb: Tuple[int, int, int]) -> bool:
        """Check if color is not too dark or too light"""
        r, g, b = rgb
        # Exclude very dark colors (< 30 for all channels)
        if r < 30 and g < 30 and b < 30:
            return False
        # Exclude very light colors (> 230 for all channels)
        if r > 230 and g > 230 and b > 230:
            return False
        return True
    
    @staticmethod
    async def extract_dominant_color(image_url: str) -> Optional[Color]:
        """Extract dominant color from an image URL"""
        if not image_url:
            return None
            
        try:
            async with session_manager.get_session() as session:
                async with session.get(image_url) as resp:
                    if resp.status != 200:
                        logger.debug(f"Failed to fetch image: {image_url} (status: {resp.status})")
                        return None
                    
                    # Check content type
                    content_type = resp.headers.get('content-type', '')
                    if not content_type.startswith('image/'):
                        logger.debug(f"URL is not an image: {image_url}")
                        return None
                    
                    image_data = await resp.read()
                    
                    # Limit image size to prevent memory issues
                    if len(image_data) > 5 * 1024 * 1024:  # 5MB limit
                        logger.debug(f"Image too large: {len(image_data)} bytes")
                        return None

            # Process image
            with Image.open(BytesIO(image_data)) as image:
                # Convert to RGB and resize for faster processing
                rgb_image = image.convert("RGB")
                resized = rgb_image.resize(config.image_resize_dimensions, Image.Resampling.LANCZOS)
                
                # Get pixel data and filter out invalid colors
                pixels = list(resized.getdata())
                valid_pixels = [p for p in pixels if ColorExtractor._is_valid_color(p)]
                
                if not valid_pixels:
                    return None
                
                # Find most common color
                color_counts = Counter(valid_pixels)
                dominant_color = color_counts.most_common(1)[0][0]
                
                return Color.from_rgb(*dominant_color)

        except UnidentifiedImageError:
            logger.debug(f"Could not identify image format: {image_url}")
        except Exception as e:
            logger.warning(f"Failed to extract color from {image_url}: {e}")
        
        return None

# --- MINIFLUX API CLIENT -----------------------------------------------------

class MinifluxClient:
    """Client for interacting with Miniflux API"""
    
    def __init__(self):
        self._headers = {
            "X-Auth-Token": config.miniflux_api_token,
            "Content-Type": "application/json"
        }
    
    async def fetch_unread_entries(self) -> List[Dict[str, Any]]:
        """Fetch unread entries from Miniflux"""
        url = f"{config.miniflux_api_url}/v1/entries"
        params = {
            "status": "unread",
            "limit": config.max_entries_per_batch,
            "order": "published_at",
            "direction": "desc"
        }
        
        try:
            async with session_manager.get_session() as session:
                async with session.get(url, headers=self._headers, params=params) as resp:
                    if resp.status == 401:
                        raise MinifluxError("Invalid API token")
                    elif resp.status != 200:
                        error_text = await resp.text()
                        raise MinifluxError(f"API error {resp.status}: {error_text}")
                    
                    data = await resp.json()
                    entries = data.get("entries", [])
                    logger.info(f"Fetched {len(entries)} unread entries")
                    return entries
                    
        except aiohttp.ClientError as e:
            raise MinifluxError(f"Network error: {e}")
        except Exception as e:
            raise MinifluxError(f"Unexpected error: {e}")
    
    async def mark_as_read(self, entry_ids: List[int]) -> bool:
        """Mark entries as read"""
        if not entry_ids:
            return True
            
        url = f"{config.miniflux_api_url}/v1/entries"
        payload = {
            "entry_ids": entry_ids,
            "status": "read"
        }
        
        try:
            async with session_manager.get_session() as session:
                async with session.put(url, headers=self._headers, json=payload) as resp:
                    if resp.status == 204:
                        logger.debug(f"Marked {len(entry_ids)} entries as read")
                        return True
                    else:
                        logger.error(f"Failed to mark entries as read: {resp.status}")
                        return False
                        
        except Exception as e:
            logger.error(f"Error marking entries as read: {e}")
            return False

# --- WEB SCRAPER -------------------------------------------------------------

class WebScraper:
    """Web scraper for extracting article content and images"""
    
    # Common selectors for article content
    CONTENT_SELECTORS = [
        'article', '[role="main"]', '.post-content', '.entry-content',
        '.article-content', '.content', 'main'
    ]
    
    # Tags to remove during scraping
    UNWANTED_TAGS = ['script', 'style', 'nav', 'header', 'footer', 'aside', 'ad', '.advertisement']
    
    @staticmethod
    def _clean_url(url: str, base_url: str = "") -> str:
        """Clean and normalize URL"""
        if not url:
            return ""
        
        # Handle protocol-relative URLs
        if url.startswith('//'):
            return f'https:{url}'
        
        # Handle relative URLs
        if url.startswith('/') and base_url:
            return f"{base_url.rstrip('/')}{url}"
        
        return url
    
    @staticmethod
    def _extract_meta_image(soup: BeautifulSoup) -> Optional[str]:
        """Extract image from meta tags"""
        meta_properties = ['og:image', 'twitter:image', 'twitter:image:src']
        
        for prop in meta_properties:
            # Try property attribute
            tag = soup.find('meta', property=prop)
            if not tag:
                # Try name attribute
                tag = soup.find('meta', attrs={'name': prop})
            
            if tag and tag.get('content'):
                return tag.get('content')
        
        return None
    
    @staticmethod
    def _extract_content_text(soup: BeautifulSoup) -> str:
        """Extract clean text content from soup"""
        # Remove unwanted tags
        for selector in WebScraper.UNWANTED_TAGS:
            for tag in soup.select(selector):
                tag.decompose()
        
        # Try to find main content area
        content_area = None
        for selector in WebScraper.CONTENT_SELECTORS:
            content_area = soup.select_one(selector)
            if content_area:
                break
        
        # Fallback to body if no content area found
        if not content_area:
            content_area = soup.find('body') or soup
        
        # Extract paragraphs
        paragraphs = []
        for p in content_area.find_all('p'):
            text = p.get_text(strip=True)
            if len(text) >= config.min_paragraph_length:
                paragraphs.append(text)
            
            # Limit to first few paragraphs for performance
            if len(paragraphs) >= 3:
                break
        
        if not paragraphs:
            # Fallback: get any text
            text = content_area.get_text(separator=' ', strip=True)
            return text[:config.max_description_length] + "..." if len(text) > config.max_description_length else text
        
        combined_text = ' '.join(paragraphs)
        return combined_text[:config.max_description_length] + "..." if len(combined_text) > config.max_description_length else combined_text
    
    @staticmethod
    async def scrape_article(url: str) -> Tuple[Optional[str], Optional[str]]:
        """Scrape article content and image from URL"""
        if not url:
            return None, None
        
        try:
            async with session_manager.get_session() as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.debug(f"Failed to scrape {url}: status {resp.status}")
                        return None, None
                    
                    html = await resp.text()
            
            soup = BeautifulSoup(html, 'html.parser')
            
            # Extract content and image
            content = WebScraper._extract_content_text(soup)
            image_url = WebScraper._extract_meta_image(soup)
            
            # Clean image URL
            if image_url:
                from urllib.parse import urljoin
                image_url = urljoin(url, image_url)
            
            return content, image_url
            
        except Exception as e:
            logger.debug(f"Scraping failed for {url}: {e}")
            return None, None

# --- CONTENT PROCESSOR -------------------------------------------------------

class ContentProcessor:
    """Processes RSS entries into Discord embeds"""
    
    @staticmethod
    def clean_html_content(html_content: str) -> str:
        """Clean HTML content and extract plain text"""
        if not html_content:
            return ""
        
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # Remove unwanted tags
        for tag in soup(['script', 'style', 'nav', 'header', 'footer']):
            tag.decompose()
        
        # Get clean text
        text = soup.get_text(separator=' ', strip=True)
        
        # Clean up whitespace
        text = re.sub(r'\s+', ' ', text).strip()
        
        return text[:config.max_description_length] + "..." if len(text) > config.max_description_length else text
    
    @staticmethod
    def extract_entry_image(entry: Dict[str, Any]) -> Optional[str]:
        """Extract image URL from RSS entry"""
        # Check enclosures first
        for enclosure in entry.get('enclosures', []):
            mime_type = enclosure.get('mime_type', '')
            if mime_type.startswith('image/'):
                return enclosure.get('url')
        
        # Check content fields
        for field in ['content', 'summary', 'description']:
            if field not in entry:
                continue
                
            soup = BeautifulSoup(entry[field], 'html.parser')
            img = soup.find('img')
            if img and img.get('src'):
                return img.get('src')
        
        return None
    
    @staticmethod
    async def process_entry(entry: Dict[str, Any]) -> Embed:
        """Process RSS entry into Discord embed"""
        # Basic entry info
        title = entry.get('title', 'Article sans titre')[:256]  # Discord title limit
        url = entry.get('url', '')
        
        # Get description
        description = ""
        for field in ['summary', 'content']:
            if field in entry:
                description = ContentProcessor.clean_html_content(entry[field])
                if description:
                    break
        
        # Get image
        image_url = ContentProcessor.extract_entry_image(entry)
        
        # Scrape if description is too short or missing
        needs_scraping = not description or len(description) < config.min_paragraph_length
        if needs_scraping and url:
            logger.debug(f"Scraping additional content for: {title}")
            scraped_content, scraped_image = await WebScraper.scrape_article(url)
            
            if scraped_content:
                description = scraped_content
            if scraped_image and not image_url:
                image_url = scraped_image
        
        # Extract dominant color from image
        embed_color = Color.default()
        if image_url:
            try:
                extracted_color = await ColorExtractor.extract_dominant_color(image_url)
                if extracted_color:
                    embed_color = extracted_color
            except Exception as e:
                logger.debug(f"Color extraction failed: {e}")
        
        # Create embed
        embed = Embed(
            title=title,
            url=url,
            description=description or "Aucun contenu disponible",
            color=embed_color
        )
        
        # Add feed info
        feed_info = entry.get('feed', {})
        if feed_info.get('title'):
            embed.set_author(name=feed_info['title'])
        
        # Add image
        if image_url:
            # Ensure proper protocol
            if image_url.startswith('//'):
                image_url = f'https:{image_url}'
            embed.set_image(url=image_url)
        
        return embed

# --- DISCORD POSTER ----------------------------------------------------------

class DiscordPoster:
    """Handles posting to Discord"""
    
    def __init__(self, bot):
        self.bot = bot
        self.miniflux_client = MinifluxClient()
    
    async def post_entry(self, entry: Dict[str, Any]) -> bool:
        """Post a single entry to Discord"""
        channel = self.bot.get_channel(config.discord_channel_id)
        if not channel:
            logger.error(f"Discord channel {config.discord_channel_id} not found")
            return False
        
        try:
            embed = await ContentProcessor.process_entry(entry)
            # Persist canonical entry in data/news.db so bookmarks can later reference full content
            try:
                # Derive an entry_id: prefer Miniflux id if present, else use URL
                entry_id = None
                if entry.get('id'):
                    entry_id = f"miniflux:{entry.get('id')}"
                else:
                    entry_id = f"url:{entry.get('url') or entry.get('guid') or ''}"

                # Extract fields from processed embed and raw entry
                title = embed.title or entry.get('title') or ''
                url = embed.url or entry.get('url') or ''
                summary = embed.description or None
                # We don't have embed.content separate; use summary for now. If you later want full HTML, adapt ContentProcessor to return it.
                image_url = None
                try:
                    if embed.image and embed.image.url:
                        image_url = embed.image.url
                except Exception:
                    image_url = None

                published_at = None
                try:
                    # Miniflux may provide published_at as an ISO string or numeric timestamp
                    pa = entry.get('published_at') or entry.get('published') or None
                    if isinstance(pa, (int, float)):
                        published_at = int(pa)
                    elif isinstance(pa, str):
                        # try ISO parse to timestamp
                        from datetime import datetime as _dt
                        try:
                            dt = _dt.fromisoformat(pa)
                            published_at = int(dt.timestamp())
                        except Exception:
                            published_at = None
                except Exception:
                    published_at = None

                # Upsert into news.db
                import aiosqlite
                db_path = "data/news.db"
                try:
                    async with aiosqlite.connect(db_path) as db:
                        await db.execute(
                            "INSERT OR REPLACE INTO news_entries (entry_id, source, source_entry_id, url, title, summary, content, image_url, published_at, posted_at, extra_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                str(entry_id),
                                'miniflux',
                                str(entry.get('id')) if entry.get('id') is not None else None,
                                url,
                                title,
                                summary,
                                None,
                                image_url,
                                published_at,
                                __import__('datetime').datetime.now().isoformat(),
                                None,
                            ),
                        )
                        await db.commit()
                except Exception:
                    logger.debug("Failed to persist news entry to data/news.db")
            except Exception as e:
                logger.debug(f"Error preparing news DB upsert: {e}")

            # Try to attach a per-user bookmark button view if BookmarkManager is available
            bookmark_cog = self.bot.get_cog('BookmarkManager')
            view = None
            try:
                if bookmark_cog:
                    # Pass the raw entry dict so the view can extract id/title/url
                    view = bookmark_cog.make_entry_view(entry)
            except Exception:
                logger.debug("Bookmark view not available")

            if view:
                await channel.send(embed=embed, view=view)
            else:
                await channel.send(embed=embed)
            
            logger.info(f"Posted article: {entry.get('title', 'Untitled')}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to post entry {entry.get('id')}: {e}")
            return False
    
    async def process_entries(self) -> None:
        """Fetch and process all unread entries"""
        try:
            entries = await self.miniflux_client.fetch_unread_entries()
            
            if not entries:
                logger.debug("No unread entries found")
                return
            
            posted_entry_ids = []
            
            for entry in entries:
                success = await self.post_entry(entry)
                if success:
                    posted_entry_ids.append(entry.get('id'))
                
                # Rate limiting
                await asyncio.sleep(config.request_delay)
            
            # Mark successfully posted entries as read
            if posted_entry_ids:
                await self.miniflux_client.mark_as_read(posted_entry_ids)
                logger.info(f"Marked {len(posted_entry_ids)} entries as read")
            
        except MinifluxError as e:
            logger.error(f"Miniflux error: {e}")
        except Exception as e:
            logger.error(f"Unexpected error processing entries: {e}")

# --- MAIN LOOP ---------------------------------------------------------------

async def run_miniflux_loop(bot) -> None:
    """Main loop for processing RSS feeds"""
    logger.info("Starting Miniflux RSS loop")
    
    # Wait for bot to be ready
    await asyncio.sleep(5)
    
    poster = DiscordPoster(bot)
    
    while True:
        try:
            entries_before = await poster.miniflux_client.fetch_unread_entries()
            entry_count_before = len(entries_before)
            
            # Process entries
            await poster.process_entries()
            
            # Determine sleep interval based on activity
            if entry_count_before > 0:
                sleep_time = config.fetch_interval_with_entries
                logger.debug(f"Processed {entry_count_before} entries, sleeping for {sleep_time}s")
            else:
                sleep_time = config.fetch_interval_no_entries
                logger.debug(f"No entries found, sleeping for {sleep_time}s")
            
            await asyncio.sleep(sleep_time)
            
        except Exception as e:
            logger.exception(f"RSS loop crashed: {e}")
            await asyncio.sleep(60)  # Wait before retrying

# --- CLEANUP -----------------------------------------------------------------

async def cleanup_miniflux():
    """Cleanup function to call on shutdown"""
    logger.info("Cleaning up Miniflux resources")
    await session_manager.close()
