"""Threads publisher using Playwright UI automation."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from playwright.async_api import Page

from config.manager import ConfigManager
from models.listing import PublishingStats
from persistence.database import DatabaseManager
from persistence.models import StoredListing
from publishers.base import BasePublisher, SessionInvalidError, PublishingError

logger = logging.getLogger(__name__)


class ThreadsPublisher(BasePublisher):
    """Publishes car listings to Threads (threads.net) via UI automation."""

    PLATFORM = "threads"
    MAX_LENGTH = 500

    def __init__(self, config: ConfigManager, db: DatabaseManager):
        super().__init__(config, db)
        self.MAX_POSTS_PER_RUN = int(config.get("publishing.threads.max_posts_per_run", 25))
        self.POST_INTERVAL_SECONDS = int(config.get("publishing.threads.post_interval_seconds", 120))
        self._max_retries_per_post = 3
        self._retry_delay = 60

    def format_post(self, listing: StoredListing) -> str:
        """Format within 500 chars: title, price, location, trust_level, source_url.

        Truncates title if needed to fit.
        """
        price_str = f"Rp {listing.price:,.0f}"
        location = listing.location or "Indonesia"
        trust = f"[{listing.trust_level.upper()}]"
        url = listing.source_url

        # Fixed parts
        fixed = f"\n\n{price_str}\n📍 {location}\n{trust}\n\n{url}"
        available_for_title = self.MAX_LENGTH - len(fixed) - 1

        title = listing.title
        if len(title) > available_for_title:
            title = title[: available_for_title - 3] + "..."

        post = f"{title}\n\n{price_str}\n📍 {location}\n{trust}\n\n{url}"

        # Final safety check
        if len(post) > self.MAX_LENGTH:
            excess = len(post) - self.MAX_LENGTH
            title = title[: len(title) - excess - 3] + "..."
            post = f"{title}\n\n{price_str}\n📍 {location}\n{trust}\n\n{url}"

        return post

    async def publish_batch(self) -> PublishingStats:
        """Publish a batch of safe listings to Threads."""
        stats = PublishingStats(platform=self.PLATFORM)

        listings = self.db.get_unpublished(
            platform=self.PLATFORM,
            trust_level="safe",
            limit=self.MAX_POSTS_PER_RUN,
        )

        if not listings:
            logger.info("No unpublished safe listings for Threads")
            return stats

        try:
            context = await self._create_context()
            page = await context.new_page()

            # Verify session
            if not await self._check_session(page):
                logger.error("No valid Threads session. Halting publishing run.")
                stats.total_failed = len(listings)
                return stats

            # Publish each listing
            for i, listing in enumerate(listings):
                success = await self._publish_with_retry(page, listing)
                if success:
                    self.db.mark_published(
                        listing_id=listing.id,
                        platform=self.PLATFORM,
                        published_at=datetime.utcnow(),
                    )
                    stats.total_published += 1
                    logger.info(
                        "Published to Threads (%d/%d): %s",
                        i + 1, len(listings), listing.title,
                    )
                else:
                    stats.total_failed += 1
                    logger.error("Failed to publish to Threads after retries: %s", listing.title)

                # Space posts apart
                if i < len(listings) - 1:
                    logger.debug("Waiting %ds before next post...", self.POST_INTERVAL_SECONDS)
                    await asyncio.sleep(self.POST_INTERVAL_SECONDS)

        except SessionInvalidError:
            logger.error("Threads session invalid. Halting run.")
            stats.total_failed = len(listings) - stats.total_published
        except Exception as e:
            logger.error("Threads publishing error: %s", str(e))
        finally:
            await self._close_context()

        logger.info(
            "Threads publishing complete: %d published, %d failed",
            stats.total_published, stats.total_failed,
        )
        return stats

    async def _publish_with_retry(self, page: Page, listing: StoredListing) -> bool:
        """Attempt to publish with up to 3 retries and 60s delay between attempts."""
        for attempt in range(self._max_retries_per_post):
            try:
                await self._publish_single(page, listing)
                return True
            except Exception as e:
                logger.warning(
                    "Threads post attempt %d/%d failed for '%s': %s",
                    attempt + 1, self._max_retries_per_post, listing.title, str(e),
                )
                if attempt < self._max_retries_per_post - 1:
                    await asyncio.sleep(self._retry_delay)
        return False

    async def _check_session(self, page: Page) -> bool:
        """Navigate to threads.net and verify logged-in state."""
        try:
            await page.goto("https://www.threads.net", timeout=30000, wait_until="domcontentloaded")
            await asyncio.sleep(3)

            # Check for logged-in indicators (compose button, profile icon)
            logged_in = await page.query_selector(
                "[aria-label='Create'], "
                "[aria-label='New thread'], "
                "a[href*='/create'], "
                "svg[aria-label='Create']"
            )
            if logged_in:
                return True

            # Check for login page
            login_indicator = await page.query_selector(
                "input[name='username'], "
                "a[href*='login'], "
                "[data-testid='login-button']"
            )
            return login_indicator is None
        except Exception as e:
            logger.debug("Threads session check failed: %s", str(e))
            return False

    async def _compose_post(self, page: Page, text: str) -> None:
        """Navigate to Threads compose UI and enter post text."""
        # Click compose/create button
        create_btn = await page.query_selector(
            "[aria-label='Create'], "
            "[aria-label='New thread'], "
            "a[href*='/create']"
        )
        if create_btn:
            await create_btn.click()
            await asyncio.sleep(2)

        # Find text area
        editor = await page.query_selector(
            "div[role='textbox'], "
            "[contenteditable='true'], "
            "div[aria-label='Text']"
        )
        if editor:
            await editor.click()
            await page.keyboard.type(text, delay=20)
        else:
            raise PublishingError("Could not find Threads compose text area")

    async def _submit_post(self, page: Page) -> bool:
        """Click the Post button and wait for confirmation."""
        submit_btn = await page.query_selector(
            "div[role='button']:has-text('Post'), "
            "button:has-text('Post'), "
            "[data-testid='post-button']"
        )
        if not submit_btn:
            return False

        await submit_btn.click()

        # Wait for confirmation
        try:
            await asyncio.sleep(5)
            # Check if compose modal closed (indicates success)
            editor = await page.query_selector("div[role='textbox']")
            return editor is None
        except Exception:
            return False

    async def _publish_single(self, page: Page, listing: StoredListing) -> None:
        """Publish a single Threads post."""
        text = self.format_post(listing)

        await self._compose_post(page, text)
        await asyncio.sleep(1)

        success = await self._submit_post(page)
        if not success:
            raise PublishingError(f"Post submission failed for: {listing.title}")
