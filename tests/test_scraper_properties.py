"""Property-based tests for scraper listing validation.

# Feature: car-sales-feed, Property 1: Incomplete listing rejection
"""

from __future__ import annotations

from datetime import datetime

from hypothesis import given, settings, assume, HealthCheck
from hypothesis import strategies as st

from models.listing import RawListing, SourcePlatform
from scrapers.base import BaseScraper
from config.manager import ConfigManager

import tempfile
import os


class ConcreteScraper(BaseScraper):
    """Concrete implementation for testing the base class validation."""

    async def scrape(self) -> list[RawListing]:
        return []


def create_scraper() -> ConcreteScraper:
    """Create a scraper with minimal config for testing."""
    yaml_content = "database:\n  url: 'sqlite:///:memory:'\nbrowser:\n  profile_dir: './test_profile'\n"
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    f.write(yaml_content)
    f.close()
    try:
        config = ConfigManager(yaml_path=f.name)
        return ConcreteScraper(config)
    finally:
        os.unlink(f.name)


def make_raw_listing(
    title: str = "Test Car",
    price: float = 100_000_000.0,
) -> RawListing:
    """Create a RawListing with specified title and price."""
    return RawListing(
        title=title,
        price=price,
        currency="IDR",
        location="Jakarta",
        description="A car",
        seller_name="Seller",
        source_url="https://olx.co.id/item/test",
        source_platform=SourcePlatform.OLX,
        scrape_timestamp=datetime.utcnow(),
    )


class TestIncompleteListingRejection:
    """Property 1: For any RawListing where the title is missing, empty, or
    whitespace-only, OR the price is missing, null, or non-positive, the
    validation function SHALL reject the listing."""

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        title=st.one_of(
            st.just(""),
            st.just("   "),
            st.just("\t\n"),
            st.just("\r\n  \t"),
            st.text(
                min_size=0,
                max_size=10,
                alphabet=st.sampled_from(" \t\n\r\x0b\x0c"),
            ),
        )
    )
    def test_empty_or_whitespace_title_rejected(self, title: str):
        """Listings with empty/whitespace-only titles are rejected."""
        scraper = create_scraper()
        listing = make_raw_listing(title=title, price=100_000_000.0)
        assert scraper._validate_listing(listing) is False

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        price=st.one_of(
            st.just(0.0),
            st.just(-1.0),
            st.floats(max_value=0.0, allow_nan=False, allow_infinity=False),
        )
    )
    def test_non_positive_price_rejected(self, price: float):
        """Listings with zero or negative prices are rejected."""
        scraper = create_scraper()
        listing = make_raw_listing(title="Valid Title", price=price)
        assert scraper._validate_listing(listing) is False

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        title=st.text(min_size=1, max_size=50, alphabet=st.characters(whitelist_categories=("L", "N"))),
        price=st.floats(min_value=0.01, max_value=1e12, allow_nan=False, allow_infinity=False),
    )
    def test_valid_listing_accepted(self, title: str, price: float):
        """Listings with non-empty title and positive price are accepted."""
        assume(title.strip())  # Ensure at least one non-whitespace char
        scraper = create_scraper()
        listing = make_raw_listing(title=title, price=price)
        assert scraper._validate_listing(listing) is True
