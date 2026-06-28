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
from scrapers.selectors import OLXSelectors

logger = logging.getLogger(__name__)

OLX_BASE_URL = "https://www.olx.co.id"
OLX_CARS_URL = f"{OLX_BASE_URL}/mobil-bekas_c198"


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
                page, OLX_CARS_URL, timeout_ms=self._timeout_ms
            )

            # Check for captcha on initial load
            if await self._detect_captcha(page):
                await self._handle_captcha(page)

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

                    # Check for captcha after navigation
                    if await self._detect_captcha(page):
                        await self._handle_captcha(page)

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

        # Wait for listing elements to appear
        try:
            await page.wait_for_selector(OLXSelectors.LISTING_CARD, timeout=10000)
        except Exception:
            logger.warning("No listing elements found on page")
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
        """Navigate to a listing detail page and extract full data."""
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

        detail_page = await self._context.new_page() if self._context else None
        if not detail_page:
            return detail_data

        try:
            await self._navigate_with_retry(detail_page, url, timeout_ms=self._timeout_ms)

            # Check for captcha on detail page
            if await self._detect_captcha(detail_page):
                await self._handle_captcha(detail_page)

            # Extract description
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

            # Extract images
            image_elements = await detail_page.query_selector_all(OLXSelectors.GALLERY_IMAGES)
            image_urls = []
            for img in image_elements[:20]:
                src = await img.get_attribute("src")
                if src and not src.startswith("data:"):
                    image_urls.append(src)
            detail_data["image_urls"] = image_urls

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
        finally:
            await detail_page.close()

        return detail_data

    async def _extract_seller_join_date(self, seller_el: ElementHandle) -> datetime | None:
        """Click on the seller profile link and extract the join date.

        Navigates to the seller's profile page, looks for the member-since
        or join date element, and returns it as a datetime.
        """
        seller_page = None
        try:
            # Get the seller profile URL
            href = await seller_el.get_attribute("href")
            if not href:
                return None

            seller_url = href if href.startswith("http") else f"{OLX_BASE_URL}{href}"

            # Open seller profile in a new page
            seller_page = await self._context.new_page() if self._context else None
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
        finally:
            if seller_page:
                await seller_page.close()

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
        """Detect if the page is showing a captcha or block."""
        try:
            for selector in OLXSelectors.CAPTCHA_INDICATORS:
                el = await page.query_selector(selector)
                if el:
                    return True

            # Check page content for block messages
            content = await page.content()
            block_indicators = [
                "blocked",
                "access denied",
                "verify you are human",
                "unusual traffic",
            ]
            content_lower = content.lower()
            for indicator in block_indicators:
                if indicator in content_lower:
                    return True

            return False
        except Exception:
            return False

    async def _handle_captcha(self, page: Page) -> None:
        """Handle captcha detection with pause and retry.

        Pauses for configurable delay, retries up to 3 times.
        Raises CaptchaDetectedError if unresolved.
        """
        for attempt in range(self._max_captcha_retries):
            logger.warning(
                "[%s] Captcha/block detected on OLX (attempt %d/%d). Pausing for %ds...",
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
