"""Unit tests for individual scoring parameters.

Tests edge cases: 0 images, very short descriptions, very old sellers,
missing mileage, price deviation with known market data, and weight
redistribution when market data unavailable.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from models.listing import RawListing, SourcePlatform
from scoring.parameters import (
    ContactInfoParameter,
    DescriptionLengthParameter,
    ImageCountParameter,
    LocationSpecificityParameter,
    MileageConsistencyParameter,
    PriceDeviationParameter,
    ScoringContext,
    SellerAgeParameter,
    SellerListingHistoryParameter,
)
from scoring.scorer import TrustScorer
from config.manager import ConfigManager

import os
import tempfile


def make_listing(**kwargs) -> RawListing:
    """Create a RawListing with defaults, overridable by kwargs."""
    defaults = {
        "title": "Honda Civic 2020",
        "price": 250_000_000.0,
        "currency": "IDR",
        "location": "Jakarta Selatan",
        "description": "Mobil bagus, terawat, tangan pertama, full original, servis rutin dealer resmi.",
        "seller_name": "Budi",
        "source_url": "https://olx.co.id/item/test",
        "source_platform": SourcePlatform.OLX,
        "scrape_timestamp": datetime(2026, 6, 1, 12, 0),
        "seller_contact": "08123456789",
        "mileage": 45000,
        "year_of_manufacture": 2020,
        "image_urls": ["https://img.example.com/1.jpg"] * 5,
        "seller_join_date": datetime(2020, 1, 1),
    }
    defaults.update(kwargs)
    return RawListing(**defaults)


def default_context(market_price: float | None = 250_000_000.0) -> ScoringContext:
    return ScoringContext(market_average_price=market_price, current_year=2026)


def create_config() -> ConfigManager:
    yaml_content = "database:\n  url: 'sqlite:///:memory:'\nbrowser:\n  profile_dir: './test'\n"
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    f.write(yaml_content)
    f.close()
    try:
        return ConfigManager(yaml_path=f.name)
    finally:
        os.unlink(f.name)


class TestImageCountParameter:
    def test_zero_images_gives_zero_score(self):
        param = ImageCountParameter()
        listing = make_listing(image_urls=[])
        result = param.evaluate(listing, default_context())
        assert result.score == 0.0
        assert result.computable is True

    def test_five_images_gives_max_score(self):
        param = ImageCountParameter()
        listing = make_listing(image_urls=["img"] * 5)
        result = param.evaluate(listing, default_context())
        assert result.score == 1.0

    def test_two_images_gives_partial_score(self):
        param = ImageCountParameter()
        listing = make_listing(image_urls=["img"] * 2)
        result = param.evaluate(listing, default_context())
        assert result.score == pytest.approx(0.4)

    def test_more_than_five_still_max(self):
        param = ImageCountParameter()
        listing = make_listing(image_urls=["img"] * 10)
        result = param.evaluate(listing, default_context())
        assert result.score == 1.0


class TestDescriptionLengthParameter:
    def test_empty_description(self):
        param = DescriptionLengthParameter()
        listing = make_listing(description="")
        result = param.evaluate(listing, default_context())
        assert result.score == 0.0

    def test_short_description_under_50(self):
        param = DescriptionLengthParameter()
        listing = make_listing(description="Short")
        result = param.evaluate(listing, default_context())
        assert result.score == 0.0

    def test_long_description_200_plus(self):
        param = DescriptionLengthParameter()
        listing = make_listing(description="x" * 200)
        result = param.evaluate(listing, default_context())
        assert result.score == 1.0

    def test_mid_length_description(self):
        param = DescriptionLengthParameter()
        listing = make_listing(description="x" * 125)  # midpoint of 50-200
        result = param.evaluate(listing, default_context())
        assert result.score == pytest.approx(0.5)


class TestPriceDeviationParameter:
    def test_at_market_price(self):
        param = PriceDeviationParameter()
        listing = make_listing(price=250_000_000.0)
        result = param.evaluate(listing, default_context(market_price=250_000_000.0))
        assert result.score == 1.0
        assert result.computable is True

    def test_above_market_price(self):
        param = PriceDeviationParameter()
        listing = make_listing(price=300_000_000.0)
        result = param.evaluate(listing, default_context(market_price=250_000_000.0))
        assert result.score == 1.0

    def test_30_percent_below_market(self):
        param = PriceDeviationParameter()
        listing = make_listing(price=175_000_000.0)
        result = param.evaluate(listing, default_context(market_price=250_000_000.0))
        assert result.score == pytest.approx(0.5, abs=0.05)

    def test_60_percent_below_market(self):
        param = PriceDeviationParameter()
        listing = make_listing(price=100_000_000.0)
        result = param.evaluate(listing, default_context(market_price=250_000_000.0))
        assert result.score == pytest.approx(0.0, abs=0.05)

    def test_no_market_data_non_computable(self):
        param = PriceDeviationParameter()
        listing = make_listing(price=250_000_000.0)
        result = param.evaluate(listing, default_context(market_price=None))
        assert result.computable is False
        assert result.score == 0.5


class TestSellerAgeParameter:
    def test_new_unverified_account(self):
        param = SellerAgeParameter()
        listing = make_listing(
            seller_join_date=datetime.utcnow() - timedelta(days=10),
            seller_verification_status=False,
        )
        result = param.evaluate(listing, default_context())
        assert result.score == 0.0

    def test_old_verified_account(self):
        param = SellerAgeParameter()
        listing = make_listing(
            seller_join_date=datetime(2020, 1, 1),
            seller_verification_status=True,
        )
        result = param.evaluate(listing, default_context())
        assert result.score == 1.0

    def test_unknown_age_non_computable(self):
        param = SellerAgeParameter()
        listing = make_listing(
            seller_join_date=None,
            seller_account_age_days=None,
        )
        result = param.evaluate(listing, default_context())
        assert result.computable is False
        assert result.score == 0.5


class TestSellerListingHistoryParameter:
    def test_normal_listing_count(self):
        param = SellerListingHistoryParameter()
        listing = make_listing(seller_active_listings_count=5)
        result = param.evaluate(listing, default_context())
        assert result.score == 1.0

    def test_high_listing_count(self):
        param = SellerListingHistoryParameter()
        listing = make_listing(seller_active_listings_count=15)
        result = param.evaluate(listing, default_context())
        assert result.score < 1.0

    def test_unknown_count_non_computable(self):
        param = SellerListingHistoryParameter()
        listing = make_listing(seller_active_listings_count=None)
        result = param.evaluate(listing, default_context())
        assert result.computable is False
        assert result.score == 0.5


class TestContactInfoParameter:
    def test_has_contact(self):
        param = ContactInfoParameter()
        listing = make_listing(seller_contact="08123456789")
        result = param.evaluate(listing, default_context())
        assert result.score == 1.0

    def test_no_contact(self):
        param = ContactInfoParameter()
        listing = make_listing(seller_contact=None)
        result = param.evaluate(listing, default_context())
        assert result.score == 0.0

    def test_empty_contact(self):
        param = ContactInfoParameter()
        listing = make_listing(seller_contact="  ")
        result = param.evaluate(listing, default_context())
        assert result.score == 0.0


class TestMileageConsistencyParameter:
    def test_normal_mileage(self):
        param = MileageConsistencyParameter()
        # 6 years, 45000 km = 7500 km/year (normal)
        listing = make_listing(mileage=45000, year_of_manufacture=2020)
        result = param.evaluate(listing, default_context())
        assert result.score > 0.5

    def test_high_mileage(self):
        param = MileageConsistencyParameter()
        # 6 years, 250000 km = 41666 km/year (high)
        listing = make_listing(mileage=250000, year_of_manufacture=2020)
        result = param.evaluate(listing, default_context())
        assert result.score < 0.5

    def test_missing_mileage_non_computable(self):
        param = MileageConsistencyParameter()
        listing = make_listing(mileage=None)
        result = param.evaluate(listing, default_context())
        assert result.computable is False
        assert result.score == 0.5

    def test_missing_year_non_computable(self):
        param = MileageConsistencyParameter()
        listing = make_listing(year_of_manufacture=None)
        result = param.evaluate(listing, default_context())
        assert result.computable is False
        assert result.score == 0.5


class TestLocationSpecificityParameter:
    def test_city_level_location(self):
        param = LocationSpecificityParameter()
        listing = make_listing(location="Jakarta Selatan")
        result = param.evaluate(listing, default_context())
        assert result.score == 1.0

    def test_empty_location(self):
        param = LocationSpecificityParameter()
        listing = make_listing(location="")
        result = param.evaluate(listing, default_context())
        assert result.score == 0.0

    def test_province_only_location(self):
        param = LocationSpecificityParameter()
        listing = make_listing(location="Kalimantan")
        result = param.evaluate(listing, default_context())
        assert result.score == 0.0


class TestWeightRedistribution:
    """Test that weight redistribution works correctly when market data unavailable."""

    def test_redistribution_sums_to_one(self):
        config = create_config()
        scorer = TrustScorer(config)

        # Skip price_deviation
        weights = scorer._redistribute_weights(["price_deviation"])
        active_sum = sum(w for w in weights.values() if w > 0)
        assert abs(active_sum - 1.0) < 0.001

    def test_no_redistribution_without_skip(self):
        config = create_config()
        scorer = TrustScorer(config)

        weights = scorer._redistribute_weights([])
        for param in scorer.parameters:
            assert weights[param.name] == param.weight
