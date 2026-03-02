"""Market Making strategy — Stoikov-adapted model for binary outcome markets.

Quotes symmetric bid/ask pairs around a reservation price skewed by
inventory. Uses maker fees (0 bps) since all quotes are limit orders.

Key adaptations from Avellaneda-Stoikov for prediction markets:
- Price bounded to [0, 1] (binary outcomes)
- Spread widens near boundaries (0.08 buffer)
- Resolution taper: reduce size approaching expiry
- Inventory skew: holding YES → lower ask to offload
"""

import logging
import math
from typing import Optional

from core.constants import (
    MM_BASE_SPREAD,
    MM_BOUNDARY_BUFFER,
    MM_MAX_INVENTORY,
    MM_MAX_SPREAD,
    MM_MIN_SPREAD,
    MM_QUOTE_SIZE,
)
from core.types import (
    Market,
    OpenOrder,
    OrderBook,
    Outcome,
    Signal,
    SignalAction,
    StrategyType,
)

logger = logging.getLogger(__name__)


class MarketMakerStrategy:
    """Stoikov-adapted market maker for Polymarket binary outcomes.

    Algorithm:
    1. Calculate reservation price: mid skewed by inventory
    2. Calculate optimal spread: base + volatility, clamped
    3. Apply boundary safety near 0/1
    4. Apply resolution taper (reduce size near expiry)
    5. Output bid (BUY) and ask (SELL) signals
    """

    def __init__(
        self,
        base_spread: float = MM_BASE_SPREAD,
        min_spread: float = MM_MIN_SPREAD,
        max_spread: float = MM_MAX_SPREAD,
        max_inventory: int = MM_MAX_INVENTORY,
        quote_size: int = MM_QUOTE_SIZE,
        boundary_buffer: float = MM_BOUNDARY_BUFFER,
        skew_factor: float = 0.01,
        min_liquidity: float = 500.0,
        min_book_depth: float = 100.0,
        max_existing_spread: float = 0.20,
        taper_start_hours: float = 168.0,
        taper_stop_hours: float = 48.0,
    ):
        self.base_spread = base_spread
        self.min_spread = min_spread
        self.max_spread = max_spread
        self.max_inventory = max_inventory
        self.quote_size = quote_size
        self.boundary_buffer = boundary_buffer
        self.skew_factor = skew_factor
        self.min_liquidity = min_liquidity
        self.min_book_depth = min_book_depth
        self.max_existing_spread = max_existing_spread
        self.taper_start_hours = taper_start_hours
        self.taper_stop_hours = taper_stop_hours

    @property
    def strategy_type(self) -> StrategyType:
        return StrategyType.MARKET_MAKING

    @property
    def name(self) -> str:
        return "market_maker"

    def evaluate(
        self,
        market: Market,
        context: dict = None,
    ) -> list[Signal]:
        """Generate bid/ask quote pair for a market.

        Context should contain:
        - book_yes: OrderBook for YES token
        - inventory_yes: current YES shares held (positive = long)
        - inventory_no: current NO shares held
        - active_orders: list of OpenOrder for this market

        Returns:
            List of 0 or 2 signals (bid BUY + ask SELL)
        """
        if not self._passes_filters(market):
            return []

        context = context or {}
        book: Optional[OrderBook] = context.get("book_yes")
        inventory = context.get("inventory_yes", 0)

        # Get midpoint
        if book and book.bids and book.asks:
            mid = book.midpoint
            volatility = self._estimate_volatility(book)
        else:
            mid = market.midpoint_yes
            volatility = market.spread_yes / 2

        # 1. Reservation price — skew by inventory
        inventory_delta = inventory / self.max_inventory if self.max_inventory > 0 else 0
        reservation = mid - inventory_delta * self.skew_factor * max(volatility, 0.01)

        # 2. Optimal spread
        spread = self.base_spread + volatility * 0.5
        spread = max(self.min_spread, min(self.max_spread, spread))

        # 3. Raw bid/ask
        bid = reservation - spread / 2
        ask = reservation + spread / 2

        # 4. Boundary safety — widen spread near 0/1
        bid, ask = self._apply_boundary_safety(bid, ask)

        # Clamp to valid range
        bid = max(0.01, min(0.99, bid))
        ask = max(0.01, min(0.99, ask))

        # Ensure bid < ask
        if bid >= ask:
            return []

        # 5. Resolution taper — reduce size near expiry
        size = self._tapered_size(market)
        if size < 1:
            return []

        logger.debug(
            f"MM quote: {market.question[:30]}... "
            f"mid={mid:.4f} res={reservation:.4f} "
            f"bid={bid:.4f} ask={ask:.4f} spread={ask - bid:.4f} "
            f"inv={inventory} size={size}"
        )

        return [
            Signal(
                market_id=market.condition_id,
                action=SignalAction.BUY,
                outcome=Outcome.YES,
                strategy=StrategyType.MARKET_MAKING,
                price=bid,
                size=size * bid,  # Dollar cost
                edge=spread / 2,
                confidence=0.5,
                reasoning=f"MM bid @ {bid:.4f}",
                metadata={
                    "mm_side": "bid",
                    "reservation_price": reservation,
                    "spread": ask - bid,
                    "inventory": inventory,
                    "volatility": volatility,
                },
            ),
            Signal(
                market_id=market.condition_id,
                action=SignalAction.SELL,
                outcome=Outcome.YES,
                strategy=StrategyType.MARKET_MAKING,
                price=ask,
                size=size,  # Shares to sell
                edge=spread / 2,
                confidence=0.5,
                reasoning=f"MM ask @ {ask:.4f}",
                metadata={
                    "mm_side": "ask",
                    "reservation_price": reservation,
                    "spread": ask - bid,
                    "inventory": inventory,
                    "volatility": volatility,
                },
            ),
        ]

    def should_cancel_quotes(
        self,
        market: Market,
        context: dict = None,
    ) -> bool:
        """Check if existing quotes should be cancelled and refreshed.

        Reasons to cancel:
        - Market is no longer active
        - Inventory exceeds max
        - Market spread moved significantly
        - Approaching resolution
        """
        if not market.active:
            return True

        context = context or {}
        inventory = abs(context.get("inventory_yes", 0))
        if inventory >= self.max_inventory:
            return True

        hours = market.hours_to_resolution
        if hours is not None and hours < self.taper_stop_hours:
            return True

        return False

    def _passes_filters(self, market: Market) -> bool:
        """Pre-filters for MM eligibility."""
        if not market.active:
            return False

        if market.liquidity < self.min_liquidity:
            return False

        hours = market.hours_to_resolution
        if hours is not None and hours < self.taper_stop_hours:
            return False

        if not market.yes_token_id or not market.no_token_id:
            return False

        # Don't MM markets with huge existing spreads (illiquid)
        if market.spread_yes > self.max_existing_spread:
            return False

        return True

    def _estimate_volatility(self, book: OrderBook) -> float:
        """Estimate short-term volatility from order book spread."""
        return book.spread / 2

    def _apply_boundary_safety(self, bid: float, ask: float) -> tuple[float, float]:
        """Widen spread near price boundaries (0 and 1).

        When bid is below buffer or ask is above (1-buffer),
        shift quotes away from the boundary.
        """
        if bid < self.boundary_buffer:
            # Shift bid down more, but keep it positive
            bid = max(0.01, bid * 0.5)

        if ask > (1.0 - self.boundary_buffer):
            # Shift ask up more, but keep it < 1
            ask = min(0.99, ask + (1.0 - ask) * 0.5)

        return bid, ask

    def _tapered_size(self, market: Market) -> int:
        """Linearly reduce quote size as resolution approaches.

        Full size at taper_start_hours (168h / 1 week).
        Zero size at taper_stop_hours (48h / 2 days).
        """
        hours = market.hours_to_resolution
        if hours is None:
            return self.quote_size  # No end date — full size

        if hours <= self.taper_stop_hours:
            return 0

        if hours >= self.taper_start_hours:
            return self.quote_size

        # Linear interpolation
        fraction = (hours - self.taper_stop_hours) / (self.taper_start_hours - self.taper_stop_hours)
        return max(1, int(self.quote_size * fraction))
