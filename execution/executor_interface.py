"""Abstract executor interface for order execution."""

from abc import ABC, abstractmethod
from typing import Optional

from core.types import (
    OrderBook,
    Outcome,
    Position,
    Side,
    TradeResult,
)


class ExecutorInterface(ABC):
    """Abstract base for all executors (paper, bridge, multi)."""

    @abstractmethod
    def buy(
        self,
        market_id: str,
        token_id: str,
        outcome: Outcome,
        price: float,
        size: float,
    ) -> TradeResult:
        """Place a buy order.

        Args:
            market_id: Market condition ID
            token_id: Token ID for the outcome
            outcome: YES or NO
            price: Limit price (0-1)
            size: Number of shares

        Returns:
            TradeResult with fill details
        """
        ...

    @abstractmethod
    def sell(
        self,
        market_id: str,
        token_id: str,
        outcome: Outcome,
        price: float,
        size: float,
    ) -> TradeResult:
        """Place a sell order."""
        ...

    @abstractmethod
    def cancel(self, order_id: str) -> bool:
        """Cancel an open order."""
        ...

    @abstractmethod
    def get_positions(self) -> list[Position]:
        """Get all open positions."""
        ...

    @abstractmethod
    def get_balance(self) -> float:
        """Get available USDC balance."""
        ...

    @abstractmethod
    def get_order_book(self, token_id: str) -> Optional[OrderBook]:
        """Get order book for a token."""
        ...
