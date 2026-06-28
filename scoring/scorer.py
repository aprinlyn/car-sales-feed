"""Trust scoring system for car listings."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol

from config.manager import ConfigManager
from models.listing import RawListing, ScoredListing, TrustLevel
from scoring.parameters import (
    ContactInfoParameter,
    DescriptionLengthParameter,
    ImageCountParameter,
    LocationSpecificityParameter,
    MileageConsistencyParameter,
    PriceDeviationParameter,
    ScoringContext,
    ScoringParameter,
    SellerAgeParameter,
    SellerListingHistoryParameter,
)

logger = logging.getLogger(__name__)


class MarketDataProvider(Protocol):
    """Protocol for providing market average price data."""

    def get_average_price(self, model: str, year: int | None) -> float | None:
        """Get average market price for a car model and year. Returns None if unavailable."""
        ...


class SimpleMarketDataProvider:
    """Simple market data provider that returns None (no data available).

    Replace with actual market data lookup in production.
    """

    def get_average_price(self, model: str, year: int | None) -> float | None:
        return None


class TrustScorer:
    """Computes trust scores for car listings based on configurable parameters.

    Scoring flow:
    1. Evaluate each parameter against the listing
    2. Handle non-computable parameters (assign midpoint 0.5, flag as partial)
    3. Redistribute weight from skipped parameters (market data unavailable)
    4. Compute weighted sum → integer score 0–100
    5. Map score to trust level
    """

    def __init__(self, config: ConfigManager, market_data: MarketDataProvider | None = None):
        self.config = config
        self.market_data = market_data or SimpleMarketDataProvider()
        self._safe_min = int(config.get("scoring.thresholds.safe_min", 70))
        self._suspicious_min = int(config.get("scoring.thresholds.suspicious_min", 40))
        self.parameters = self._init_parameters()

    def _init_parameters(self) -> list[ScoringParameter]:
        """Initialize scoring parameters with configured weights."""
        return [
            PriceDeviationParameter(
                weight=float(self.config.get("scoring.weights.price_deviation", 0.20))
            ),
            ImageCountParameter(
                weight=float(self.config.get("scoring.weights.image_count", 0.10))
            ),
            DescriptionLengthParameter(
                weight=float(self.config.get("scoring.weights.description_length", 0.10))
            ),
            SellerAgeParameter(
                weight=float(self.config.get("scoring.weights.seller_age", 0.15))
            ),
            SellerListingHistoryParameter(
                weight=float(self.config.get("scoring.weights.seller_listing_history", 0.10))
            ),
            ContactInfoParameter(
                weight=float(self.config.get("scoring.weights.contact_info", 0.10))
            ),
            MileageConsistencyParameter(
                weight=float(self.config.get("scoring.weights.mileage_consistency", 0.10))
            ),
            LocationSpecificityParameter(
                weight=float(self.config.get("scoring.weights.location_specificity", 0.15))
            ),
        ]

    def score(self, listing: RawListing) -> ScoredListing:
        """Compute trust score and level for a listing."""
        context = ScoringContext(
            market_average_price=self.market_data.get_average_price(
                listing.title, listing.year_of_manufacture
            ),
            current_year=datetime.utcnow().year,
        )

        # Evaluate all parameters
        results: dict[str, tuple[float, bool]] = {}  # name -> (score, computable)
        partially_scored = False

        for param in self.parameters:
            result = param.evaluate(listing, context)
            results[param.name] = (result.score, result.computable)
            if not result.computable:
                partially_scored = True

        # Determine which parameters need weight redistribution
        # (price_deviation when market data unavailable is "skipped" per spec)
        skipped_params: list[str] = []
        computable_params: list[str] = []

        for param in self.parameters:
            score_val, computable = results[param.name]
            if param.name == "price_deviation" and not computable:
                # Price deviation is fully skipped when market data unavailable
                skipped_params.append(param.name)
            else:
                computable_params.append(param.name)

        # Compute effective weights after redistribution
        effective_weights = self._redistribute_weights(skipped_params)

        # Compute weighted sum
        weighted_sum = 0.0
        parameter_scores: dict[str, float] = {}

        for param in self.parameters:
            param_score, _ = results[param.name]
            parameter_scores[param.name] = param_score

            if param.name not in skipped_params:
                weighted_sum += param_score * effective_weights[param.name]

        # Convert to 0–100 integer
        final_score = max(0, min(100, round(weighted_sum * 100)))

        # Assign trust level
        trust_level = self._assign_trust_level(final_score)

        return ScoredListing(
            raw=listing,
            score=final_score,
            trust_level=trust_level,
            partially_scored=partially_scored,
            parameter_scores=parameter_scores,
            scoring_timestamp=datetime.utcnow(),
        )

    def _redistribute_weights(self, skipped_params: list[str]) -> dict[str, float]:
        """Redistribute weight from skipped params equally to remaining computable ones.

        Returns a dict of param_name -> effective_weight.
        If no params are skipped, returns original weights.
        """
        active_params = [p for p in self.parameters if p.name not in skipped_params]

        if not skipped_params or not active_params:
            return {p.name: p.weight for p in self.parameters}

        # Total weight to redistribute
        skipped_weight = sum(
            p.weight for p in self.parameters if p.name in skipped_params
        )

        # Distribute equally among active params
        extra_per_param = skipped_weight / len(active_params)

        effective_weights: dict[str, float] = {}
        for param in self.parameters:
            if param.name in skipped_params:
                effective_weights[param.name] = 0.0
            else:
                effective_weights[param.name] = param.weight + extra_per_param

        return effective_weights

    def _assign_trust_level(self, score: int) -> TrustLevel:
        """Map numeric score to trust level category.

        70–100 → safe
        40–69  → suspicious
        0–39   → unsafe
        """
        if score >= self._safe_min:
            return TrustLevel.SAFE
        elif score >= self._suspicious_min:
            return TrustLevel.SUSPICIOUS
        else:
            return TrustLevel.UNSAFE
