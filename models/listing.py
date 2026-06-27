"""Data models for car sales feed listings."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class SourcePlatform(str, Enum):
    """Source platform for a car listing."""

    OLX = "olx"
    FACEBOOK = "facebook"


class TrustLevel(str, Enum):
    """Trust level assigned to a listing after scoring."""

    SAFE = "safe"
    SUSPICIOUS = "suspicious"
    UNSAFE = "unsafe"


@dataclass
class RawListing:
    """A listing as extracted by the scraper before scoring."""

    title: str
    price: float
    currency: str  # "IDR"
    location: str
    description: str
    seller_name: str
    source_url: str  # Direct link to the original listing on the source platform
    source_platform: SourcePlatform
    scrape_timestamp: datetime
    seller_contact: str | None = None
    posting_date: datetime | None = None
    mileage: int | None = None  # km
    year_of_manufacture: int | None = None
    image_urls: list[str] = field(default_factory=list)  # max 20
    # Facebook-specific fields
    seller_account_age_days: int | None = None
    seller_verification_status: bool | None = None
    seller_active_listings_count: int | None = None


@dataclass
class ScoredListing:
    """Extends RawListing with scoring results."""

    raw: RawListing
    score: int  # 0–100
    trust_level: TrustLevel
    partially_scored: bool
    parameter_scores: dict[str, float]  # parameter_name -> individual score (0.0–1.0)
    scoring_timestamp: datetime


@dataclass
class ParameterResult:
    """Result of evaluating a single scoring parameter."""

    score: float  # 0.0–1.0
    computable: bool
    reason: str  # Human-readable explanation


@dataclass
class ScrapingStats:
    """Statistics for a scraping session."""

    total_extracted: int = 0
    total_skipped: int = 0
    total_failed: int = 0
    start_time: datetime | None = None
    end_time: datetime | None = None


@dataclass
class PublishingStats:
    """Statistics for a publishing session."""

    total_published: int = 0
    total_failed: int = 0
    total_skipped: int = 0
    platform: str = ""
