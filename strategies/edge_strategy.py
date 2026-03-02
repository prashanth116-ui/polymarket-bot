"""Edge-based trading strategy.

Buy when the model's probability estimate exceeds the market price
by at least min_edge. Position sized with quarter-Kelly.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from core.constants import (
    DEFAULT_KELLY_FRACTION,
    DEFAULT_MAX_POSITION_SIZE,
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_MIN_EDGE,
    DEFAULT_MIN_LIQUIDITY,
    MAX_PRICE_FOR_BUY,
    MIN_HOURS_TO_RESOLUTION,
    MIN_PRICE_FOR_BUY,
)
from core.kelly import expected_value, kelly_fraction, size_position
from core.types import (
    ExitReason,
    Market,
    Outcome,
    Position,
    Signal,
    SignalAction,
    StrategyType,
)
from models.base import ProbabilityModel
from models.ensemble import EnsembleModel

logger = logging.getLogger(__name__)


class EdgeStrategy:
    """Buy when model_prob > market_price + min_edge.

    Uses Kelly criterion for position sizing with fractional Kelly.
    """

    def __init__(
        self,
        model: ProbabilityModel,
        min_edge: float = DEFAULT_MIN_EDGE,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        min_liquidity: float = DEFAULT_MIN_LIQUIDITY,
        kelly_mult: float = DEFAULT_KELLY_FRACTION,
        max_position: float = DEFAULT_MAX_POSITION_SIZE,
        bankroll: float = 1000.0,
    ):
        self.model = model
        self.min_edge = min_edge
        self.min_confidence = min_confidence
        self.min_liquidity = min_liquidity
        self.kelly_mult = kelly_mult
        self.max_position = max_position
        self.bankroll = bankroll

    @property
    def strategy_type(self) -> StrategyType:
        return StrategyType.EDGE

    @property
    def name(self) -> str:
        return "edge"

    def evaluate(
        self,
        market: Market,
        context: dict = None,
    ) -> Optional[Signal]:
        """Evaluate both YES and NO outcomes, return signal for the best edge.

        Returns:
            Signal if edge found, None otherwise
        """
        # Pre-filters
        if not self._passes_filters(market):
            return None

        best_signal = None
        best_edge = 0.0

        for outcome in [Outcome.YES, Outcome.NO]:
            signal = self._evaluate_outcome(market, outcome, context)
            if signal and signal.edge > best_edge:
                best_signal = signal
                best_edge = signal.edge

        return best_signal

    def _evaluate_outcome(
        self,
        market: Market,
        outcome: Outcome,
        context: dict = None,
    ) -> Optional[Signal]:
        """Evaluate a single outcome for edge."""
        market_price = market.last_price_yes if outcome == Outcome.YES else market.last_price_no

        # Skip extreme prices
        if market_price < MIN_PRICE_FOR_BUY or market_price > MAX_PRICE_FOR_BUY:
            return None

        # Get model estimate
        estimate = self.model.predict(market, outcome, context)
        if estimate is None:
            return None

        # Check confidence
        if estimate.confidence < self.min_confidence:
            return None

        # Calculate edge
        edge = estimate.probability - market_price
        if edge < self.min_edge:
            return None

        # Calculate position size via Kelly
        size_usd = size_position(
            bankroll=self.bankroll,
            true_prob=estimate.probability,
            market_price=market_price,
            kelly_mult=self.kelly_mult,
            max_position=self.max_position,
        )

        if size_usd < 1.0:  # Minimum $1 trade
            return None

        # Calculate expected value
        shares = size_usd / market_price
        ev = expected_value(estimate.probability, market_price, shares)

        logger.info(
            f"Edge found: {market.question[:40]}... {outcome.value} "
            f"model={estimate.probability:.1%} market={market_price:.1%} "
            f"edge={edge:.1%} size=${size_usd:.2f} EV=${ev:.2f}"
        )

        return Signal(
            market_id=market.condition_id,
            action=SignalAction.BUY,
            outcome=outcome,
            strategy=StrategyType.EDGE,
            price=market_price,
            size=size_usd,
            edge=edge,
            confidence=estimate.confidence,
            reasoning=estimate.reasoning,
            metadata={
                "model_prob": estimate.probability,
                "market_price": market_price,
                "kelly_fraction": kelly_fraction(estimate.probability, market_price),
                "expected_value": ev,
                "model_name": estimate.model_name,
            },
        )

    def check_exit(
        self,
        market: Market,
        position: Position,
        context: dict = None,
    ) -> Optional[Signal]:
        """Check if an existing position should be exited.

        Exit conditions:
        1. Edge gone — model estimate moved or market moved to our price
        2. Stop loss — position down > stop_loss_pct
        3. Take profit — position up > take_profit_pct
        4. Near resolution with uncertainty
        """
        market_price = market.last_price_yes if position.outcome == Outcome.YES else market.last_price_no
        position.update_pnl(market_price)

        exit_reason = None
        exit_reasoning = ""

        # 1. Check edge — re-run model
        estimate = self.model.predict(market, position.outcome, context)
        if estimate:
            current_edge = estimate.probability - market_price
            if current_edge < self.min_edge * 0.4:  # Edge less than 40% of min threshold
                exit_reason = ExitReason.EDGE_GONE
                exit_reasoning = (
                    f"Edge gone: model={estimate.probability:.1%} "
                    f"market={market_price:.1%} edge={current_edge:.1%}"
                )

        # 2. Stop loss — 30% loss
        if not exit_reason and position.cost_basis > 0:
            loss_pct = -position.unrealized_pnl / position.cost_basis
            if loss_pct > 0.30:
                exit_reason = ExitReason.STOP_LOSS
                exit_reasoning = f"Stop loss: {loss_pct:.1%} loss (${position.unrealized_pnl:.2f})"

        # 3. Take profit — 50% gain
        if not exit_reason and position.cost_basis > 0:
            gain_pct = position.unrealized_pnl / position.cost_basis
            if gain_pct > 0.50:
                exit_reason = ExitReason.TAKE_PROFIT
                exit_reasoning = f"Take profit: {gain_pct:.1%} gain (${position.unrealized_pnl:.2f})"

        # 4. Near resolution with uncertainty
        if not exit_reason:
            hours = market.hours_to_resolution
            if hours is not None and hours < 6 and 0.25 < market_price < 0.75:
                exit_reason = ExitReason.NEAR_RESOLUTION
                exit_reasoning = f"Near resolution ({hours:.1f}h) with uncertain price ({market_price:.1%})"

        if not exit_reason:
            return None

        logger.info(f"Exit signal: {exit_reasoning}")

        return Signal(
            market_id=market.condition_id,
            action=SignalAction.EXIT,
            outcome=position.outcome,
            strategy=StrategyType.EDGE,
            price=market_price,
            size=position.size,
            edge=0.0,
            confidence=1.0,
            reasoning=exit_reasoning,
            metadata={
                "exit_reason": exit_reason.value,
                "unrealized_pnl": position.unrealized_pnl,
                "entry_price": position.entry_price,
                "current_price": market_price,
            },
        )

    def _passes_filters(self, market: Market) -> bool:
        """Pre-filters before running the model (saves LLM cost)."""
        if not market.active:
            return False

        if market.liquidity < self.min_liquidity:
            return False

        # Don't trade markets resolving too soon
        hours = market.hours_to_resolution
        if hours is not None and hours < MIN_HOURS_TO_RESOLUTION:
            return False

        # Need token IDs for trading
        if not market.yes_token_id or not market.no_token_id:
            return False

        return True

    def update_bankroll(self, bankroll: float):
        """Update bankroll for position sizing."""
        self.bankroll = bankroll
