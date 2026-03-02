"""Edge-based trading strategy.

Buy when the model's probability estimate exceeds the market price
by at least min_edge. Position sized with quarter-Kelly.
"""

import logging
import time
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
    TAKER_FEE_BPS,
)
from core.kelly import expected_value, kelly_fraction, size_position
from core.types import (
    ExitReason,
    Market,
    Outcome,
    Position,
    ProbabilityEstimate,
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
        exit_config: dict = None,
    ):
        self.model = model
        self.min_edge = min_edge
        self.min_confidence = min_confidence
        self.min_liquidity = min_liquidity
        self.kelly_mult = kelly_mult
        self.max_position = max_position
        self.bankroll = bankroll

        # Exit tuning (configurable via settings.yaml)
        ec = exit_config or {}
        self.trailing_trigger = ec.get("trailing_stop_trigger", 0.20)
        self.trailing_retracement = ec.get("trailing_stop_retracement", 0.50)
        self.stop_tight = ec.get("stop_loss_tight", 0.20)
        self.stop_wide = ec.get("stop_loss_wide", 0.30)
        self.edge_decay_threshold = ec.get("edge_decay_threshold", 0.03)
        self.edge_decay_checks = ec.get("edge_decay_checks", 3)
        self.tp_near_pct = ec.get("tp_near_pct", 0.15)
        self.tp_mid_pct = ec.get("tp_mid_pct", 0.30)
        self.tp_far_pct = ec.get("tp_far_pct", 0.50)

        # Exit prediction cache (avoid re-calling LLM every minute for same market)
        self._exit_cache: dict[str, tuple[float, ProbabilityEstimate]] = {}
        self._exit_cache_ttl = 120  # seconds — cache exit predictions for 2 min

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
        tier: str = "screening",
    ) -> Optional[Signal]:
        """Evaluate both YES and NO outcomes, return signal for the best edge.

        Estimates YES probability once, derives NO = 1 - YES for consistency.

        Returns:
            Signal if edge found, None otherwise
        """
        # Pre-filters
        if not self._passes_filters(market):
            return None

        # Get YES estimate once — derive NO from it
        yes_estimate = self.model.predict(market, Outcome.YES, context, tier=tier)
        if yes_estimate is None:
            return None

        if yes_estimate.confidence < self.min_confidence:
            return None

        yes_prob = yes_estimate.probability
        no_prob = 1.0 - yes_prob

        best_signal = None
        best_edge = 0.0

        # Check YES side
        signal = self._evaluate_with_prob(
            market, Outcome.YES, yes_prob, yes_estimate, context,
        )
        if signal and signal.edge > best_edge:
            best_signal = signal
            best_edge = signal.edge

        # Check NO side (derived probability)
        signal = self._evaluate_with_prob(
            market, Outcome.NO, no_prob, yes_estimate, context,
        )
        if signal and signal.edge > best_edge:
            best_signal = signal
            best_edge = signal.edge

        return best_signal

    def _evaluate_with_prob(
        self,
        market: Market,
        outcome: Outcome,
        model_prob: float,
        estimate: ProbabilityEstimate,
        context: dict = None,
    ) -> Optional[Signal]:
        """Evaluate a single outcome using a pre-computed probability."""
        market_price = market.last_price_yes if outcome == Outcome.YES else market.last_price_no

        # Skip extreme prices
        if market_price < MIN_PRICE_FOR_BUY or market_price > MAX_PRICE_FOR_BUY:
            return None

        # Calculate edge (fee-adjusted to match Kelly sizing)
        fee_rate = TAKER_FEE_BPS / 10000
        effective_price = market_price * (1 + fee_rate)
        edge = model_prob - effective_price
        if edge < self.min_edge:
            return None

        # Calculate position size via Kelly
        size_usd = size_position(
            bankroll=self.bankroll,
            true_prob=model_prob,
            market_price=market_price,
            kelly_mult=self.kelly_mult,
            max_position=self.max_position,
        )

        if size_usd < 1.0:  # Minimum $1 trade
            return None

        # Calculate expected value
        shares = size_usd / market_price
        ev = expected_value(model_prob, market_price, shares)

        logger.info(
            f"Edge found: {market.question[:40]}... {outcome.value} "
            f"model={model_prob:.1%} market={market_price:.1%} "
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
                "model_prob": model_prob,
                "market_price": market_price,
                "kelly_fraction": kelly_fraction(model_prob, market_price),
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

        # 1. Edge gone — re-run model (with 2-min cache to reduce API calls)
        current_edge = None
        cache_key = f"{market.condition_id}:{position.outcome.value}"
        cached = self._exit_cache.get(cache_key)
        if cached and time.time() - cached[0] < self._exit_cache_ttl:
            estimate = cached[1]
        else:
            estimate = self.model.predict(market, position.outcome, context)
            if estimate:
                self._exit_cache[cache_key] = (time.time(), estimate)
        if estimate:
            current_edge = estimate.probability - market_price
            if current_edge < self.min_edge * 0.4:  # Edge less than 40% of min threshold
                exit_reason = ExitReason.EDGE_GONE
                exit_reasoning = (
                    f"Edge gone: model={estimate.probability:.1%} "
                    f"market={market_price:.1%} edge={current_edge:.1%}"
                )

        # 2. Edge decay — edge below threshold for N consecutive checks
        if not exit_reason and current_edge is not None:
            if current_edge < self.edge_decay_threshold:
                position.low_edge_consecutive += 1
                if position.low_edge_consecutive >= self.edge_decay_checks:
                    exit_reason = ExitReason.EDGE_DECAY
                    exit_reasoning = (
                        f"Edge decay: edge={current_edge:.1%} below {self.edge_decay_threshold:.0%} "
                        f"for {position.low_edge_consecutive} consecutive checks"
                    )
            else:
                position.low_edge_consecutive = 0

        # 3. Trailing stop — after 20%+ gain, trail at 50% of peak
        if not exit_reason and position.cost_basis > 0.01:
            gain_pct = position.unrealized_pnl / position.cost_basis
            peak_pct = position.peak_unrealized_pnl / position.cost_basis
            if peak_pct > self.trailing_trigger and position.unrealized_pnl < position.peak_unrealized_pnl * self.trailing_retracement:
                exit_reason = ExitReason.TRAILING_STOP
                exit_reasoning = (
                    f"Trailing stop: peak=${position.peak_unrealized_pnl:.2f} "
                    f"({peak_pct:.1%}), current=${position.unrealized_pnl:.2f} "
                    f"({gain_pct:.1%}), below {self.trailing_retracement:.0%} of peak"
                )

        # 4. Dynamic stop loss — tighter for small-edge trades
        if not exit_reason and position.cost_basis > 0.01:
            loss_pct = -position.unrealized_pnl / position.cost_basis
            # Small-edge trades (<8% entry edge) get tighter stop
            stop_threshold = self.stop_tight if position.entry_edge < 0.08 else self.stop_wide
            if loss_pct > stop_threshold:
                exit_reason = ExitReason.STOP_LOSS
                exit_reasoning = (
                    f"Stop loss: {loss_pct:.1%} loss "
                    f"(${position.unrealized_pnl:.2f}, threshold={stop_threshold:.0%})"
                )

        # 5. Time-based take profit — tightens approaching resolution
        if not exit_reason and position.cost_basis > 0.01:
            gain_pct = position.unrealized_pnl / position.cost_basis
            hours = market.hours_to_resolution
            # Tighten TP as resolution approaches
            if hours is not None and hours < 24:
                tp_threshold = self.tp_near_pct
            elif hours is not None and hours < 72:
                tp_threshold = self.tp_mid_pct
            else:
                tp_threshold = self.tp_far_pct
            if gain_pct > tp_threshold:
                exit_reason = ExitReason.TAKE_PROFIT
                exit_reasoning = (
                    f"Take profit: {gain_pct:.1%} gain "
                    f"(${position.unrealized_pnl:.2f}, threshold={tp_threshold:.0%}"
                    f"{f', {hours:.0f}h to resolution' if hours else ''})"
                )

        # 6. Near resolution with uncertainty — only if not meaningfully profitable
        if not exit_reason and position.cost_basis > 0.01:
            hours = market.hours_to_resolution
            gain_pct_nr = position.unrealized_pnl / position.cost_basis
            if hours is not None and hours < 6 and 0.25 < market_price < 0.75 and gain_pct_nr < 0.10:
                exit_reason = ExitReason.NEAR_RESOLUTION
                exit_reasoning = (
                    f"Near resolution ({hours:.1f}h) with uncertain price ({market_price:.1%})"
                    f" and low gain ({gain_pct_nr:.1%})"
                )

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
