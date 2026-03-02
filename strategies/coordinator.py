"""Strategy Coordinator — orchestrates Edge, Arbitrage, and Market Making.

Priority order: Edge > Arbitrage > Market Making.
Prevents conflicts: a market with an edge position won't get MM quotes.
Enforces per-strategy exposure caps.
"""

import logging
from typing import Optional

from core.constants import (
    ARB_MAX_ARB_EXPOSURE,
    MM_MAX_MM_EXPOSURE,
    MM_MAX_MM_MARKETS,
)
from core.types import (
    Market,
    OpenOrder,
    Signal,
    StrategyType,
)
from strategies.arbitrage import ArbitrageStrategy
from strategies.edge_strategy import EdgeStrategy
from strategies.market_maker import MarketMakerStrategy

logger = logging.getLogger(__name__)


class StrategyCoordinator:
    """Orchestrates multiple strategies without conflicts.

    Priority: Edge > Arbitrage > Market Making.

    Conflict rules:
    - Market with edge position → no MM/arb signals
    - Market with arb position → no MM signals
    - Arb found → cancel existing MM quotes on that market
    - Edge found → block MM/arb for that market
    """

    def __init__(
        self,
        edge_strategy: EdgeStrategy,
        arb_strategy: ArbitrageStrategy,
        mm_strategy: MarketMakerStrategy,
        max_mm_markets: int = MM_MAX_MM_MARKETS,
        max_mm_exposure: float = MM_MAX_MM_EXPOSURE,
        max_arb_exposure: float = ARB_MAX_ARB_EXPOSURE,
    ):
        self.edge = edge_strategy
        self.arb = arb_strategy
        self.mm = mm_strategy
        self.max_mm_markets = max_mm_markets
        self.max_mm_exposure = max_mm_exposure
        self.max_arb_exposure = max_arb_exposure

        # Track active markets per strategy
        self._edge_markets: set[str] = set()  # Markets with edge positions
        self._arb_markets: set[str] = set()   # Markets with arb positions
        self._mm_markets: set[str] = set()    # Markets with active MM quotes

        # Exposure tracking
        self._mm_exposure: float = 0.0
        self._arb_exposure: float = 0.0

        # Open order tracking
        self._open_orders: dict[str, OpenOrder] = {}  # order_id -> OpenOrder

        # Degraded mode — bridge down, only allow edge exits
        self._degraded: bool = False

    @property
    def degraded_mode(self) -> bool:
        """Whether the coordinator is in degraded mode (bridge down)."""
        return self._degraded

    @degraded_mode.setter
    def degraded_mode(self, value: bool):
        if value != self._degraded:
            self._degraded = value
            logger.warning(f"Coordinator degraded_mode={'ON' if value else 'OFF'}")

    def evaluate_market(
        self,
        market: Market,
        context: dict = None,
    ) -> list[Signal]:
        """Run strategies in priority order, respecting conflict rules.

        Returns all valid signals for this market (may include multiple
        from different strategies if no conflicts).

        In degraded mode: blocks all new entries (MM, arb, edge).
        Edge exits are handled separately in _manage_positions.
        """
        context = context or {}
        cid = market.condition_id
        signals: list[Signal] = []

        # Degraded mode: no new entries at all
        if self._degraded:
            return signals

        # Priority 1: Edge
        if cid not in self._edge_markets:
            edge_signal = self.edge.evaluate(market, context)
            if edge_signal:
                signals.append(edge_signal)
                # Edge blocks MM and arb on this market
                return signals

        # If edge position exists, skip arb and MM
        if cid in self._edge_markets:
            return signals

        # Priority 2: Arbitrage
        if cid not in self._arb_markets:
            if self._arb_exposure < self.max_arb_exposure:
                arb_signals = self.arb.evaluate(market, context)
                if arb_signals:
                    signals.extend(arb_signals)
                    return signals

        # If arb position exists, skip MM
        if cid in self._arb_markets:
            return signals

        # Priority 3: Market Making
        if len(self._mm_markets) < self.max_mm_markets:
            if self._mm_exposure < self.max_mm_exposure:
                mm_signals = self.mm.evaluate(market, context)
                if mm_signals:
                    signals.extend(mm_signals)

        return signals

    def get_markets_to_mm(self, all_markets: list[Market]) -> list[Market]:
        """Select the best markets for market making.

        Ranks by spread * liquidity — wider spreads with good liquidity
        are more profitable for MM.
        """
        candidates = []
        for m in all_markets:
            if m.condition_id in self._edge_markets:
                continue
            if m.condition_id in self._arb_markets:
                continue
            if not m.active:
                continue
            if m.liquidity < self.mm.min_liquidity:
                continue

            score = m.spread_yes * m.liquidity
            candidates.append((score, m))

        candidates.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in candidates[:self.max_mm_markets]]

    # --- Order lifecycle tracking ---

    def record_order_placed(self, order: OpenOrder):
        """Track a newly placed order."""
        self._open_orders[order.order_id] = order
        if order.strategy == StrategyType.MARKET_MAKING:
            self._mm_markets.add(order.market_id)

    def record_order_filled(self, order_id: str, fill_cost: float):
        """Track an order fill."""
        order = self._open_orders.pop(order_id, None)
        if not order:
            return

        if order.strategy == StrategyType.MARKET_MAKING:
            self._mm_exposure += fill_cost
        elif order.strategy == StrategyType.ARBITRAGE:
            self._arb_exposure += fill_cost

    def record_order_cancelled(self, order_id: str):
        """Track an order cancellation."""
        order = self._open_orders.pop(order_id, None)
        if not order:
            return

        # Check if any orders remain for this market
        if order.strategy == StrategyType.MARKET_MAKING:
            remaining = any(
                o.market_id == order.market_id and o.strategy == StrategyType.MARKET_MAKING
                for o in self._open_orders.values()
            )
            if not remaining:
                self._mm_markets.discard(order.market_id)

    def record_edge_entry(self, market_id: str):
        """Record that an edge position was opened."""
        self._edge_markets.add(market_id)

    def record_edge_exit(self, market_id: str):
        """Record that an edge position was closed."""
        self._edge_markets.discard(market_id)

    def record_arb_entry(self, market_id: str, cost: float):
        """Record that an arb position was opened."""
        self._arb_markets.add(market_id)
        self._arb_exposure += cost

    def record_arb_exit(self, market_id: str, cost: float):
        """Record that an arb position was closed."""
        self._arb_markets.discard(market_id)
        self._arb_exposure = max(0, self._arb_exposure - cost)

    def record_mm_exit(self, market_id: str, cost: float):
        """Record that an MM position was closed."""
        self._mm_exposure = max(0, self._mm_exposure - cost)
        # Don't remove from _mm_markets — quotes may still be active

    def cancel_mm_quotes(self, market_id: str):
        """Mark all MM quotes for a market as needing cancellation."""
        to_remove = [
            oid for oid, o in self._open_orders.items()
            if o.market_id == market_id and o.strategy == StrategyType.MARKET_MAKING
        ]
        for oid in to_remove:
            del self._open_orders[oid]
        if to_remove:
            self._mm_markets.discard(market_id)
        return to_remove

    def summary(self) -> dict:
        """Current coordinator state for monitoring."""
        return {
            "edge_markets": len(self._edge_markets),
            "arb_markets": len(self._arb_markets),
            "mm_markets": len(self._mm_markets),
            "mm_exposure": round(self._mm_exposure, 2),
            "arb_exposure": round(self._arb_exposure, 2),
            "open_orders": len(self._open_orders),
            "max_mm_markets": self.max_mm_markets,
            "max_mm_exposure": self.max_mm_exposure,
            "max_arb_exposure": self.max_arb_exposure,
            "degraded_mode": self._degraded,
        }
