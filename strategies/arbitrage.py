"""Arbitrage strategy — buy YES + NO when combined ask < $1.00 after fees.

On Polymarket, YES + NO shares for the same market always resolve to $1.00.
If best_ask_yes + best_ask_no < 1.00 after taker fees, buying both locks in
a guaranteed profit regardless of the outcome.
"""

import logging
from typing import Optional

from core.constants import (
    ARB_MAX_POSITION,
    ARB_MIN_PROFIT_BPS,
    TAKER_FEE_BPS,
)
from core.types import (
    Market,
    OrderBook,
    OrderBookLevel,
    Outcome,
    Signal,
    SignalAction,
    StrategyType,
)

logger = logging.getLogger(__name__)


class ArbitrageStrategy:
    """Detect and exploit YES + NO < $1.00 arbitrage opportunities.

    When the sum of best asks for YES and NO is less than $1.00 after
    taker fees (2% per side), buying both sides guarantees a profit at
    resolution — one side pays $1.00, the other pays $0.00.
    """

    def __init__(
        self,
        min_profit_bps: int = ARB_MIN_PROFIT_BPS,
        max_position: float = ARB_MAX_POSITION,
        fee_bps: int = TAKER_FEE_BPS,
    ):
        self.min_profit_bps = min_profit_bps
        self.max_position = max_position
        self.fee_bps = fee_bps
        self._fee_mult = 1 + fee_bps / 10000  # 1.02 for 2% taker

    @property
    def strategy_type(self) -> StrategyType:
        return StrategyType.ARBITRAGE

    @property
    def name(self) -> str:
        return "arbitrage"

    def evaluate(
        self,
        market: Market,
        context: dict = None,
    ) -> list[Signal]:
        """Evaluate a market for internal arb (YES + NO < $1.00).

        Returns:
            List of 0 or 2 signals (BUY YES + BUY NO) if arb exists.
        """
        if not market.active:
            return []

        if not market.yes_token_id or not market.no_token_id:
            return []

        context = context or {}
        book_yes: Optional[OrderBook] = context.get("book_yes")
        book_no: Optional[OrderBook] = context.get("book_no")

        if not book_yes or not book_no:
            # Fall back to market-level prices
            return self._check_market_prices(market)

        return self._check_order_books(market, book_yes, book_no)

    def _check_market_prices(self, market: Market) -> list[Signal]:
        """Quick check using market best ask prices."""
        ask_yes = market.best_ask_yes
        ask_no = market.best_ask_no

        if ask_yes <= 0 or ask_no <= 0 or ask_yes >= 1 or ask_no >= 1:
            return []

        return self._evaluate_arb(market, ask_yes, ask_no, size=None)

    def _check_order_books(
        self, market: Market, book_yes: OrderBook, book_no: OrderBook
    ) -> list[Signal]:
        """Walk order books to find executable arb size at profitable prices."""
        if not book_yes.asks or not book_no.asks:
            return []

        ask_yes = book_yes.best_ask
        ask_no = book_no.best_ask

        # Determine max executable size from book depth
        max_size_yes = self._walkable_size(book_yes.asks)
        max_size_no = self._walkable_size(book_no.asks)
        max_size = min(max_size_yes, max_size_no, self.max_position)

        return self._evaluate_arb(market, ask_yes, ask_no, size=max_size)

    def _evaluate_arb(
        self,
        market: Market,
        ask_yes: float,
        ask_no: float,
        size: Optional[float],
    ) -> list[Signal]:
        """Core arb check: is YES + NO < $1.00 after fees?"""
        total_cost_per_share = (ask_yes + ask_no) * self._fee_mult
        payout_per_share = 1.0  # One side always resolves to $1

        if total_cost_per_share >= payout_per_share:
            return []  # No arb

        profit_per_share = payout_per_share - total_cost_per_share
        profit_bps = int(profit_per_share / total_cost_per_share * 10000)

        if profit_bps < self.min_profit_bps:
            return []  # Below minimum

        # Size: use book-derived size or default from max_position
        if size is None:
            # Estimate shares from dollar amount
            shares = self.max_position / total_cost_per_share
        else:
            shares = min(size, self.max_position / total_cost_per_share)

        if shares < 1:
            return []

        guaranteed_profit = profit_per_share * shares

        logger.info(
            f"ARB FOUND: {market.question[:40]}... "
            f"YES@{ask_yes:.4f} + NO@{ask_no:.4f} = {ask_yes + ask_no:.4f} "
            f"(after fees: {total_cost_per_share:.4f}) "
            f"profit={profit_bps}bps ${guaranteed_profit:.2f}"
        )

        metadata = {
            "is_arb": True,
            "guaranteed_profit": guaranteed_profit,
            "profit_bps": profit_bps,
            "total_cost_per_share": total_cost_per_share,
        }

        return [
            Signal(
                market_id=market.condition_id,
                action=SignalAction.BUY,
                outcome=Outcome.YES,
                strategy=StrategyType.ARBITRAGE,
                price=ask_yes,
                size=shares * ask_yes,  # Dollar cost for YES leg
                edge=profit_per_share,
                confidence=1.0,  # Guaranteed profit
                reasoning=f"Arb leg: BUY YES @ {ask_yes:.4f}",
                metadata={**metadata, "arb_leg": "yes"},
            ),
            Signal(
                market_id=market.condition_id,
                action=SignalAction.BUY,
                outcome=Outcome.NO,
                strategy=StrategyType.ARBITRAGE,
                price=ask_no,
                size=shares * ask_no,  # Dollar cost for NO leg
                edge=profit_per_share,
                confidence=1.0,
                reasoning=f"Arb leg: BUY NO @ {ask_no:.4f}",
                metadata={**metadata, "arb_leg": "no"},
            ),
        ]

    def _walkable_size(self, levels: list[OrderBookLevel]) -> float:
        """Sum available size across order book levels."""
        return sum(level.size for level in levels)

    def check_cross_platform(
        self,
        market: Market,
        external_price: float,
        context: dict = None,
    ) -> list[Signal]:
        """Stub for cross-platform arbitrage (e.g., Polymarket vs Kalshi).

        Future: compare Polymarket price with Kalshi or other platforms.
        Currently returns empty — placeholder for when Kalshi integration is added.
        """
        return []
