"""Abstract base class for trading strategies."""

from abc import ABC, abstractmethod
from typing import Optional

from core.types import Market, Signal, StrategyType


class Strategy(ABC):
    """Base class for all trading strategies.

    A strategy evaluates a market and optionally generates a Signal
    indicating a trade should be placed.
    """

    @property
    @abstractmethod
    def strategy_type(self) -> StrategyType:
        """Strategy identifier."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable strategy name."""
        ...

    @abstractmethod
    def evaluate(
        self,
        market: Market,
        context: dict = None,
    ) -> Optional[Signal]:
        """Evaluate a market and optionally generate a trading signal.

        Args:
            market: The market to evaluate
            context: Additional data (prices, news, model estimates, etc.)

        Returns:
            Signal if a trade should be made, None otherwise
        """
        ...

    def should_exit(
        self,
        market: Market,
        context: dict = None,
    ) -> Optional[Signal]:
        """Check whether an existing position should be exited.

        Override in subclasses for strategy-specific exit logic.
        Default: no exit signal.
        """
        return None

    @property
    def enabled(self) -> bool:
        """Whether this strategy is active. Override to add toggle logic."""
        return True
