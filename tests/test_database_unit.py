"""Unit tests for DatabaseManager.

Tests:
- Dead-letter file written after max retries exhausted
- get_unpublished returns correct ordering and filtering
- mark_published prevents re-selection
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from config.manager import ConfigManager
from models.listing import RawListing, ScoredListing, SourcePlatform, TrustLevel
from persistence.database import DatabaseManager
from persistence.models import StoredListing, Publication


def create_test_db(dead_letter_path: str | None = None) -> DatabaseManager:
    """Create an in-memory DatabaseManager for testing."""
    yaml_content = (
        "database:\n"
        "  url: 'sqlite:///:memory:'\n"
        "  retry_max: 3\n"
        f"  dead_letter_path: '{dead_letter_path or 'test_dead_letter.jsonl'}'\n"
        "browser:\n"
        "  profile_dir: './test_profile'\n"
    )
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    f.write(yaml_content)
    f.close()
    try:
        config = ConfigManager(yaml_path=f.name)
        return DatabaseManager(config)
    finally:
        os.unlink(f.name)


def make_scored_listing(
    title: str = "Honda Civic 2020",
    price: float = 250_000_000.0,
    seller_name: str = "Budi",
    source_platform: SourcePlatform = SourcePlatform.OLX,
    score: int = 75,
    trust_level: TrustLevel = TrustLevel.SAFE,
    scrape_timestamp: datetime | None = None,
) -> ScoredListing:
    """Helper to create a ScoredListing for testing."""
    if scrape_timestamp is None:
        scrape_timestamp = datetime.utcnow()

    raw = RawListing(
        title=title,
        price=price,
        currency="IDR",
        location="Jakarta",
        description="Well maintained car",
        seller_name=seller_name,
        source_url=f"https://olx.co.id/item/{title.replace(' ', '-').lower()}",
        source_platform=source_platform,
        scrape_timestamp=scrape_timestamp,
        image_urls=["https://example.com/img1.jpg"],
    )

    return ScoredListing(
        raw=raw,
        score=score,
        trust_level=trust_level,
        partially_scored=False,
        parameter_scores={"image_count": 0.8},
        scoring_timestamp=scrape_timestamp,
    )


class TestDeadLetterFile:
    """Test that dead-letter file is written after max retries exhausted."""

    def test_dead_letter_written_on_exhausted_retries(self):
        """After all retries fail, listing data is written to dead-letter JSONL."""
        dead_letter_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        )
        dead_letter_file.close()
        dead_letter_path = dead_letter_file.name

        try:
            db = create_test_db(dead_letter_path=dead_letter_path)

            # Mock _do_store to always raise
            with patch.object(db, "_do_store", side_effect=Exception("DB connection lost")):
                listing = make_scored_listing(title="Failed Car")
                db.store_listing(listing)

            # Verify dead-letter file was written
            with open(dead_letter_path) as f:
                lines = f.readlines()

            assert len(lines) == 1
            entry = json.loads(lines[0])
            assert entry["listing"]["title"] == "Failed Car"
            assert "DB connection lost" in entry["error"]
            assert entry["listing"]["price"] == 250_000_000.0
        finally:
            os.unlink(dead_letter_path)

    def test_dead_letter_not_written_on_success(self):
        """No dead-letter entry when store succeeds."""
        dead_letter_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        )
        dead_letter_file.close()
        dead_letter_path = dead_letter_file.name

        try:
            db = create_test_db(dead_letter_path=dead_letter_path)
            listing = make_scored_listing(title="Good Car")
            db.store_listing(listing)

            # Dead-letter file should be empty
            with open(dead_letter_path) as f:
                content = f.read()
            assert content == ""
        finally:
            os.unlink(dead_letter_path)


class TestGetUnpublished:
    """Test get_unpublished returns correct ordering and filtering."""

    def test_returns_only_safe_listings(self):
        """Only listings with trust_level 'safe' are returned."""
        db = create_test_db()

        # Store listings with different trust levels
        safe = make_scored_listing(title="Safe Car", score=80, trust_level=TrustLevel.SAFE)
        suspicious = make_scored_listing(
            title="Suspicious Car", price=100_000_000.0, score=50, trust_level=TrustLevel.SUSPICIOUS
        )
        unsafe = make_scored_listing(
            title="Unsafe Car", price=50_000_000.0, score=20, trust_level=TrustLevel.UNSAFE
        )

        db.store_listing(safe)
        db.store_listing(suspicious)
        db.store_listing(unsafe)

        results = db.get_unpublished(platform="twitter", trust_level="safe")
        assert len(results) == 1
        assert results[0].title == "Safe Car"

    def test_ordered_by_scrape_timestamp_ascending(self):
        """Results are ordered oldest-first by scrape_timestamp."""
        db = create_test_db()

        now = datetime.utcnow()
        oldest = make_scored_listing(
            title="Oldest", price=100_000_000.0, scrape_timestamp=now - timedelta(hours=3)
        )
        middle = make_scored_listing(
            title="Middle", price=200_000_000.0, scrape_timestamp=now - timedelta(hours=1)
        )
        newest = make_scored_listing(
            title="Newest", price=300_000_000.0, scrape_timestamp=now
        )

        # Store out of order
        db.store_listing(newest)
        db.store_listing(oldest)
        db.store_listing(middle)

        results = db.get_unpublished(platform="twitter", trust_level="safe")
        assert len(results) == 3
        assert results[0].title == "Oldest"
        assert results[1].title == "Middle"
        assert results[2].title == "Newest"

    def test_respects_limit(self):
        """Limit parameter caps the number of results."""
        db = create_test_db()

        now = datetime.utcnow()
        for i in range(10):
            listing = make_scored_listing(
                title=f"Car {i}",
                price=float(100_000_000 + i * 1_000_000),
                scrape_timestamp=now + timedelta(minutes=i),
            )
            db.store_listing(listing)

        results = db.get_unpublished(platform="twitter", trust_level="safe", limit=5)
        assert len(results) == 5

    def test_excludes_already_published(self):
        """Listings already published to the platform are excluded."""
        db = create_test_db()

        listing1 = make_scored_listing(title="Published Car", price=100_000_000.0)
        listing2 = make_scored_listing(title="Unpublished Car", price=200_000_000.0)

        db.store_listing(listing1)
        db.store_listing(listing2)

        # Mark listing1 as published to twitter
        results = db.get_unpublished(platform="twitter", trust_level="safe")
        published_id = results[0].id  # Get the first one (Published Car, oldest)

        db.mark_published(
            listing_id=published_id,
            platform="twitter",
            published_at=datetime.utcnow(),
        )

        # Now query again — should only get the unpublished one
        results = db.get_unpublished(platform="twitter", trust_level="safe")
        assert len(results) == 1
        assert results[0].title == "Unpublished Car"


class TestMarkPublished:
    """Test mark_published prevents re-selection."""

    def test_published_listing_excluded_from_same_platform(self):
        """After marking published on twitter, listing is not returned for twitter."""
        db = create_test_db()

        listing = make_scored_listing(title="My Car")
        db.store_listing(listing)

        results = db.get_unpublished(platform="twitter", trust_level="safe")
        assert len(results) == 1

        db.mark_published(
            listing_id=results[0].id,
            platform="twitter",
            published_at=datetime.utcnow(),
        )

        results = db.get_unpublished(platform="twitter", trust_level="safe")
        assert len(results) == 0

    def test_published_on_one_platform_still_available_on_other(self):
        """Publishing to twitter doesn't affect threads selection."""
        db = create_test_db()

        listing = make_scored_listing(title="Cross Platform Car")
        db.store_listing(listing)

        results = db.get_unpublished(platform="twitter", trust_level="safe")
        listing_id = results[0].id

        db.mark_published(
            listing_id=listing_id,
            platform="twitter",
            published_at=datetime.utcnow(),
        )

        # Should still be available for threads
        results = db.get_unpublished(platform="threads", trust_level="safe")
        assert len(results) == 1
        assert results[0].id == listing_id

    def test_mark_published_stores_post_id(self):
        """Publication record stores the optional post_id."""
        db = create_test_db()

        listing = make_scored_listing(title="Posted Car")
        db.store_listing(listing)

        results = db.get_unpublished(platform="twitter", trust_level="safe")
        listing_id = results[0].id

        db.mark_published(
            listing_id=listing_id,
            platform="twitter",
            published_at=datetime.utcnow(),
            post_id="tweet_12345",
        )

        with db.SessionFactory() as session:
            pub = session.query(Publication).filter(Publication.listing_id == listing_id).first()
            assert pub is not None
            assert pub.post_id == "tweet_12345"
            assert pub.platform == "twitter"
