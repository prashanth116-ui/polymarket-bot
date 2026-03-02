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

        Exit conditions (checked in order):
        1. Edge gone — model edge < 40% of threshold
        2. Edge decay — edge < 3% for 3 consecutive checks
        3. Trailing stop — after 20%+ gain, exit if unrealized drops below 50% of peak
        4. Dynamic stop loss — 20% for small-edge (<8%), 30% for large-edge
        5. Time-based take profit — tightens as resolution approaches
        6. Near resolution with uncertainty
        """
        market_price = market.last_price_yes if position.outcome == Outcome.YES else market.last_price_no
        position.update_pnl(market_price)

        # Track peak unrealized P/L for trailing stop
        if position.unrealized_pnl > position.peak_unrealized_pnl:
            position.peak_unrealized_pnl = position.unrealized_pnl

        exit_reason = None
        exit_reasoning = ""

        # 1. Edge gone — re-run model
        current_edge = None
        estimate = self.model.predict(market, position.outcome, context)
        if estimate:
            current_edge = estimate.probability - market_price
            if current_edge < self.min_edge * 0.4:  # Edge less than 40% of min threshold
                exit_reason = ExitReason.EDGE_GONE
                exit_reasoning = (
                    f"Edge gone: model={estimate.probability:.1%} "
                    f"market={market_price:.1%} edge={current_edge:.1%}"
                )

        # 2. Edge decay — edge < 3% for 3 consecutive checks
        if not exit_reason and current_edge is not None:
            if current_edge < 0.03:
                position.low_edge_consecutive += 1
                if position.low_edge_consecutive >= 3:
                    exit_reason = ExitReason.EDGE_DECAY
                    exit_reasoning = (
                        f"Edge decay: edge={current_edge:.1%} below 3% "
                        f"for {position.low_edge_consecutive} consecutive checks"
                    )
            else:
                position.low_edge_consecutive = 0

        # 3. Trailing stop — after 20%+ gain, trail at 50% of peak
        if not exit_reason and position.cost_basis > 0:
            gain_pct = position.unrealized_pnl / position.cost_basis
            peak_pct = position.peak_unrealized_pnl / position.cost_basis
            if peak_pct > 0.20 and position.unrealized_pnl < position.peak_unrealized_pnl * 0.50:
                exit_reason = ExitReason.TRAILING_STOP
                exit_reasoning = (
                    f"Trailing stop: peak=${position.peak_unrealized_pnl:.2f} "
                    f"({peak_pct:.1%}), current=${position.unrealized_pnl:.2f} "
                    f"({gain_pct:.1%}), below 50% of peak"
                )

        # 4. Dynamic stop loss — tighter for small-edge trades
        if not exit_reason and position.cost_basis > 0:
            loss_pct = -position.unrealized_pnl / position.cost_basis
            # Small-edge trades (<8% entry edge) get tighter 20% stop
            entry_edge = position.entry_price  # approximate; exact edge not stored
            stop_threshold = 0.20 if (current_edge is not None and current_edge < 0.08) else 0.30
            if loss_pct > stop_threshold:
                exit_reason = ExitReason.STOP_LOSS
                exit_reasoning = (
                    f"Stop loss: {loss_pct:.1%} loss "
                    f"(${position.unrealized_pnl:.2f}, threshold={stop_threshold:.0%})"
                )

        # 5. Time-based take profit — tightens approaching resolution
        if not exit_reason and position.cost_basis > 0:
            gain_pct = position.unrealized_pnl / position.cost_basis
            hours = market.hours_to_resolution
            # Tighten TP as resolution approaches
            if hours is not None and hours < 24:
                tp_threshold = 0.15
            elif hours is not None and hours < 72:
                tp_threshold = 0.30
            else:
                tp_threshold = 0.50
            if gain_pct > tp_threshold:
                exit_reason = ExitReason.TAKE_PROFIT
                exit_reasoning = (
                    f"Take profit: {gain_pct:.1%} gain "
                    f"(${position.unrealized_pnl:.2f}, threshold={tp_threshold:.0%}"
                    f"{f', {hours:.0f}h to resolution' if hours else ''})"
                )

        # 6. Near resolution with uncertainty
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
                "peak_unrealized_pnl": position.peak_unrealized_pnl,
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
