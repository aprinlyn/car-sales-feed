"""OLX Indonesia car listings scraper."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime

from playwright.async_api import Page, ElementHandle

from config.manager import ConfigManager
from models.listing import RawListing, ScrapingStats, SourcePlatform
from scrapers.base import BaseScraper, CaptchaDetectedError, ScraperError
from scrapers.locations import get_location
from scrapers.selectors import OLXSelectors

logger = logging.getLogger(__name__)

OLX_BASE_URL = "https://www.olx.co.id"


class OLXScraper(BaseScraper):
    """Scrapes car listings from OLX Indonesia (olx.co.id).

    Features:
    - Navigates car sales section with configurable page load timeout
    - Extracts all listing fields (title, price, location, etc.)
    - Pagination up to configurable max pages
    - Captcha detection with pause and retry
    - Individual listing failure isolation
    - Session summary logging
    """

    def __init__(self, config: ConfigManager):
        super().__init__(config)
        self._max_pages = int(config.get("scraping.olx.max_pages", 10))
        self._page_delay = float(config.get("scraping.olx.page_delay_seconds", 3))
        self._timeout_ms = int(config.get("scraping.olx.timeout_seconds", 30)) * 1000
        self._captcha_pause = float(config.get("scraping.olx.captcha_pause_seconds", 60))
        self._max_captcha_retries = 3

        # Build OLX URL with location filter
        location_name = config.get("browser.location", "jakarta")
        city = get_location(location_name)
        self._cars_url = f"{OLX_BASE_URL}/mobil-bekas_c198?filter=location_{city.olx_location_slug}"
        logger.info("OLX scraper configured for location: %s (%s)", city.name, self._cars_url)

    async def scrape(self) -> list[RawListing]:
        """Execute a full OLX scraping session.

        Returns list of validated RawListings.
        """
        self.stats = ScrapingStats(start_time=datetime.utcnow())
        listings: list[RawListing] = []

        try:
            context = await self._create_context()
            page = await context.new_page()

            # Navigate to OLX cars section
            await self._navigate_with_retry(
                page, self._cars_url, timeout_ms=self._timeout_ms
            )

            # Brief pause to let initial content render
            await asyncio.sleep(2)

            # In headed mode, let user handle any captcha/challenge manually
            # Only auto-detect in headless mode
            headless = self.config.get("browser.headless", True)
            if headless and await self._detect_captcha(page):
                await self._handle_captcha(page)
            elif not headless:
                # Wait for user to solve any challenge if present
                # Just check if listings exist yet, if not wait longer
                for _ in range(30):  # Wait up to 90s for page to be ready
                    el = await page.query_selector(OLXSelectors.LISTING_CARD)
                    if el:
                        break
                    await asyncio.sleep(3)

            # Scrape pages
            for page_num in range(1, self._max_pages + 1):
                logger.info("Scraping OLX page %d/%d", page_num, self._max_pages)

                page_listings = await self._extract_listings_from_page(page)
                listings.extend(page_listings)

                # Try to navigate to next page
                if page_num < self._max_pages:
                    has_next = await self._navigate_next_page(page, page_num)
                    if not has_next:
                        logger.info("No more pages available after page %d", page_num)
                        break

                    # Configurable delay between pages
                    await asyncio.sleep(self._page_delay)

        except CaptchaDetectedError:
            logger.error(
                "[%s] OLX session aborted due to unresolved captcha",
                time.strftime("%Y-%m-%d %H:%M:%S"),
            )
        except ScraperError as e:
            logger.error(
                "[%s] OLX scraping session failed: %s",
                time.strftime("%Y-%m-%d %H:%M:%S"),
                str(e),
            )
        finally:
            await self._close_context()
            self.stats.end_time = datetime.utcnow()
            self._log_session_summary()

        return listings

    async def _extract_listings_from_page(self, page: Page) -> list[RawListing]:
        """Extract all listings from the current results page."""
        listings: list[RawListing] = []

        # Scroll down to trigger lazy-loading of listing cards (ads cover top)
        await self._scroll_to_load_listings(page)

        # Wait for listing elements to appear
        try:
            await page.wait_for_selector(OLXSelectors.LISTING_CARD, timeout=30000)
        except Exception:
            # Debug: save screenshot and HTML to diagnose why listings aren't found
            try:
                await page.screenshot(path="debug_screenshot.png")
                html = await page.content()
                with open("debug_page.html", "w", encoding="utf-8") as f:
                    f.write(html)
                logger.warning(
                    "No listing elements found on page. "
                    "Saved debug_screenshot.png and debug_page.html for inspection."
                )
            except Exception as e:
                logger.warning("No listing elements found on page (debug save failed: %s)", e)
            return listings

        listing_elements = await page.query_selector_all(OLXSelectors.LISTING_CARD)

        for element in listing_elements:
            try:
                listing = await self._extract_listing(page, element)
                if listing is None:
                    self.stats.total_skipped += 1
                    continue

                if self._validate_listing(listing):
                    listings.append(listing)
                    self.stats.total_extracted += 1
                else:
                    self.stats.total_skipped += 1
                    logger.debug("Listing skipped (validation failed): %s", listing.title)
            except Exception as e:
                self.stats.total_failed += 1
                logger.warning(
                    "[%s] Failed to extract listing: %s",
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    str(e),
                )

        return listings

    async def _extract_listing(self, page: Page, element: ElementHandle) -> RawListing | None:
        """Extract data from a single listing element on the results page.

        For OLX, we first extract summary data from the listing card,
        then optionally navigate to the detail page for full data.
        """
        try:
            # Extract link to detail page
            link_el = await element.query_selector(OLXSelectors.LISTING_LINK)
            if not link_el:
                return None

            href = await link_el.get_attribute("href")
            if not href:
                return None

            source_url = href if href.startswith("http") else f"{OLX_BASE_URL}{href}"

            # Extract title from card
            title_el = await element.query_selector(OLXSelectors.ITEM_TITLE)
            title = await title_el.inner_text() if title_el else ""

            # Extract price from card
            price_el = await element.query_selector(OLXSelectors.ITEM_PRICE)
            price_text = await price_el.inner_text() if price_el else ""
            price = self._parse_price(price_text)

            # Extract location from card
            location_el = await element.query_selector(OLXSelectors.ITEM_LOCATION)
            location = await location_el.inner_text() if location_el else ""

            # Extract date from card
            date_el = await element.query_selector(OLXSelectors.ITEM_DATE)
            date_text = await date_el.inner_text() if date_el else ""

            # Navigate to detail page for full information
            detail_data = await self._extract_detail_page(page, source_url)

            return RawListing(
                title=title.strip(),
                price=price,
                currency="IDR",
                location=location.strip() if location else detail_data.get("location", ""),
                description=detail_data.get("description", ""),
                seller_name=detail_data.get("seller_name", ""),
                seller_contact=detail_data.get("seller_contact"),
                posting_date=self._parse_date(date_text),
                mileage=detail_data.get("mileage"),
                year_of_manufacture=detail_data.get("year_of_manufacture"),
                image_urls=detail_data.get("image_urls", [])[:20],
                source_url=source_url,
                source_platform=SourcePlatform.OLX,
                scrape_timestamp=datetime.utcnow(),
                seller_join_date=detail_data.get("seller_join_date"),
            )
        except Exception as e:
            logger.debug("Error extracting listing element: %s", str(e))
            return None

    async def _extract_detail_page(self, page: Page, url: str) -> dict:
        """Navigate to a listing detail page and extract full data.
        
        Reuses a single detail page tab to avoid resource exhaustion.
        """
        detail_data: dict = {
            "description": "",
            "seller_name": "",
            "seller_contact": None,
            "seller_join_date": None,
            "mileage": None,
            "year_of_manufacture": None,
            "image_urls": [],
            "location": "",
        }

        # Reuse or create a single detail page
        if not hasattr(self, '_detail_page') or self._detail_page is None or self._detail_page.is_closed():
            self._detail_page = await self._context.new_page() if self._context else None
        
        detail_page = self._detail_page
        if not detail_page:
            return detail_data

        try:
            await self._navigate_with_retry(detail_page, url, timeout_ms=self._timeout_ms)

            # Brief wait and scroll to trigger lazy-loaded images
            await asyncio.sleep(1)
            await detail_page.mouse.wheel(0, 300)
            await asyncio.sleep(0.5)

            # Check for captcha on detail page
            if await self._detect_captcha(detail_page):
                await self._handle_captcha(detail_page)

            # Extract description
            # First try clicking "Selengkapnya" button to open full description modal
            try:
                expand_btn = await detail_page.query_selector(OLXSelectors.DESCRIPTION_EXPAND)
                if expand_btn and await expand_btn.is_visible():
                    await expand_btn.click()
                    await asyncio.sleep(1)

                    # Read from the modal that appears after clicking
                    modal_desc = await detail_page.query_selector(OLXSelectors.DESCRIPTION_MODAL)
                    if modal_desc:
                        detail_data["description"] = (await modal_desc.inner_text()).strip()
            except Exception:
                pass

            # Fallback: if modal didn't work, get the partial description
            if not detail_data["description"]:
                desc_el = await detail_page.query_selector(OLXSelectors.DESCRIPTION)
                if desc_el:
                    detail_data["description"] = (await desc_el.inner_text()).strip()

            # Extract seller name and navigate to seller profile for join date
            seller_el = await detail_page.query_selector(OLXSelectors.SELLER_PROFILE_LINK)
            if seller_el:
                detail_data["seller_name"] = (await seller_el.inner_text()).strip()

                # Navigate to seller profile to get join date
                seller_join_date = await self._extract_seller_join_date(seller_el)
                if seller_join_date:
                    detail_data["seller_join_date"] = seller_join_date

            # Extract seller contact (phone number if visible)
            phone_el = await detail_page.query_selector(OLXSelectors.PHONE_BUTTON)
            if phone_el:
                phone_text = await phone_el.get_attribute("href")
                if phone_text and phone_text.startswith("tel:"):
                    detail_data["seller_contact"] = phone_text[4:]

            # Extract images - skip for now (gallery uses lazy-loaded carousel)
            # image_urls are stored as empty list; can be implemented later
            detail_data["image_urls"] = []

            # Extract vehicle details (mileage, year)
            detail_items = await detail_page.query_selector_all(OLXSelectors.VEHICLE_DETAILS)
            for item in detail_items:
                text = await item.inner_text()
                text_lower = text.lower()
                if "kilometer" in text_lower or "km" in text_lower:
                    detail_data["mileage"] = self._parse_number(text)
                elif "tahun" in text_lower or "year" in text_lower:
                    detail_data["year_of_manufacture"] = self._parse_number(text)

            # Extract location from detail if not already available
            loc_el = await detail_page.query_selector(OLXSelectors.DETAIL_LOCATION)
            if loc_el:
                detail_data["location"] = (await loc_el.inner_text()).strip()

        except Exception as e:
            logger.debug("Error extracting detail page %s: %s", url, str(e))

        return detail_data

    async def _extract_seller_join_date(self, seller_el: ElementHandle) -> datetime | None:
        """Click on the seller profile link and extract the join date.

        Navigates to the seller's profile page, looks for the member-since
        or join date element, and returns it as a datetime.
        Reuses a single seller page tab.
        """
        try:
            # Get the seller profile URL
            href = await seller_el.get_attribute("href")
            if not href:
                return None

            seller_url = href if href.startswith("http") else f"{OLX_BASE_URL}{href}"

            # Reuse or create a single seller page
            if not hasattr(self, '_seller_page') or self._seller_page is None or self._seller_page.is_closed():
                self._seller_page = await self._context.new_page() if self._context else None

            seller_page = self._seller_page
            if not seller_page:
                return None

            await self._navigate_with_retry(seller_page, seller_url, timeout_ms=self._timeout_ms)

            # Look for member since / join date element
            for selector in OLXSelectors.SELLER_JOIN_DATE:
                el = await seller_page.query_selector(selector)
                if el:
                    text = (await el.inner_text()).strip()
                    parsed = self._parse_join_date(text)
                    if parsed:
                        return parsed

            # Fallback: search for date pattern in the profile card area
            profile_area = await seller_page.query_selector(OLXSelectors.SELLER_PROFILE_AREA)
            if profile_area:
                profile_text = await profile_area.inner_text()
                parsed = self._parse_join_date(profile_text)
                if parsed:
                    return parsed

            return None
        except Exception as e:
            logger.debug("Error extracting seller join date: %s", str(e))
            return None

    async def _scroll_to_load_listings(self, page: Page) -> None:
        """Scroll down the page to bypass ads and trigger lazy-loading of listings."""
        scroll_distance = 500  # pixels per scroll step
        max_scrolls = 10
        for _ in range(max_scrolls):
            await page.mouse.wheel(0, scroll_distance)
            await asyncio.sleep(0.5)

            # Stop scrolling once we find at least one listing card
            el = await page.query_selector(OLXSelectors.LISTING_CARD)
            if el:
                # Scroll a bit more to load additional cards
                await page.mouse.wheel(0, scroll_distance * 2)
                await asyncio.sleep(1)
                break

    async def _navigate_next_page(self, page: Page, current_page: int) -> bool:
        """Navigate to the next page. Returns True if next page exists."""
        try:
            next_btn = await page.query_selector(OLXSelectors.NEXT_PAGE)

            if not next_btn:
                return False

            # Check if the next button is disabled
            is_disabled = await next_btn.get_attribute("disabled")
            if is_disabled is not None:
                return False

            await next_btn.click()
            await page.wait_for_load_state("domcontentloaded")
            return True
        except Exception as e:
            logger.debug("Failed to navigate to next page: %s", str(e))
            return False

    async def _detect_captcha(self, page: Page) -> bool:
        """Detect if the page is showing a captcha or Cloudflare challenge."""
        try:
            for selector in OLXSelectors.CAPTCHA_INDICATORS:
                el = await page.query_selector(selector)
                if el:
                    return True

            # Check for Cloudflare-specific challenge page (more specific checks)
            title = await page.title()
            if "just a moment" in title.lower():
                return True

            # Check for the OLX-specific "not connected" block page
            content = await page.content()
            content_lower = content.lower()
            block_phrases = [
                "verify you are human",
                "jaringan anda tidak terkoneksi",
                "checking your browser",
                "please wait while we verify",
            ]
            for phrase in block_phrases:
                if phrase in content_lower:
                    return True

            return False
        except Exception:
            return False

    async def _handle_captcha(self, page: Page) -> None:
        """Handle captcha/Cloudflare challenge.

        In headed mode: waits for the user to manually solve it.
        In headless mode: pauses and retries (reload), aborts after max retries.
        """
        headless = self.config.get("browser.headless", True)

        for attempt in range(self._max_captcha_retries):
            if not headless:
                logger.warning(
                    "[%s] Captcha/block detected. Please solve it manually in the browser. "
                    "Waiting up to 120s for resolution (attempt %d/%d)...",
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    attempt + 1,
                    self._max_captcha_retries,
                )
                # Wait up to 120s for the user to solve the captcha
                # Check every 3 seconds if it's resolved
                for _ in range(40):
                    await asyncio.sleep(3)
                    if not await self._detect_captcha(page):
                        logger.info("Captcha resolved manually.")
                        return
            else:
                logger.warning(
                    "[%s] Captcha/block detected (attempt %d/%d). Pausing for %ds...",
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    attempt + 1,
                    self._max_captcha_retries,
                    int(self._captcha_pause),
                )
                await asyncio.sleep(self._captcha_pause)

                # Reload the page
                try:
                    await page.reload(timeout=self._timeout_ms)
                except Exception:
                    pass

                if not await self._detect_captcha(page):
                    logger.info("Captcha resolved after attempt %d", attempt + 1)
                    return

        raise CaptchaDetectedError(
            f"Captcha not resolved after {self._max_captcha_retries} attempts"
        )

    @staticmethod
    def _parse_join_date(text: str) -> datetime | None:
        """Parse join date from seller profile text.

        Handles formats like:
        - "Member since Jan 2020"
        - "Bergabung sejak Jan 2020"
        - "Member since 15 Jan 2020"
        """
        if not text:
            return None

        # Remove common prefixes
        cleaned = text.lower()
        for prefix in ("member since", "bergabung sejak", "bergabung"):
            if prefix in cleaned:
                cleaned = cleaned.split(prefix)[-1].strip()
                break

        # Try common date formats
        formats = [
            "%b %Y",         # "Jan 2020"
            "%B %Y",         # "January 2020"
            "%d %b %Y",     # "15 Jan 2020"
            "%d %B %Y",     # "15 January 2020"
            "%d/%m/%Y",     # "15/01/2020"
            "%Y-%m-%d",     # "2020-01-15"
            "%d %b, %Y",   # "15 Jan, 2020"
        ]

        for fmt in formats:
            try:
                return datetime.strptime(cleaned, fmt)
            except ValueError:
                continue

        # Try extracting just month and year with regex
        month_year_match = re.search(
            r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+(\d{4})",
            cleaned,
        )
        if month_year_match:
            try:
                return datetime.strptime(
                    f"{month_year_match.group(1)[:3]} {month_year_match.group(2)}",
                    "%b %Y",
                )
            except ValueError:
                pass

        return None

    @staticmethod
    def _parse_price(price_text: str) -> float:
        """Parse price string (e.g., 'Rp 150.000.000') to float."""
        if not price_text:
            return 0.0

        # Remove currency prefix and whitespace
        cleaned = price_text.replace("Rp", "").replace("IDR", "").strip()
        # Remove thousand separators (. in Indonesian format)
        cleaned = cleaned.replace(".", "").replace(",", "")
        # Remove any remaining non-digit characters
        digits = "".join(c for c in cleaned if c.isdigit())

        try:
            return float(digits) if digits else 0.0
        except ValueError:
            return 0.0

    @staticmethod
    def _parse_date(date_text: str) -> datetime | None:
        """Parse date string from OLX listing."""
        if not date_text:
            return None

        # OLX uses various date formats, try common ones
        date_text = date_text.strip()
        formats = [
            "%d %b %Y",
            "%d %B %Y",
            "%d/%m/%Y",
            "%Y-%m-%d",
        ]

        for fmt in formats:
            try:
                return datetime.strptime(date_text, fmt)
            except ValueError:
                continue

        return None

    @staticmethod
    def _parse_number(text: str) -> int | None:
        """Extract a number from text."""
        digits = "".join(c for c in text if c.isdigit())
        try:
            return int(digits) if digits else None
        except ValueError:
            return None
