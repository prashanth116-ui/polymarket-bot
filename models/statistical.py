"""Statistical probability models — base rates, implied odds, historical patterns."""

import logging
import math
from datetime import datetime, timezone
from typing import Optional

from core.types import Market, Outcome, ProbabilityEstimate
from models.base import ProbabilityModel

logger = logging.getLogger(__name__)


class MarketImpliedModel(ProbabilityModel):
    """Uses the market's own price as a probability estimate.

    Useful as a baseline — the market price IS a probability estimate
    informed by all participants. This model adjusts for known biases:
    - Favorite-longshot bias: extreme prices are less reliable
    - Liquidity discount: thin markets have noisier prices
    """

    @property
    def name(self) -> str:
        return "market_implied"

    def predict(
        self,
        market: Market,
        outcome: Outcome,
        context: dict = None,
    ) -> Optional[ProbabilityEstimate]:
        price = market.last_price_yes if outcome == Outcome.YES else market.last_price_no

        # Adjust for favorite-longshot bias
        # Extreme prices (near 0 or 1) tend to be less accurate
        adjusted = self._adjust_flb(price)

        # Confidence based on liquidity
        confidence = self._liquidity_confidence(market.liquidity)

        return ProbabilityEstimate(
            market_id=market.condition_id,
            outcome=outcome,
            probability=adjusted,
            confidence=confidence,
            reasoning=f"Market-implied probability with favorite-longshot bias adjustment (raw: {price:.1%})",
            model_name=self.name,
        )

    def _adjust_flb(self, price: float) -> float:
        """Adjust for favorite-longshot bias.

        Markets tend to overprice longshots (low probability events)
        and underprice favorites (high probability events).
        Apply a mild logistic correction.
        """
        if price <= 0.01 or price >= 0.99:
            return price

        # Logit transform, mild shrinkage toward 0.5
        shrinkage = 0.1
        logit_p = math.log(price / (1 - price))
        adjusted_logit = logit_p * (1 - shrinkage)
        adjusted = 1 / (1 + math.exp(-adjusted_logit))

        return max(0.01, min(0.99, adjusted))

    def _liquidity_confidence(self, liquidity: float) -> float:
        """Higher liquidity = more confidence in market price."""
        if liquidity <= 0:
            return 0.3
        # Log scale: $100 -> 0.5, $1000 -> 0.65, $10000 -> 0.8, $100000 -> 0.95
        conf = 0.3 + 0.15 * math.log10(max(1, liquidity))
        return max(0.3, min(0.95, conf))


class BaseRateModel(ProbabilityModel):
    """Uses historical base rates for common prediction categories.

    Base rates from historical prediction market resolutions and
    real-world event frequencies.
    """

    # Historical base rates for common categories
    BASE_RATES = {
        "politics": {
            "incumbent_wins": 0.60,
            "party_change": 0.45,
            "legislation_passes": 0.35,
            "impeachment_conviction": 0.05,
            "supreme_court_overturn": 0.10,
        },
        "crypto": {
            "price_target_hit": 0.40,
            "new_ath": 0.30,
            "regulation_passes": 0.25,
            "hack_occurs": 0.15,
        },
        "sports": {
            "favorite_wins": 0.60,
            "upset": 0.25,
            "champion_repeats": 0.30,
        },
        "economics": {
            "recession_this_year": 0.15,
            "rate_cut": 0.40,
            "rate_hike": 0.30,
            "inflation_above_target": 0.45,
        },
        "science": {
            "fda_approval": 0.30,
            "launch_success": 0.85,
            "discovery_confirmed": 0.20,
        },
    }

    @property
    def name(self) -> str:
        return "base_rate"

    def predict(
        self,
        market: Market,
        outcome: Outcome,
        context: dict = None,
    ) -> Optional[ProbabilityEstimate]:
        category = market.category.lower() if market.category else "other"
        question_lower = market.question.lower()

        # Try to match question to a known base rate
        base_rate = self._find_base_rate(category, question_lower)
        if base_rate is None:
            return None

        prob = base_rate if outcome == Outcome.YES else (1 - base_rate)

        return ProbabilityEstimate(
            market_id=market.condition_id,
            outcome=outcome,
            probability=prob,
            confidence=0.4,  # Base rates are low-confidence
            reasoning=f"Historical base rate for {category} category events",
            model_name=self.name,
        )

    def _find_base_rate(self, category: str, question: str) -> Optional[float]:
        """Match question keywords to known base rates."""
        rates = self.BASE_RATES.get(category, {})

        keyword_map = {
            "incumbent": "incumbent_wins",
            "reelect": "incumbent_wins",
            "flip": "party_change",
            "pass": "legislation_passes",
            "impeach": "impeachment_conviction",
            "convict": "impeachment_conviction",
            "overturn": "supreme_court_overturn",
            "bitcoin": "price_target_hit",
            "ethereum": "price_target_hit",
            "all-time high": "new_ath",
            "ath": "new_ath",
            "hack": "hack_occurs",
            "recession": "recession_this_year",
            "rate cut": "rate_cut",
            "rate hike": "rate_hike",
            "inflation": "inflation_above_target",
            "fda": "fda_approval",
            "approve": "fda_approval",
            "launch": "launch_success",
            "spacex": "launch_success",
            "favorite": "favorite_wins",
            "champion": "champion_repeats",
            "repeat": "champion_repeats",
        }

        for keyword, rate_key in keyword_map.items():
            if keyword in question:
                if rate_key in rates:
                    return rates[rate_key]
                # Try cross-category lookup
                for cat_rates in self.BASE_RATES.values():
                    if rate_key in cat_rates:
                        return cat_rates[rate_key]

        return None

    def supports_market(self, market: Market) -> bool:
        category = market.category.lower() if market.category else "other"
        return category in self.BASE_RATES


class TimeDecayModel(ProbabilityModel):
    """Adjusts probability based on time remaining until resolution.

    Markets near resolution with extreme prices (>0.85 or <0.15)
    are more likely to stay extreme. Markets far from resolution
    are pulled toward 0.5.
    """

    @property
    def name(self) -> str:
        return "time_decay"

    def predict(
        self,
        market: Market,
        outcome: Outcome,
        context: dict = None,
    ) -> Optional[ProbabilityEstimate]:
        hours = market.hours_to_resolution
        if hours is None:
            return None

        price = market.last_price_yes if outcome == Outcome.YES else market.last_price_no

        # Time factor: how much to pull toward 0.5
        # Near resolution (< 24h): barely adjust (factor ~0.05)
        # Far from resolution (> 720h/30d): pull significantly (factor ~0.3)
        if hours <= 0:
            time_factor = 0.0
        elif hours < 24:
            time_factor = 0.05
        elif hours < 168:  # 1 week
            time_factor = 0.10
        elif hours < 720:  # 30 days
            time_factor = 0.20
        else:
            time_factor = 0.30

        # Pull toward 0.5
        adjusted = price + time_factor * (0.5 - price)
        adjusted = max(0.01, min(0.99, adjusted))

        confidence = 0.3 + (0.4 if hours < 72 else 0.0)  # More confident near resolution

        return ProbabilityEstimate(
            market_id=market.condition_id,
            outcome=outcome,
            probability=adjusted,
            confidence=confidence,
            reasoning=f"Time-decay adjustment: {hours:.0f}h to resolution (factor={time_factor:.2f})",
            model_name=self.name,
        )
