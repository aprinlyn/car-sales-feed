"""Scoring parameters for trust evaluation of car listings."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from models.listing import ParameterResult, RawListing


@dataclass
class ScoringContext:
    """Context data provided to scoring parameters."""

    market_average_price: float | None = None  # Average price for same model/year
    current_year: int = 2026


class ScoringParameter(ABC):
    """Abstract base class for scoring parameters."""

    name: str
    weight: float  # 0.0–1.0, all weights sum to 1.0

    def __init__(self, name: str, weight: float):
        self.name = name
        self.weight = weight

    @abstractmethod
    def evaluate(self, listing: RawListing, context: ScoringContext) -> ParameterResult:
        """Evaluate this parameter. Returns score 0.0–1.0 and computable flag."""
        ...


class PriceDeviationParameter(ScoringParameter):
    """Evaluates price deviation from market average.

    Listings deviating more than 30% below market average receive reduced points.
    If market data is unavailable, returns non-computable.
    """

    def __init__(self, weight: float = 0.20):
        super().__init__("price_deviation", weight)

    def evaluate(self, listing: RawListing, context: ScoringContext) -> ParameterResult:
        if context.market_average_price is None or context.market_average_price <= 0:
            return ParameterResult(
                score=0.5,
                computable=False,
                reason="Market average price unavailable",
            )

        deviation = (listing.price - context.market_average_price) / context.market_average_price

        if deviation < -0.30:
            # More than 30% below market — suspicious
            # Scale: -30% = 0.5, -60% = 0.0
            score = max(0.0, 0.5 + (deviation + 0.30) * (0.5 / 0.30))
            return ParameterResult(
                score=score,
                computable=True,
                reason=f"Price {deviation*100:.0f}% below market average (suspicious)",
            )
        elif deviation < 0:
            # Slightly below market — normal
            score = 0.5 + (0.5 * (1.0 + deviation / 0.30))
            return ParameterResult(
                score=min(1.0, score),
                computable=True,
                reason=f"Price {abs(deviation)*100:.0f}% below market average",
            )
        else:
            # At or above market — normal/good
            return ParameterResult(
                score=1.0,
                computable=True,
                reason=f"Price at or above market average (+{deviation*100:.0f}%)",
            )


class ImageCountParameter(ScoringParameter):
    """Evaluates the number of images provided.

    0 images = 0.0; 5+ images = 1.0; linear interpolation between.
    """

    def __init__(self, weight: float = 0.10):
        super().__init__("image_count", weight)

    def evaluate(self, listing: RawListing, context: ScoringContext) -> ParameterResult:
        count = len(listing.image_urls)
        score = min(1.0, count / 5.0)

        return ParameterResult(
            score=score,
            computable=True,
            reason=f"{count} images provided (5+ = max score)",
        )


class DescriptionLengthParameter(ScoringParameter):
    """Evaluates description completeness.

    Fewer than 50 characters = 0.0; 200+ characters = 1.0; linear between.
    """

    def __init__(self, weight: float = 0.10):
        super().__init__("description_length", weight)

    def evaluate(self, listing: RawListing, context: ScoringContext) -> ParameterResult:
        length = len(listing.description)

        if length >= 200:
            score = 1.0
        elif length < 50:
            score = 0.0
        else:
            score = (length - 50) / (200 - 50)

        return ParameterResult(
            score=score,
            computable=True,
            reason=f"Description length: {length} chars",
        )


class SellerAgeParameter(ScoringParameter):
    """Evaluates seller account age and verification status.

    Accounts younger than 30 days with no verification receive minimum points.
    Uses seller_join_date or seller_account_age_days.
    """

    def __init__(self, weight: float = 0.15):
        super().__init__("seller_age", weight)

    def evaluate(self, listing: RawListing, context: ScoringContext) -> ParameterResult:
        # Try to determine account age
        account_age_days: int | None = None

        if listing.seller_join_date is not None:
            delta = datetime.utcnow() - listing.seller_join_date
            account_age_days = delta.days
        elif listing.seller_account_age_days is not None:
            account_age_days = listing.seller_account_age_days

        if account_age_days is None:
            return ParameterResult(
                score=0.5,
                computable=False,
                reason="Seller account age unknown",
            )

        is_verified = listing.seller_verification_status or False

        if account_age_days < 30 and not is_verified:
            score = 0.0
            reason = f"Account {account_age_days} days old, not verified"
        elif account_age_days < 30 and is_verified:
            score = 0.5
            reason = f"Account {account_age_days} days old but verified"
        elif account_age_days < 90:
            score = 0.5 + (account_age_days - 30) / (90 - 30) * 0.3
            reason = f"Account {account_age_days} days old"
        else:
            # 90+ days
            score = 0.8 if not is_verified else 1.0
            reason = f"Account {account_age_days} days old, {'verified' if is_verified else 'not verified'}"

        return ParameterResult(score=score, computable=True, reason=reason)


class SellerListingHistoryParameter(ScoringParameter):
    """Evaluates seller listing volume.

    More than 10 simultaneous active listings from a single seller reduces points.
    """

    def __init__(self, weight: float = 0.10):
        super().__init__("seller_listing_history", weight)

    def evaluate(self, listing: RawListing, context: ScoringContext) -> ParameterResult:
        active_count = listing.seller_active_listings_count

        if active_count is None:
            return ParameterResult(
                score=0.5,
                computable=False,
                reason="Seller active listing count unknown",
            )

        if active_count > 10:
            # Reduce score proportionally: 11 = 0.8, 20+ = 0.2
            score = max(0.2, 1.0 - (active_count - 10) * 0.08)
            reason = f"Seller has {active_count} active listings (high volume)"
        else:
            score = 1.0
            reason = f"Seller has {active_count} active listings (normal)"

        return ParameterResult(score=score, computable=True, reason=reason)


class ContactInfoParameter(ScoringParameter):
    """Evaluates presence of contact information.

    At least one phone number or messaging identifier present = 1.0; absent = 0.0.
    """

    def __init__(self, weight: float = 0.10):
        super().__init__("contact_info", weight)

    def evaluate(self, listing: RawListing, context: ScoringContext) -> ParameterResult:
        has_contact = listing.seller_contact is not None and listing.seller_contact.strip()

        if has_contact:
            return ParameterResult(
                score=1.0,
                computable=True,
                reason="Contact information present",
            )
        else:
            return ParameterResult(
                score=0.0,
                computable=True,
                reason="No contact information provided",
            )


class MileageConsistencyParameter(ScoringParameter):
    """Evaluates consistency between listed mileage and year of manufacture.

    Average exceeding 30,000 km per year reduces points.
    """

    def __init__(self, weight: float = 0.10):
        super().__init__("mileage_consistency", weight)

    def evaluate(self, listing: RawListing, context: ScoringContext) -> ParameterResult:
        if listing.mileage is None or listing.year_of_manufacture is None:
            return ParameterResult(
                score=0.5,
                computable=False,
                reason="Mileage or year of manufacture unavailable",
            )

        age_years = context.current_year - listing.year_of_manufacture
        if age_years <= 0:
            return ParameterResult(
                score=1.0,
                computable=True,
                reason="Brand new car (0 years old)",
            )

        avg_km_per_year = listing.mileage / age_years

        if avg_km_per_year > 30000:
            # Exceeds threshold — reduce score
            # 30k = 0.5, 60k+ = 0.0
            score = max(0.0, 0.5 - (avg_km_per_year - 30000) / 60000)
            reason = f"High mileage: {avg_km_per_year:.0f} km/year average"
        else:
            # Normal mileage
            score = 0.5 + (1.0 - avg_km_per_year / 30000) * 0.5
            reason = f"Normal mileage: {avg_km_per_year:.0f} km/year average"

        return ParameterResult(score=score, computable=True, reason=reason)


class LocationSpecificityParameter(ScoringParameter):
    """Evaluates location specificity.

    City-level or more specific location = 1.0;
    Province-only or missing location = 0.0.
    """

    def __init__(self, weight: float = 0.15):
        super().__init__("location_specificity", weight)

    # Common Indonesian province-only names (not city-level)
    PROVINCE_ONLY_KEYWORDS = [
        "jawa barat", "jawa tengah", "jawa timur",
        "sumatera utara", "sumatera barat", "sumatera selatan",
        "kalimantan", "sulawesi", "bali", "papua",
        "nusa tenggara", "maluku", "lampung", "riau",
        "jambi", "bengkulu", "aceh", "gorontalo",
    ]

    def evaluate(self, listing: RawListing, context: ScoringContext) -> ParameterResult:
        location = listing.location.strip() if listing.location else ""

        if not location:
            return ParameterResult(
                score=0.0,
                computable=True,
                reason="No location provided",
            )

        location_lower = location.lower()

        # Check if it's just a province name
        is_province_only = any(
            prov in location_lower for prov in self.PROVINCE_ONLY_KEYWORDS
        )

        # If it's longer or contains a comma/district, it's likely city-level
        has_specificity = (
            "," in location
            or len(location.split()) >= 2
            or not is_province_only
        )

        if has_specificity:
            return ParameterResult(
                score=1.0,
                computable=True,
                reason=f"Specific location: {location}",
            )
        else:
            return ParameterResult(
                score=0.0,
                computable=True,
                reason=f"Province-level only: {location}",
            )
