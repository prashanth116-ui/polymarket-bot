"""Abstract base class for probability estimation models."""

from abc import ABC, abstractmethod
from typing import Optional

from core.types import Market, Outcome, ProbabilityEstimate


class ProbabilityModel(ABC):
    """Base class for all probability estimation models.

    Each model takes a market + contextual data and returns a
    ProbabilityEstimate for a given outcome.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique model identifier (e.g., 'llm_claude', 'statistical_polls')."""
        ...

    @abstractmethod
    def predict(
        self,
        market: Market,
        outcome: Outcome,
        context: dict = None,
    ) -> Optional[ProbabilityEstimate]:
        """Estimate the probability of a market outcome.

        Args:
            market: The prediction market to evaluate
            outcome: Which outcome to estimate (YES or NO)
            context: Additional data (news articles, polls, economic indicators, etc.)

        Returns:
            ProbabilityEstimate or None if the model can't make a prediction
        """
        ...

    def supports_market(self, market: Market) -> bool:
        """Whether this model can handle this type of market.

        Override to restrict models to specific categories or market types.
        Default: supports all markets.
        """
        return True

    def cost_per_call(self) -> float:
        """Estimated cost per prediction in USD. Override for paid APIs."""
        return 0.0
