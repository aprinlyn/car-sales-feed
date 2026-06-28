"""Base scraper with shared browser context and retry logic."""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Any

from playwright.async_api import BrowserContext, Page, async_playwright

from config.manager import ConfigManager
from models.listing import RawListing, ScrapingStats

logger = logging.getLogger(__name__)


class ScraperError(Exception):
    """Base exception for scraper errors."""

    pass


class CaptchaDetectedError(ScraperError):
    """Raised when a captcha or block is detected."""

    pass


class BaseScraper(ABC):
    """Abstract base class for scrapers using Playwright persistent browser context.

    Provides:
    - Persistent browser context creation (retains cookies/sessions between runs)
    - Navigation with exponential backoff retry
    - Listing validation (required fields check)
    - Scraping stats tracking
    """

    def __init__(self, config: ConfigManager):
        self.config = config
        self.stats = ScrapingStats()
        self._profile_dir = config.get("browser.profile_dir", "./browser_profile")
        self._playwright: Any = None
        self._context: BrowserContext | None = None

    async def _create_context(self) -> BrowserContext:
        """Launch a persistent browser context using the configured profile directory.

        Uses playwright.chromium.launch_persistent_context(user_data_dir=self._profile_dir)
        to retain cookies, local storage, and sessions across runs.
        """
        headless = self.config.get("browser.headless", True)
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=self._profile_dir,
            headless=headless,
            locale="id-ID",
            timezone_id="Asia/Jakarta",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--use-mock-keychain",
            ],
        )
        # Remove the navigator.webdriver flag that exposes automation
        for page in self._context.pages:
            await page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
        self._context.on("page", lambda page: page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        ))
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
    async def scrape(self) -> list[RawListing]:
        """Execute a full scraping session. Returns extracted listings."""
        ...

    async def _navigate_with_retry(
        self,
        page: Page,
        url: str,
        timeout_ms: int = 30000,
        max_retries: int = 3,
        base_delay: float = 5.0,
        max_delay: float = 60.0,
    ) -> None:
        """Navigate to a URL with exponential backoff retry logic.

        Args:
            page: Playwright page instance
            url: URL to navigate to
            timeout_ms: Page load timeout in milliseconds
            max_retries: Maximum number of retry attempts
            base_delay: Initial retry delay in seconds
            max_delay: Maximum retry delay cap in seconds

        Raises:
            ScraperError: If all retry attempts are exhausted
        """
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                return
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    delay = min(base_delay * (2**attempt), max_delay)
                    logger.warning(
                        "[%s] Navigation to %s failed (attempt %d/%d): %s. Retrying in %.1fs...",
                        time.strftime("%Y-%m-%d %H:%M:%S"),
                        url,
                        attempt + 1,
                        max_retries + 1,
                        str(e),
                        delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        "[%s] Navigation to %s failed after %d attempts: %s",
                        time.strftime("%Y-%m-%d %H:%M:%S"),
                        url,
                        max_retries + 1,
                        str(e),
                    )

        raise ScraperError(
            f"Failed to navigate to {url} after {max_retries + 1} attempts: {last_error}"
        )

    def _validate_listing(self, listing: RawListing) -> bool:
        """Check required fields: title must be non-empty, price must be positive.

        Returns True if the listing is valid, False otherwise.
        """
        # Title must be non-empty and not just whitespace
        if not listing.title or not listing.title.strip():
            return False

        # Price must be positive
        if listing.price is None or listing.price <= 0:
            return False

        return True

    def _log_session_summary(self) -> None:
        """Log the scraping session summary."""
        duration = ""
        if self.stats.start_time and self.stats.end_time:
            elapsed = (self.stats.end_time - self.stats.start_time).total_seconds()
            duration = f" (duration: {elapsed:.1f}s)"

        logger.info(
            "Scraping session complete%s: extracted=%d, skipped=%d, failed=%d",
            duration,
            self.stats.total_extracted,
            self.stats.total_skipped,
            self.stats.total_failed,
        )
