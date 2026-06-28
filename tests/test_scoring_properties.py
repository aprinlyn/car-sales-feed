"""Property-based tests for the trust scoring system.

# Feature: car-sales-feed, Property 2: Score range invariant
# Feature: car-sales-feed, Property 3: Scoring weights always sum to 1.0
# Feature: car-sales-feed, Property 4: Score-to-trust-level mapping is exhaustive and deterministic
# Feature: car-sales-feed, Property 5: Missing parameter defaults to midpoint
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta

import yaml
from hypothesis import given, settings, assume, HealthCheck
from hypothesis import strategies as st

from config.manager import ConfigManager
from models.listing import RawListing, SourcePlatform, TrustLevel
from scoring.scorer import TrustScorer, SimpleMarketDataProvider
from scoring.parameters import ScoringContext


def create_config() -> ConfigManager:
    """Create a ConfigManager with default scoring config."""
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


# Strategy for generating valid RawListings with various field combinations
raw_listing_strategy = st.builds(
    RawListing,
    title=st.text(min_size=1, max_size=50, alphabet=st.characters(whitelist_categories=("L", "N", "Zs"))),
    price=st.floats(min_value=1.0, max_value=1e12, allow_nan=False, allow_infinity=False),
    currency=st.just("IDR"),
    location=st.one_of(
        st.just(""),
        st.just("Jakarta"),
        st.just("Jakarta Selatan, DKI"),
        st.just("Jawa Barat"),
        st.text(min_size=1, max_size=30, alphabet=st.characters(whitelist_categories=("L", "Zs"))),
    ),
    description=st.text(min_size=0, max_size=300, alphabet=st.characters(whitelist_categories=("L", "N", "Zs", "P"))),
    seller_name=st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N"))),
    source_url=st.just("https://olx.co.id/item/test"),
    source_platform=st.just(SourcePlatform.OLX),
    scrape_timestamp=st.just(datetime(2026, 6, 1, 12, 0, 0)),
    seller_contact=st.one_of(st.none(), st.just("08123456789")),
    posting_date=st.one_of(st.none(), st.just(datetime(2026, 5, 15))),
    mileage=st.one_of(st.none(), st.integers(min_value=0, max_value=500000)),
    year_of_manufacture=st.one_of(st.none(), st.integers(min_value=2000, max_value=2026)),
    image_urls=st.lists(st.just("https://img.example.com/1.jpg"), min_size=0, max_size=10),
    seller_join_date=st.one_of(
        st.none(),
        st.datetimes(min_value=datetime(2015, 1, 1), max_value=datetime(2026, 6, 1)),
    ),
    seller_account_age_days=st.one_of(st.none(), st.integers(min_value=0, max_value=3650)),
    seller_verification_status=st.one_of(st.none(), st.booleans()),
    seller_active_listings_count=st.one_of(st.none(), st.integers(min_value=0, max_value=50)),
)


class TestScoreRangeInvariant:
    """Property 2: For any valid RawListing passed to the Trust Scorer,
    the computed Score SHALL be an integer between 0 and 100 inclusive."""

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(listing=raw_listing_strategy)
    def test_score_always_0_to_100(self, listing: RawListing):
        """Score is always an integer in [0, 100]."""
        config = create_config()
        scorer = TrustScorer(config)
        result = scorer.score(listing)

        assert isinstance(result.score, int)
        assert 0 <= result.score <= 100


class TestWeightsSumToOne:
    """Property 3: For any subset of computable Scoring Parameters,
    the effective weights used in score computation SHALL sum to 1.0
    (within floating-point tolerance of ±0.001)."""

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        has_market_data=st.booleans(),
    )
    def test_effective_weights_sum_to_one(self, has_market_data: bool):
        """Weights always sum to 1.0 whether market data exists or not."""
        config = create_config()
        scorer = TrustScorer(config)

        # Original weights sum to 1.0
        original_sum = sum(p.weight for p in scorer.parameters)
        assert abs(original_sum - 1.0) < 0.001

        # Redistribute and verify
        skipped = [] if has_market_data else ["price_deviation"]
        effective_weights = scorer._redistribute_weights(skipped)

        active_weight_sum = sum(
            w for name, w in effective_weights.items() if w > 0
        )
        assert abs(active_weight_sum - 1.0) < 0.001

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        skip_count=st.integers(min_value=0, max_value=3),
    )
    def test_redistribution_preserves_total_weight(self, skip_count: int):
        """Redistributing any subset of params still sums to 1.0."""
        config = create_config()
        scorer = TrustScorer(config)

        param_names = [p.name for p in scorer.parameters]
        skipped = param_names[:skip_count]

        effective_weights = scorer._redistribute_weights(skipped)
        active_weight_sum = sum(
            w for name, w in effective_weights.items() if w > 0
        )

        if skip_count < len(param_names):
            assert abs(active_weight_sum - 1.0) < 0.001


class TestTrustLevelMapping:
    """Property 4: For any integer score in the range 0–100, the trust level
    assignment SHALL produce exactly one of: 'safe' (70–100), 'suspicious' (40–69),
    or 'unsafe' (0–39), with no gaps or overlaps."""

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(score=st.integers(min_value=0, max_value=100))
    def test_every_score_maps_to_exactly_one_level(self, score: int):
        """Every integer score 0-100 produces exactly one trust level."""
        config = create_config()
        scorer = TrustScorer(config)

        level = scorer._assign_trust_level(score)

        assert level in (TrustLevel.SAFE, TrustLevel.SUSPICIOUS, TrustLevel.UNSAFE)

        # Verify the correct mapping
        if score >= 70:
            assert level == TrustLevel.SAFE
        elif score >= 40:
            assert level == TrustLevel.SUSPICIOUS
        else:
            assert level == TrustLevel.UNSAFE

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(score=st.integers(min_value=70, max_value=100))
    def test_safe_range(self, score: int):
        """Scores 70-100 always map to SAFE."""
        config = create_config()
        scorer = TrustScorer(config)
        assert scorer._assign_trust_level(score) == TrustLevel.SAFE

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(score=st.integers(min_value=40, max_value=69))
    def test_suspicious_range(self, score: int):
        """Scores 40-69 always map to SUSPICIOUS."""
        config = create_config()
        scorer = TrustScorer(config)
        assert scorer._assign_trust_level(score) == TrustLevel.SUSPICIOUS

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(score=st.integers(min_value=0, max_value=39))
    def test_unsafe_range(self, score: int):
        """Scores 0-39 always map to UNSAFE."""
        config = create_config()
        scorer = TrustScorer(config)
        assert scorer._assign_trust_level(score) == TrustLevel.UNSAFE


class TestMissingParameterMidpoint:
    """Property 5: For any RawListing where one or more Scoring Parameters cannot
    be computed due to missing data, each non-computable parameter SHALL receive
    a score of 0.5, and the listing SHALL be flagged as partially scored."""

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(listing=raw_listing_strategy)
    def test_non_computable_params_get_midpoint(self, listing: RawListing):
        """Non-computable parameters always get 0.5 score."""
        config = create_config()
        scorer = TrustScorer(config)

        context = ScoringContext(
            market_average_price=None,  # Force price_deviation to be non-computable
            current_year=2026,
        )

        for param in scorer.parameters:
            result = param.evaluate(listing, context)
            if not result.computable:
                assert result.score == 0.5, (
                    f"Parameter '{param.name}' is non-computable but score is "
                    f"{result.score} (expected 0.5)"
                )

    def test_missing_data_flags_partially_scored(self):
        """Listing with missing data is flagged as partially_scored."""
        config = create_config()
        scorer = TrustScorer(config)

        # Listing with minimal data — many params won't be computable
        listing = RawListing(
            title="Test Car",
            price=100_000_000.0,
            currency="IDR",
            location="",
            description="",
            seller_name="Seller",
            source_url="https://olx.co.id/item/test",
            source_platform=SourcePlatform.OLX,
            scrape_timestamp=datetime.utcnow(),
            # All optional fields are None → non-computable params
        )

        result = scorer.score(listing)
        # With no market data and no seller info, should be partially scored
        assert result.partially_scored is True
