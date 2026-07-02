"""Property-based tests for publishers.

# Feature: car-sales-feed, Property 7: Publisher selection correctness
# Feature: car-sales-feed, Property 8: Twitter post format constraint
# Feature: car-sales-feed, Property 9: Threads post format constraint
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from hypothesis import given, settings, assume, HealthCheck
from hypothesis import strategies as st

from config.manager import ConfigManager
from models.listing import RawListing, ScoredListing, SourcePlatform, TrustLevel
from persistence.database import DatabaseManager
from persistence.models import StoredListing
from publishers.twitter import TwitterPublisher
from publishers.threads import ThreadsPublisher


def create_test_db() -> DatabaseManager:
    yaml_content = (
        "database:\n  url: 'sqlite:///:memory:'\n"
        "browser:\n  profile_dir: './test'\n"
    )
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    f.write(yaml_content)
    f.close()
    try:
        config = ConfigManager(yaml_path=f.name)
        return DatabaseManager(config)
    finally:
        os.unlink(f.name)


def create_config() -> ConfigManager:
    yaml_content = (
        "database:\n  url: 'sqlite:///:memory:'\n"
        "browser:\n  profile_dir: './test'\n"
    )
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    f.write(yaml_content)
    f.close()
    try:
        return ConfigManager(yaml_path=f.name)
    finally:
        os.unlink(f.name)


def make_scored_listing(
    title: str = "Honda Civic 2020",
    price: float = 250_000_000.0,
    seller_name: str = "Budi",
    trust_level: TrustLevel = TrustLevel.SAFE,
    scrape_timestamp: datetime | None = None,
) -> ScoredListing:
    if scrape_timestamp is None:
        scrape_timestamp = datetime.utcnow()
    raw = RawListing(
        title=title,
        price=price,
        currency="IDR",
        location="Jakarta Selatan",
        description="Well maintained",
        seller_name=seller_name,
        source_url="https://olx.co.id/item/test-car",
        source_platform=SourcePlatform.OLX,
        scrape_timestamp=scrape_timestamp,
    )
    return ScoredListing(
        raw=raw,
        score=75,
        trust_level=trust_level,
        partially_scored=False,
        parameter_scores={},
        scoring_timestamp=scrape_timestamp,
    )


def make_stored_listing(
    title: str = "Honda Civic 2020",
    price: float = 250_000_000.0,
    location: str = "Jakarta Selatan",
    source_url: str = "https://olx.co.id/item/test-car",
    trust_level: str = "safe",
) -> StoredListing:
    """Create a mock StoredListing object."""
    listing = MagicMock(spec=StoredListing)
    listing.title = title
    listing.price = price
    listing.location = location
    listing.source_url = source_url
    listing.trust_level = trust_level
    return listing


class TestPublisherSelection:
    """Property 7: Publisher selection returns only safe+unpublished+ordered."""

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(num_safe=st.integers(min_value=0, max_value=10))
    def test_only_safe_listings_selected(self, num_safe: int):
        """Only 'safe' listings are returned for publishing."""
        db = create_test_db()

        now = datetime.utcnow()
        for i in range(num_safe):
            db.store_listing(make_scored_listing(
                title=f"Safe {i}", price=float(100_000_000 + i),
                trust_level=TrustLevel.SAFE,
                scrape_timestamp=now + timedelta(minutes=i),
            ))
        # Add non-safe listings
        for i in range(3):
            db.store_listing(make_scored_listing(
                title=f"Unsafe {i}", price=float(50_000_000 + i),
                trust_level=TrustLevel.UNSAFE,
                scrape_timestamp=now + timedelta(minutes=i),
            ))

        results = db.get_unpublished(platform="twitter", trust_level="safe", limit=30)
        assert len(results) == num_safe
        for r in results:
            assert r.trust_level == "safe"

    def test_ordered_by_scrape_timestamp_ascending(self):
        """Results are oldest-first."""
        db = create_test_db()
        now = datetime.utcnow()

        db.store_listing(make_scored_listing(
            title="New", price=200_000_000.0,
            scrape_timestamp=now,
        ))
        db.store_listing(make_scored_listing(
            title="Old", price=100_000_000.0,
            scrape_timestamp=now - timedelta(hours=5),
        ))

        results = db.get_unpublished(platform="twitter", trust_level="safe", limit=30)
        assert results[0].title == "Old"
        assert results[1].title == "New"

    def test_excludes_already_published(self):
        """Published listings are not returned."""
        db = create_test_db()
        db.store_listing(make_scored_listing(title="Published", price=100_000_000.0))
        db.store_listing(make_scored_listing(title="Not Published", price=200_000_000.0))

        results = db.get_unpublished(platform="twitter", trust_level="safe")
        published_id = [r for r in results if r.title == "Published"][0].id
        db.mark_published(published_id, "twitter", datetime.utcnow())

        results = db.get_unpublished(platform="twitter", trust_level="safe")
        titles = [r.title for r in results]
        assert "Published" not in titles
        assert "Not Published" in titles


class TestTwitterPostFormat:
    """Property 8: Twitter format_post always ≤ 280 chars with required fields."""

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        title=st.text(min_size=1, max_size=200, alphabet=st.characters(whitelist_categories=("L", "N", "Zs"))),
        price=st.floats(min_value=1.0, max_value=1e12, allow_nan=False, allow_infinity=False),
        location=st.text(min_size=1, max_size=50, alphabet=st.characters(whitelist_categories=("L", "N", "Zs"))),
    )
    def test_post_always_within_280_chars(self, title: str, price: float, location: str):
        """Formatted post is always ≤ 280 characters."""
        config = create_config()
        db = create_test_db()
        publisher = TwitterPublisher(config, db)

        listing = make_stored_listing(
            title=title, price=price, location=location,
            source_url="https://olx.co.id/item/test",
        )
        post = publisher.format_post(listing)
        assert len(post) <= 280

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        title=st.text(min_size=1, max_size=200, alphabet=st.characters(whitelist_categories=("L", "N", "Zs"))),
        price=st.floats(min_value=1.0, max_value=1e12, allow_nan=False, allow_infinity=False),
    )
    def test_post_contains_price_and_url(self, title: str, price: float):
        """Post always contains the price and source URL."""
        config = create_config()
        db = create_test_db()
        publisher = TwitterPublisher(config, db)

        url = "https://olx.co.id/item/test"
        listing = make_stored_listing(title=title, price=price, source_url=url)
        post = publisher.format_post(listing)

        assert url in post
        assert "Rp" in post

    def test_truncated_title_has_ellipsis(self):
        """When title is truncated, ellipsis is added."""
        config = create_config()
        db = create_test_db()
        publisher = TwitterPublisher(config, db)

        long_title = "A" * 300
        listing = make_stored_listing(title=long_title, source_url="https://olx.co.id/item/x")
        post = publisher.format_post(listing)

        assert "..." in post
        assert len(post) <= 280


class TestThreadsPostFormat:
    """Property 9: Threads format_post always ≤ 500 chars with required fields."""

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        title=st.text(min_size=1, max_size=300, alphabet=st.characters(whitelist_categories=("L", "N", "Zs"))),
        price=st.floats(min_value=1.0, max_value=1e12, allow_nan=False, allow_infinity=False),
        location=st.text(min_size=1, max_size=50, alphabet=st.characters(whitelist_categories=("L", "N", "Zs"))),
    )
    def test_post_always_within_500_chars(self, title: str, price: float, location: str):
        """Formatted post is always ≤ 500 characters."""
        config = create_config()
        db = create_test_db()
        publisher = ThreadsPublisher(config, db)

        listing = make_stored_listing(
            title=title, price=price, location=location,
            source_url="https://olx.co.id/item/test",
        )
        post = publisher.format_post(listing)
        assert len(post) <= 500

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        title=st.text(min_size=1, max_size=300, alphabet=st.characters(whitelist_categories=("L", "N", "Zs"))),
    )
    def test_post_contains_url_and_trust_level(self, title: str):
        """Post always contains source URL and trust level."""
        config = create_config()
        db = create_test_db()
        publisher = ThreadsPublisher(config, db)

        url = "https://olx.co.id/item/test"
        listing = make_stored_listing(title=title, source_url=url, trust_level="safe")
        post = publisher.format_post(listing)

        assert url in post
        assert "SAFE" in post
