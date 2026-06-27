"""Database manager for storing and querying car listings."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, and_
from sqlalchemy.orm import Session, sessionmaker

from config.manager import ConfigManager
from models.listing import ScoredListing, TrustLevel
from persistence.models import Base, Publication, StoredListing

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Manages database operations for car listings with retry and dead-letter support."""

    def __init__(self, config: ConfigManager):
        self._config = config
        db_url = config.get("database.url", "sqlite:///car_sales_feed.db")
        self._retry_max = int(config.get("database.retry_max", 3))
        self._dead_letter_path = config.get("database.dead_letter_path", "dead_letter.jsonl")

        self.engine = create_engine(db_url, echo=False)
        self.SessionFactory = sessionmaker(bind=self.engine)

        # Create tables if they don't exist
        Base.metadata.create_all(self.engine)

    def store_listing(self, listing: ScoredListing) -> None:
        """Store or update a listing with deduplication.

        Deduplication key: (title, price, seller_name, source_platform).
        If a duplicate exists, update the existing record with changed fields.
        """
        attempt = 0
        last_error: Exception | None = None

        while attempt <= self._retry_max:
            try:
                self._do_store(listing)
                return
            except Exception as e:
                last_error = e
                attempt += 1
                logger.warning(
                    "Database write failed (attempt %d/%d): %s",
                    attempt,
                    self._retry_max,
                    str(e),
                )

        # All retries exhausted — write to dead-letter file
        logger.error(
            "All %d retry attempts exhausted for listing '%s'. Writing to dead-letter file.",
            self._retry_max,
            listing.raw.title,
        )
        self._write_dead_letter(listing, last_error)

    def _do_store(self, listing: ScoredListing) -> None:
        """Perform the actual store/update operation."""
        with self.SessionFactory() as session:
            # Check for existing duplicate
            existing = (
                session.query(StoredListing)
                .filter(
                    and_(
                        StoredListing.title == listing.raw.title,
                        StoredListing.price == listing.raw.price,
                        StoredListing.seller_name == listing.raw.seller_name,
                        StoredListing.source_platform == listing.raw.source_platform.value,
                    )
                )
                .first()
            )

            if existing:
                # Update existing record
                self._update_listing(existing, listing)
                existing.updated_at = datetime.utcnow()
            else:
                # Create new record
                stored = self._create_stored_listing(listing)
                session.add(stored)

            session.commit()

    def _create_stored_listing(self, listing: ScoredListing) -> StoredListing:
        """Create a new StoredListing from a ScoredListing."""
        now = datetime.utcnow()
        return StoredListing(
            id=str(uuid.uuid4()),
            title=listing.raw.title,
            price=listing.raw.price,
            currency=listing.raw.currency,
            location=listing.raw.location,
            description=listing.raw.description,
            seller_name=listing.raw.seller_name,
            seller_contact=listing.raw.seller_contact,
            posting_date=listing.raw.posting_date,
            mileage=listing.raw.mileage,
            year_of_manufacture=listing.raw.year_of_manufacture,
            image_urls=json.dumps(listing.raw.image_urls),
            source_url=listing.raw.source_url,
            source_platform=listing.raw.source_platform.value,
            scrape_timestamp=listing.raw.scrape_timestamp,
            score=listing.score,
            trust_level=listing.trust_level.value,
            partially_scored=listing.partially_scored,
            parameter_scores=json.dumps(listing.parameter_scores),
            scoring_timestamp=listing.scoring_timestamp,
            created_at=now,
            updated_at=now,
        )

    def _update_listing(self, existing: StoredListing, listing: ScoredListing) -> None:
        """Update an existing StoredListing with new data."""
        existing.currency = listing.raw.currency
        existing.location = listing.raw.location
        existing.description = listing.raw.description
        existing.seller_contact = listing.raw.seller_contact
        existing.posting_date = listing.raw.posting_date
        existing.mileage = listing.raw.mileage
        existing.year_of_manufacture = listing.raw.year_of_manufacture
        existing.image_urls = json.dumps(listing.raw.image_urls)
        existing.source_url = listing.raw.source_url
        existing.scrape_timestamp = listing.raw.scrape_timestamp
        existing.score = listing.score
        existing.trust_level = listing.trust_level.value
        existing.partially_scored = listing.partially_scored
        existing.parameter_scores = json.dumps(listing.parameter_scores)
        existing.scoring_timestamp = listing.scoring_timestamp

    def get_unpublished(
        self, platform: str, trust_level: str = "safe", limit: int = 30
    ) -> list[StoredListing]:
        """Query unpublished listings for a given platform and trust level.

        Returns listings ordered by scrape_timestamp ascending (oldest first),
        filtered to only those not yet published to the specified platform.
        """
        with self.SessionFactory() as session:
            # Subquery for listing IDs already published to this platform
            published_ids = (
                session.query(Publication.listing_id)
                .filter(Publication.platform == platform)
                .scalar_subquery()
            )

            results = (
                session.query(StoredListing)
                .filter(
                    and_(
                        StoredListing.trust_level == trust_level,
                        StoredListing.id.notin_(published_ids),
                    )
                )
                .order_by(StoredListing.scrape_timestamp.asc())
                .limit(limit)
                .all()
            )

            # Expunge results so they can be used outside the session
            for r in results:
                session.expunge(r)

            return results

    def mark_published(
        self, listing_id: str, platform: str, published_at: datetime, post_id: str | None = None
    ) -> None:
        """Record that a listing was published to a platform."""
        with self.SessionFactory() as session:
            publication = Publication(
                id=str(uuid.uuid4()),
                listing_id=listing_id,
                platform=platform,
                published_at=published_at,
                post_id=post_id,
            )
            session.add(publication)
            session.commit()

    def query_listings(
        self,
        trust_level: str | None = None,
        source_platform: str | None = None,
        price_min: float | None = None,
        price_max: float | None = None,
        scrape_date_from: datetime | None = None,
        scrape_date_to: datetime | None = None,
    ) -> list[StoredListing]:
        """Query listings by various filters.

        Supports filtering by Trust_Level, source platform, price range, and scrape date.
        """
        with self.SessionFactory() as session:
            query = session.query(StoredListing)

            if trust_level is not None:
                query = query.filter(StoredListing.trust_level == trust_level)
            if source_platform is not None:
                query = query.filter(StoredListing.source_platform == source_platform)
            if price_min is not None:
                query = query.filter(StoredListing.price >= price_min)
            if price_max is not None:
                query = query.filter(StoredListing.price <= price_max)
            if scrape_date_from is not None:
                query = query.filter(StoredListing.scrape_timestamp >= scrape_date_from)
            if scrape_date_to is not None:
                query = query.filter(StoredListing.scrape_timestamp <= scrape_date_to)

            results = query.order_by(StoredListing.scrape_timestamp.desc()).all()

            for r in results:
                session.expunge(r)

            return results

    def _write_dead_letter(self, listing: ScoredListing, error: Exception | None) -> None:
        """Write a failed listing to the dead-letter file for manual recovery."""
        dead_letter_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "error": str(error) if error else "unknown",
            "listing": {
                "title": listing.raw.title,
                "price": listing.raw.price,
                "currency": listing.raw.currency,
                "location": listing.raw.location,
                "description": listing.raw.description,
                "seller_name": listing.raw.seller_name,
                "seller_contact": listing.raw.seller_contact,
                "posting_date": listing.raw.posting_date.isoformat() if listing.raw.posting_date else None,
                "mileage": listing.raw.mileage,
                "year_of_manufacture": listing.raw.year_of_manufacture,
                "image_urls": listing.raw.image_urls,
                "source_url": listing.raw.source_url,
                "source_platform": listing.raw.source_platform.value,
                "scrape_timestamp": listing.raw.scrape_timestamp.isoformat(),
                "score": listing.score,
                "trust_level": listing.trust_level.value,
                "partially_scored": listing.partially_scored,
                "parameter_scores": listing.parameter_scores,
                "scoring_timestamp": listing.scoring_timestamp.isoformat(),
            },
        }

        path = Path(self._dead_letter_path)
        with open(path, "a") as f:
            f.write(json.dumps(dead_letter_entry) + "\n")

        logger.info("Listing '%s' written to dead-letter file: %s", listing.raw.title, path)
