"""SQLAlchemy models for persistent storage."""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""

    pass


class StoredListing(Base):
    """Persisted car listing with scoring data."""

    __tablename__ = "listings"

    id = Column(String, primary_key=True)
    title = Column(String, nullable=False)
    price = Column(Float, nullable=False)
    currency = Column(String, nullable=False, default="IDR")
    location = Column(String, nullable=False, default="")
    description = Column(Text, nullable=False, default="")
    seller_name = Column(String, nullable=False, default="")
    seller_contact = Column(String, nullable=True)
    seller_join_date = Column(DateTime, nullable=True)
    posting_date = Column(DateTime, nullable=True)
    mileage = Column(Integer, nullable=True)
    year_of_manufacture = Column(Integer, nullable=True)
    image_urls = Column(Text, nullable=False, default="[]")  # JSON-serialized list
    source_url = Column(String, nullable=False)
    source_platform = Column(String, nullable=False)
    scrape_timestamp = Column(DateTime, nullable=False)
    score = Column(Integer, nullable=False)
    trust_level = Column(String, nullable=False)
    partially_scored = Column(Boolean, nullable=False, default=False)
    parameter_scores = Column(Text, nullable=False, default="{}")  # JSON-serialized dict
    scoring_timestamp = Column(DateTime, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    publications = relationship("Publication", back_populates="listing", cascade="all, delete-orphan")

    def get_image_urls(self) -> list[str]:
        """Deserialize image_urls from JSON."""
        return json.loads(self.image_urls) if self.image_urls else []

    def set_image_urls(self, urls: list[str]) -> None:
        """Serialize image_urls to JSON."""
        self.image_urls = json.dumps(urls)

    def get_parameter_scores(self) -> dict[str, float]:
        """Deserialize parameter_scores from JSON."""
        return json.loads(self.parameter_scores) if self.parameter_scores else {}

    def set_parameter_scores(self, scores: dict[str, float]) -> None:
        """Serialize parameter_scores to JSON."""
        self.parameter_scores = json.dumps(scores)


class Publication(Base):
    """Tracks which listings have been published to which platforms."""

    __tablename__ = "publications"

    id = Column(String, primary_key=True)
    listing_id = Column(String, ForeignKey("listings.id"), nullable=False)
    platform = Column(String, nullable=False)  # "twitter" | "threads"
    published_at = Column(DateTime, nullable=False)
    post_id = Column(String, nullable=True)  # Platform-specific post identifier

    listing = relationship("StoredListing", back_populates="publications")
