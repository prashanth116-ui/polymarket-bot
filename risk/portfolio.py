"""Portfolio-level exposure tracking and correlated market detection."""

import logging
from typing import Optional

from core.types import Market, Outcome, Position

logger = logging.getLogger(__name__)


class Portfolio:
    """Tracks portfolio-wide exposure, P/L, and correlated positions.

    Used by the risk manager and live loop to enforce portfolio-level
    constraints and provide summary metrics.
    """

    def __init__(self):
        self._positions: dict[str, Position] = {}  # key: "market_id:outcome"
        self._market_metadata: dict[str, dict] = {}  # market_id -> {category, question, ...}

    def _key(self, market_id: str, outcome: Outcome) -> str:
        return f"{market_id}:{outcome.value}"

    def add_position(self, position: Position, market: Market = None):
        """Track a new open position."""
        key = self._key(position.market_id, position.outcome)
        self._positions[key] = position

        if market:
            self._market_metadata[position.market_id] = {
                "category": market.category or "other",
                "question": market.question,
            }

    def remove_position(self, market_id: str, outcome: Outcome):
        """Remove a closed position."""
        key = self._key(market_id, outcome)
        self._positions.pop(key, None)

    def update_position(self, market_id: str, outcome: Outcome, current_price: float):
        """Update a position's current price and P/L."""
        key = self._key(market_id, outcome)
        pos = self._positions.get(key)
        if pos:
            pos.update_pnl(current_price)

    def get_position(self, market_id: str, outcome: Outcome) -> Optional[Position]:
        key = self._key(market_id, outcome)
        return self._positions.get(key)

    def has_position(self, market_id: str) -> bool:
        """Check if we have any position in this market (YES or NO)."""
        for key in self._positions:
            if key.startswith(market_id + ":"):
                return True
        return False

    @property
    def positions(self) -> list[Position]:
        return list(self._positions.values())

    @property
    def position_count(self) -> int:
        return len(self._positions)

    @property
    def total_exposure(self) -> float:
        """Total cost basis of all open positions."""
        return sum(p.cost_basis for p in self._positions.values())

    @property
    def total_unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self._positions.values())

    @property
    def total_market_value(self) -> float:
        return sum(p.market_value for p in self._positions.values())

    def exposure_by_category(self) -> dict[str, float]:
        """Get total exposure grouped by market category."""
        categories: dict[str, float] = {}
        for pos in self._positions.values():
            meta = self._market_metadata.get(pos.market_id, {})
            cat = meta.get("category", "other")
            categories[cat] = categories.get(cat, 0.0) + pos.cost_basis
        return categories

    def exposure_by_strategy(self) -> dict[str, float]:
        """Get total exposure grouped by strategy type."""
        strategies: dict[str, float] = {}
        for pos in self._positions.values():
            key = pos.strategy.value if pos.strategy else "unknown"
            strategies[key] = strategies.get(key, 0.0) + pos.cost_basis
        return strategies

    def find_correlated(self, market: Market) -> list[Position]:
        """Find existing positions that might be correlated with a market.

        Looks for markets in the same category or with similar keywords.
        """
        correlated = []
        category = market.category or "other"
        question_words = set(market.question.lower().split())

        for pos in self._positions.values():
            if pos.market_id == market.condition_id:
                continue

            meta = self._market_metadata.get(pos.market_id, {})

            # Same category
            if meta.get("category") == category:
                correlated.append(pos)
                continue

            # Keyword overlap
            pos_words = set(meta.get("question", "").lower().split())
            overlap = question_words & pos_words
            meaningful = overlap - {"will", "the", "be", "in", "a", "an", "by", "to", "of", "?"}
            if len(meaningful) >= 2:
                correlated.append(pos)

        return correlated

    def remaining_exposure(self, max_exposure: float) -> float:
        """How much more exposure can we take before hitting the cap."""
        return max(0, max_exposure - self.total_exposure)

    def remaining_category_exposure(self, category: str, max_per_category: float) -> float:
        """How much more exposure we can take in a specific category."""
        current = self.exposure_by_category().get(category, 0.0)
        return max(0, max_per_category - current)

    def summary(self) -> dict:
        return {
            "positions": self.position_count,
            "total_exposure": round(self.total_exposure, 2),
            "total_unrealized_pnl": round(self.total_unrealized_pnl, 2),
            "total_market_value": round(self.total_market_value, 2),
            "by_category": {k: round(v, 2) for k, v in self.exposure_by_category().items()},
            "by_strategy": {k: round(v, 2) for k, v in self.exposure_by_strategy().items()},
        }
