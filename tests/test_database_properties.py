"""Property-based tests for DatabaseManager.

# Feature: car-sales-feed, Property 6: Deduplication invariant
"""

from __future__ import annotations

from datetime import datetime, timedelta

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from config.manager import ConfigManager
from models.listing import RawListing, ScoredListing, SourcePlatform, TrustLevel
from persistence.database import DatabaseManager
from persistence.models import StoredListing

import tempfile
import os


def create_in_memory_db() -> DatabaseManager:
    """Create a DatabaseManager backed by in-memory SQLite."""
    yaml_content = "database:\n  url: 'sqlite:///:memory:'\nbrowser:\n  profile_dir: './test_profile'\n"
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    f.write(yaml_content)
    f.close()
    try:
        config = ConfigManager(yaml_path=f.name)
        return DatabaseManager(config)
    finally:
        os.unlink(f.name)


def make_scored_listing(
    title: str = "Test Car",
    price: float = 100_000_000.0,
    seller_name: str = "Seller A",
    source_platform: SourcePlatform = SourcePlatform.OLX,
    score: int = 75,
    trust_level: TrustLevel = TrustLevel.SAFE,
    description: str = "A nice car",
    location: str = "Jakarta",
    scrape_timestamp: datetime | None = None,
) -> ScoredListing:
    """Helper to create a ScoredListing for testing."""
    if scrape_timestamp is None:
        scrape_timestamp = datetime.utcnow()

    raw = RawListing(
        title=title,
        price=price,
        currency="IDR",
        location=location,
        description=description,
        seller_name=seller_name,
        source_url=f"https://olx.co.id/item/{title.replace(' ', '-').lower()}",
        source_platform=source_platform,
        scrape_timestamp=scrape_timestamp,
    )

    return ScoredListing(
        raw=raw,
        score=score,
        trust_level=trust_level,
        partially_scored=False,
        parameter_scores={"image_count": 0.8, "description_length": 0.7},
        scoring_timestamp=scrape_timestamp,
    )


class TestDeduplicationInvariant:
    """Property 6: For any sequence of RawListings stored in the database,
    the database SHALL contain at most one record per unique combination of
    (title, price, seller_name, source_platform). Storing a duplicate SHALL
    update the existing record rather than creating a new entry."""

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        num_duplicates=st.integers(min_value=2, max_value=10),
        title=st.text(min_size=1, max_size=30, alphabet=st.characters(whitelist_categories=("L", "N", "Zs"))),
        price=st.floats(min_value=1.0, max_value=1e12, allow_nan=False, allow_infinity=False),
        seller_name=st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N"))),
    )
    def test_no_duplicates_after_multiple_inserts(
        self, num_duplicates: int, title: str, price: float, seller_name: str
    ):
        """Inserting the same listing multiple times results in exactly one DB record."""
        db = create_in_memory_db()

        for i in range(num_duplicates):
            listing = make_scored_listing(
                title=title,
                price=price,
                seller_name=seller_name,
                source_platform=SourcePlatform.OLX,
                description=f"Description version {i}",
                scrape_timestamp=datetime.utcnow() + timedelta(hours=i),
            )
            db.store_listing(listing)

        # Query all listings — should be exactly 1
        with db.SessionFactory() as session:
            count = session.query(StoredListing).count()
            assert count == 1, f"Expected 1 listing, got {count} after {num_duplicates} inserts"

            # The record should have the latest description
            stored = session.query(StoredListing).first()
            assert stored is not None
            assert stored.description == f"Description version {num_duplicates - 1}"

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        num_unique=st.integers(min_value=2, max_value=8),
    )
    def test_distinct_listings_stored_separately(self, num_unique: int):
        """Listings with different dedup keys are stored as separate records."""
        db = create_in_memory_db()

        for i in range(num_unique):
            listing = make_scored_listing(
                title=f"Car {i}",
                price=float(100_000_000 + i * 1_000_000),
                seller_name=f"Seller {i}",
                source_platform=SourcePlatform.OLX,
            )
            db.store_listing(listing)

        with db.SessionFactory() as session:
            count = session.query(StoredListing).count()
            assert count == num_unique, f"Expected {num_unique} listings, got {count}"

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        platform=st.sampled_from([SourcePlatform.OLX, SourcePlatform.FACEBOOK]),
    )
    def test_same_listing_different_platform_not_deduplicated(self, platform: SourcePlatform):
        """Same title/price/seller on different platforms = 2 records."""
        db = create_in_memory_db()

        listing_olx = make_scored_listing(
            title="Honda Civic 2020",
            price=250_000_000.0,
            seller_name="Budi",
            source_platform=SourcePlatform.OLX,
        )
        listing_fb = make_scored_listing(
            title="Honda Civic 2020",
            price=250_000_000.0,
            seller_name="Budi",
            source_platform=SourcePlatform.FACEBOOK,
        )

        db.store_listing(listing_olx)
        db.store_listing(listing_fb)

        with db.SessionFactory() as session:
            count = session.query(StoredListing).count()
            assert count == 2
