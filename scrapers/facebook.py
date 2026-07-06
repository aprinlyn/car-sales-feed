"""Facebook Marketplace car listings scraper."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from playwright.async_api import Page

from config.manager import ConfigManager
from models.listing import RawListing, ScrapingStats, SourcePlatform
from scrapers.base import BaseScraper, CaptchaDetectedError, ScraperError

logger = logging.getLogger(__name__)

FB_BASE_URL = "https://www.facebook.com"
FB_MARKETPLACE_VEHICLES_URL = f"{FB_BASE_URL}/marketplace/category/vehicles"


class AuthenticationError(ScraperError):
    """Raised when Facebook authentication fails."""
    pass


class FacebookScraper(BaseScraper):
    """Scrapes car listings from Facebook Marketplace.

    Features:
    - Session persistence via Browser_Profile (avoids re-login)
    - Authentication check before scraping
    - Infinite scroll to load additional listings
    - Captcha/block detection with retry
    - Individual listing failure isolation
    """

    def __init__(self, config: ConfigManager):
        super().__init__(config)
        self._max_listings = int(config.get("scraping.facebook.max_listings", 100))
        self._timeout_ms = int(config.get("scraping.facebook.timeout_seconds", 30)) * 1000
        self._captcha_pause = float(config.get("scraping.facebook.captcha_pause_seconds", 60))
        self._max_captcha_retries = 3

    async def scrape(self) -> list[RawListing]:
        """Execute a full Facebook Marketplace scraping session."""
        self.stats = ScrapingStats(start_time=datetime.utcnow())
        listings: list[RawListing] = []

        try:
            context = await self._create_context()
            page = await context.new_page()

            # Check session / authenticate
            if not await self._has_valid_session(page):
                logger.error("No valid Facebook session. Please log in manually with headless=false first.")
                raise AuthenticationError("No valid Facebook session in Browser_Profile")

            # Navigate to Marketplace vehicles
            await self._navigate_with_retry(page, FB_MARKETPLACE_VEHICLES_URL, timeout_ms=self._timeout_ms)
            await asyncio.sleep(3)

            # Check for captcha/block
            if await self._detect_captcha(page):
                await self._handle_captcha(page)

            # Scroll and collect listing URLs
            listing_elements = await self._scroll_and_load(page, self._max_listings)
            logger.info("Found %d listing elements on Marketplace", len(listing_elements))

            # Extract data from each listing
            for i, element_data in enumerate(listing_elements):
                try:
                    listing = await self._extract_listing(page, element_data)
                    if listing is None:
                        self.stats.total_skipped += 1
                        continue

                    if self._validate_listing(listing):
                        listings.append(listing)
                        self.stats.total_extracted += 1
                    else:
                        self.stats.total_skipped += 1
                except Exception as e:
                    self.stats.total_failed += 1
                    logger.warning("Failed to extract FB listing %d: %s", i, str(e))

        except AuthenticationError:
            logger.error("Facebook authentication failed. Halting session.")
        except CaptchaDetectedError:
            logger.error("[%s] Facebook session aborted due to unresolved captcha", time.strftime("%Y-%m-%d %H:%M:%S"))
        except ScraperError as e:
            logger.error("[%s] Facebook scraping failed: %s", time.strftime("%Y-%m-%d %H:%M:%S"), str(e))
        finally:
            await self._close_context()
            self.stats.end_time = datetime.utcnow()
            self._log_session_summary()

        return listings

    async def _has_valid_session(self, page: Page) -> bool:
        """Check if the persistent browser profile has a valid Facebook session."""
        try:
            await page.goto(FB_BASE_URL, timeout=self._timeout_ms, wait_until="domcontentloaded")
            await asyncio.sleep(3)

            # Check for logged-in indicators
            logged_in_selectors = [
                "[aria-label='Your profile']",
                "[aria-label='Account']",
                "[data-pagelet='ProfileTail']",
                "div[role='navigation'] a[href*='/me']",
                "[aria-label='Messenger']",
            ]
            for selector in logged_in_selectors:
                el = await page.query_selector(selector)
                if el:
                    logger.info("Valid Facebook session found")
                    return True

            # Check for login form (means not logged in)
            login_form = await page.query_selector("input[name='email'], #email, form[data-testid='royal_login_form']")
            if login_form:
                logger.info("Facebook login form detected — no valid session")
                return False

            # Ambiguous — check URL
            if "login" in page.url or "checkpoint" in page.url:
                return False

            return True
        except Exception as e:
            logger.debug("Facebook session check failed: %s", str(e))
            return False

    async def _scroll_and_load(self, page: Page, max_listings: int) -> list[dict]:
        """Handle infinite scroll to load listings up to max_listings.

        Returns a list of dicts with {url, title, price} extracted from cards.
        """
        collected: list[dict] = []
        seen_urls: set[str] = set()
        no_new_count = 0
        max_scroll_attempts = 30

        for scroll_attempt in range(max_scroll_attempts):
            # Extract visible listing cards
            new_items = await page.evaluate("""
                () => {
                    const items = [];
                    const links = document.querySelectorAll('a[href*="/marketplace/item/"]');
                    links.forEach(link => {
                        const href = link.getAttribute('href');
                        const url = href.startsWith('http') ? href : 'https://www.facebook.com' + href;
                        // Try to get title and price from the card
                        const text = link.innerText || '';
                        items.push({url: url, text: text});
                    });
                    return items;
                }
            """)

            # Deduplicate and collect
            added = 0
            for item in new_items:
                url = item["url"].split("?")[0]  # Remove query params
                if url not in seen_urls:
                    seen_urls.add(url)
                    collected.append({"url": url, "text": item.get("text", "")})
                    added += 1

            if len(collected) >= max_listings:
                break

            if added == 0:
                no_new_count += 1
                if no_new_count >= 5:
                    logger.info("No new listings after %d scroll attempts, stopping", no_new_count)
                    break
            else:
                no_new_count = 0

            # Scroll down
            await page.mouse.wheel(0, 1000)
            await asyncio.sleep(2)

        return collected[:max_listings]

    async def _extract_listing(self, page: Page, element_data: dict) -> RawListing | None:
        """Extract data from a listing by navigating to its detail page."""
        url = element_data.get("url", "")
        if not url:
            return None

        try:
            result = await asyncio.wait_for(
                self._do_extract_fb_detail(url),
                timeout=30,
            )
            return result
        except asyncio.TimeoutError:
            logger.warning("FB detail extraction timed out for %s", url)
            return None
        except Exception as e:
            logger.debug("Error extracting FB listing %s: %s", url, str(e))
            return None

    async def _do_extract_fb_detail(self, url: str) -> RawListing | None:
        """Navigate to a Facebook listing detail and extract data."""
        # Reuse or create detail page
        if not hasattr(self, '_detail_page') or self._detail_page is None or self._detail_page.is_closed():
            self._detail_page = await self._context.new_page() if self._context else None

        detail_page = self._detail_page
        if not detail_page:
            return None

        await self._navigate_with_retry(detail_page, url, timeout_ms=self._timeout_ms)
        await asyncio.sleep(2)

        # Extract title
        title = ""
        title_el = await detail_page.query_selector(
            "h1, "
            "span[class*='title'], "
            "[data-testid='marketplace_listing_title']"
        )
        if title_el:
            title = (await title_el.inner_text()).strip()

        # Extract price
        price = 0.0
        price_el = await detail_page.query_selector(
            "[data-testid='marketplace_listing_price'], "
            "span[class*='price']"
        )
        if price_el:
            price_text = await price_el.inner_text()
            price = self._parse_price(price_text)

        # If no title from specific selector, try from page title
        if not title:
            page_title = await detail_page.title()
            if page_title and "Marketplace" in page_title:
                title = page_title.split(" | ")[0].strip()

        # Extract location
        location = ""
        loc_el = await detail_page.query_selector(
            "span:has-text('Listed in'), "
            "[class*='location']"
        )
        if loc_el:
            location = (await loc_el.inner_text()).replace("Listed in", "").strip()

        # Extract description
        description = ""
        desc_el = await detail_page.query_selector(
            "[data-testid='marketplace_listing_description'], "
            "span[class*='description']"
        )
        if desc_el:
            description = (await desc_el.inner_text()).strip()

        # Extract seller name
        seller_name = ""
        seller_el = await detail_page.query_selector(
            "a[href*='/marketplace/profile/'] span, "
            "[data-testid='marketplace_listing_seller_name']"
        )
        if seller_el:
            seller_name = (await seller_el.inner_text()).strip()

        # Extract vehicle details from the listing attributes
        mileage = None
        year = None
        detail_spans = await detail_page.query_selector_all(
            "div[class*='attribute'] span, "
            "li span"
        )
        for span in detail_spans:
            text = await span.inner_text()
            text_lower = text.lower()
            if "km" in text_lower or "kilometer" in text_lower:
                mileage = self._parse_number(text)
            elif text.strip().isdigit() and len(text.strip()) == 4:
                potential_year = int(text.strip())
                if 1990 <= potential_year <= 2030:
                    year = potential_year

        if not title and not price:
            return None

        return RawListing(
            title=title,
            price=price,
            currency="IDR",
            location=location,
            description=description,
            seller_name=seller_name,
            source_url=url,
            source_platform=SourcePlatform.FACEBOOK,
            scrape_timestamp=datetime.utcnow(),
            mileage=mileage,
            year_of_manufacture=year,
            image_urls=[],  # Skipped for MVP
        )

    async def _detect_captcha(self, page: Page) -> bool:
        """Detect Facebook-specific blocks."""
        try:
            content = await page.content()
            content_lower = content.lower()
            block_phrases = [
                "you must log in to continue",
                "confirm your identity",
                "security check",
                "we need to verify",
                "account has been disabled",
            ]
            for phrase in block_phrases:
                if phrase in content_lower:
                    return True

            # Check URL for checkpoint
            if "checkpoint" in page.url:
                return True

            return False
        except Exception:
            return False

    async def _handle_captcha(self, page: Page) -> None:
        """Handle Facebook captcha/block with manual resolution in headed mode."""
        headless = self.config.get("browser.headless", False)

        for attempt in range(self._max_captcha_retries):
            if not headless:
                logger.warning(
                    "Facebook block detected. Please resolve manually in the browser. "
                    "Waiting up to 120s (attempt %d/%d)...",
                    attempt + 1, self._max_captcha_retries,
                )
                for _ in range(40):
                    await asyncio.sleep(3)
                    if not await self._detect_captcha(page):
                        logger.info("Facebook block resolved.")
                        return
            else:
                logger.warning(
                    "Facebook block detected (attempt %d/%d). Pausing %ds...",
                    attempt + 1, self._max_captcha_retries, int(self._captcha_pause),
                )
                await asyncio.sleep(self._captcha_pause)
                try:
                    await page.reload(timeout=self._timeout_ms)
                except Exception:
                    pass
                if not await self._detect_captcha(page):
                    return

        raise CaptchaDetectedError("Facebook block not resolved after retries")

    @staticmethod
    def _parse_price(price_text: str) -> float:
        """Parse Facebook price string to float."""
        if not price_text:
            return 0.0
        cleaned = price_text.replace("Rp", "").replace("IDR", "").replace("₫", "")
        cleaned = cleaned.replace(".", "").replace(",", "").replace(" ", "")
        digits = "".join(c for c in cleaned if c.isdigit())
        try:
            return float(digits) if digits else 0.0
        except ValueError:
            return 0.0

    @staticmethod
    def _parse_number(text: str) -> int | None:
        """Extract a number from text."""
        digits = "".join(c for c in text if c.isdigit())
        try:
            return int(digits) if digits else None
        except ValueError:
            return None
