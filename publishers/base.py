"""Base publisher with shared browser context and post formatting."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

import aiohttp
from playwright.async_api import BrowserContext, Page, async_playwright

from config.manager import ConfigManager
from models.listing import PublishingStats
from persistence.database import DatabaseManager
from persistence.models import StoredListing

logger = logging.getLogger(__name__)


class PublishingError(Exception):
    """Base exception for publishing errors."""
    pass


class SessionInvalidError(PublishingError):
    """Raised when no valid logged-in session exists."""
    pass


class BasePublisher(ABC):
    """Abstract base class for social media publishers.

    Uses the same persistent Playwright browser context as scrapers.
    Provides common methods for session checking, post composition,
    image upload, and submission.
    """

    PLATFORM: str = ""
    MAX_LENGTH: int = 280
    POST_INTERVAL_SECONDS: int = 120
    MAX_POSTS_PER_RUN: int = 30

    def __init__(self, config: ConfigManager, db: DatabaseManager):
        self.config = config
        self.db = db
        self._profile_dir = config.get("browser.profile_dir", "./browser_profile")
        self._playwright: Any = None
        self._context: BrowserContext | None = None

    async def _create_context(self) -> BrowserContext:
        """Launch a persistent browser context using the configured profile directory."""
        headless = self.config.get("browser.headless", False)
        channel = self.config.get("browser.channel", "chrome")

        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=self._profile_dir,
            headless=headless,
            channel=channel,
            locale="id-ID",
            viewport={"width": 1366, "height": 768},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--use-mock-keychain",
            ],
        )
        return self._context

    async def _close_context(self) -> None:
        """Close the browser context and playwright instance."""
        if self._context:
            await self._context.close()
            self._context = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    @abstractmethod
    async def publish_batch(self) -> PublishingStats:
        """Publish a batch of safe listings."""
        ...

    @abstractmethod
    def format_post(self, listing: StoredListing) -> str:
        """Format a listing into a platform-specific post."""
        ...

    @abstractmethod
    async def _check_session(self, page: Page) -> bool:
        """Verify that a valid logged-in session exists."""
        ...

    @abstractmethod
    async def _compose_post(self, page: Page, text: str) -> None:
        """Navigate to compose UI and enter post text."""
        ...

    async def _submit_post(self, page: Page) -> bool:
        """Click submit and wait for confirmation (30s timeout).

        Returns True if post was confirmed, False otherwise.
        Subclasses should override with platform-specific selectors.
        """
        return False

    async def _download_image(self, url: str) -> str | None:
        """Download a listing image to a temp file for browser upload.

        Returns the local file path, or None if download fails.
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.read()
                    if not data:
                        return None

                    # Determine extension
                    content_type = resp.headers.get("Content-Type", "image/jpeg")
                    ext = ".jpg"
                    if "png" in content_type:
                        ext = ".png"
                    elif "webp" in content_type:
                        ext = ".webp"

                    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
                    tmp.write(data)
                    tmp.close()
                    return tmp.name
        except Exception as e:
            logger.warning("Failed to download image %s: %s", url, str(e))
            return None
