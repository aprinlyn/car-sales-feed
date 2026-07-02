"""Twitter (X) publisher using Playwright UI automation."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from playwright.async_api import Page

from config.manager import ConfigManager
from models.listing import PublishingStats
from persistence.database import DatabaseManager
from persistence.models import StoredListing
from publishers.base import BasePublisher, SessionInvalidError

logger = logging.getLogger(__name__)


class TwitterPublisher(BasePublisher):
    """Publishes car listings to Twitter (x.com) via UI automation."""

    PLATFORM = "twitter"
    MAX_LENGTH = 280

    def __init__(self, config: ConfigManager, db: DatabaseManager):
        super().__init__(config, db)
        self.MAX_POSTS_PER_RUN = int(config.get("publishing.twitter.max_posts_per_run", 30))
        self.POST_INTERVAL_SECONDS = int(config.get("publishing.twitter.post_interval_seconds", 120))

    def format_post(self, listing: StoredListing) -> str:
        """Format within 280 chars: title, price, location, trust_level, source_url.

        Truncates title with ellipsis if needed to fit.
        """
        price_str = f"Rp {listing.price:,.0f}"
        location = listing.location or "Indonesia"
        trust = f"[{listing.trust_level.upper()}]"
        url = listing.source_url

        # Fixed parts (always included)
        fixed = f"\n{price_str} | {location}\n{trust}\n{url}"
        available_for_title = self.MAX_LENGTH - len(fixed) - 1  # -1 for newline after title

        title = listing.title
        if len(title) > available_for_title:
            title = title[: available_for_title - 3] + "..."

        post = f"{title}\n{price_str} | {location}\n{trust}\n{url}"

        # Final safety check
        if len(post) > self.MAX_LENGTH:
            # Aggressively truncate title
            excess = len(post) - self.MAX_LENGTH
            title = title[: len(title) - excess - 3] + "..."
            post = f"{title}\n{price_str} | {location}\n{trust}\n{url}"

        return post

    async def publish_batch(self) -> PublishingStats:
        """Publish a batch of safe listings to Twitter."""
        stats = PublishingStats(platform=self.PLATFORM)

        # Get unpublished safe listings
        listings = self.db.get_unpublished(
            platform=self.PLATFORM,
            trust_level="safe",
            limit=self.MAX_POSTS_PER_RUN,
        )

        if not listings:
            logger.info("No unpublished safe listings for Twitter")
            return stats

        try:
            context = await self._create_context()
            page = await context.new_page()

            # Verify session
            if not await self._check_session(page):
                logger.error("No valid Twitter session. Halting publishing run.")
                stats.total_failed = len(listings)
                return stats

            # Publish each listing
            for i, listing in enumerate(listings):
                try:
                    await self._publish_single(page, listing)
                    self.db.mark_published(
                        listing_id=listing.id,
                        platform=self.PLATFORM,
                        published_at=datetime.utcnow(),
                    )
                    stats.total_published += 1
                    logger.info(
                        "Published to Twitter (%d/%d): %s",
                        i + 1, len(listings), listing.title,
                    )
                except Exception as e:
                    stats.total_failed += 1
                    logger.error("Failed to publish to Twitter: %s - %s", listing.title, str(e))

                # Space posts apart
                if i < len(listings) - 1:
                    logger.debug("Waiting %ds before next post...", self.POST_INTERVAL_SECONDS)
                    await asyncio.sleep(self.POST_INTERVAL_SECONDS)

        except SessionInvalidError:
            logger.error("Twitter session invalid. Halting run.")
            stats.total_failed = len(listings) - stats.total_published
        except Exception as e:
            logger.error("Twitter publishing error: %s", str(e))
        finally:
            await self._close_context()

        logger.info(
            "Twitter publishing complete: %d published, %d failed",
            stats.total_published, stats.total_failed,
        )
        return stats

    async def _check_session(self, page: Page) -> bool:
        """Navigate to x.com and verify logged-in state."""
        try:
            await page.goto("https://x.com/home", timeout=30000, wait_until="domcontentloaded")
            await asyncio.sleep(3)

            # Check for logged-in indicators
            compose_btn = await page.query_selector(
                "[data-testid='SideNav_NewTweet_Button'], "
                "[aria-label='Post'], "
                "a[href='/compose/post']"
            )
            if compose_btn:
                return True

            # Check if we're on a login page
            login_form = await page.query_selector(
                "[data-testid='loginForm'], "
                "input[name='text'][autocomplete='username']"
            )
            if login_form:
                return False

            # Check for home timeline elements
            timeline = await page.query_selector("[data-testid='primaryColumn']")
            return timeline is not None
        except Exception as e:
            logger.debug("Twitter session check failed: %s", str(e))
            return False

    async def _compose_post(self, page: Page, text: str) -> None:
        """Navigate to compose UI and enter tweet text."""
        # Click the compose button or navigate to compose
        await page.goto("https://x.com/compose/post", timeout=30000, wait_until="domcontentloaded")
        await asyncio.sleep(2)

        # Find the text area and type
        editor = await page.query_selector(
            "[data-testid='tweetTextarea_0'], "
            "[role='textbox'][data-testid='tweetTextarea_0'], "
            "div[role='textbox']"
        )
        if editor:
            await editor.click()
            await page.keyboard.type(text, delay=20)
        else:
            raise PublishingError("Could not find tweet compose text area")

    async def _submit_post(self, page: Page) -> bool:
        """Click the Post button and wait for confirmation."""
        submit_btn = await page.query_selector(
            "[data-testid='tweetButton'], "
            "[data-testid='tweetButtonInline']"
        )
        if not submit_btn:
            return False

        await submit_btn.click()

        # Wait for post confirmation (toast or redirect)
        try:
            await page.wait_for_selector(
                "[data-testid='toast'], "
                "[role='alert']",
                timeout=30000,
            )
            return True
        except Exception:
            # Check if URL changed (indicates success)
            await asyncio.sleep(3)
            return "compose" not in page.url

    async def _publish_single(self, page: Page, listing: StoredListing) -> None:
        """Publish a single tweet."""
        text = self.format_post(listing)

        await self._compose_post(page, text)
        await asyncio.sleep(1)

        success = await self._submit_post(page)
        if not success:
            raise PublishingError(f"Post submission failed for: {listing.title}")
