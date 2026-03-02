"""Tests for position exit optimization — trailing stop, edge decay, dynamic stop, time-based TP."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone, timedelta
from core.types import ExitReason, Market, Outcome, Position, ProbabilityEstimate, Side, StrategyType
from models.base import ProbabilityModel
from strategies.edge_strategy import EdgeStrategy


class StubModel(ProbabilityModel):
    """Returns a fixed probability."""

    def __init__(self, prob: float, confidence: float = 0.8):
        self._prob = prob
        self._conf = confidence

    @property
    def name(self) -> str:
        return "stub"

    def predict(self, market, outcome, context=None, **kwargs):
        return ProbabilityEstimate(
            market_id=market.condition_id,
            outcome=outcome,
            probability=self._prob,
            confidence=self._conf,
            reasoning="Stub",
            model_name="stub",
        )


def _make_market(price_yes=0.50, liquidity=1000, hours=168):
    end_date = datetime.now(timezone.utc) + timedelta(hours=hours)
    return Market(
        condition_id="test-market",
        question="Will X happen?",
        description="Test",
        category="politics",
        end_date=end_date,
        tokens={"YES": "token_yes", "NO": "token_no"},
        last_price_yes=price_yes,
        last_price_no=1 - price_yes,
        liquidity=liquidity,
        active=True,
    )


def _make_position(entry_price=0.50, size=20, cost_basis=10.0, peak_pnl=0.0):
    pos = Position(
        market_id="test-market",
        condition_id="test-market",
        outcome=Outcome.YES,
        token_id="token_yes",
        side=Side.BUY,
        entry_price=entry_price,
        size=size,
        cost_basis=cost_basis,
    )
    pos.peak_unrealized_pnl = peak_pnl
    return pos


# --- Trailing Stop Tests ---

def test_trailing_stop_triggers():
    """Position that gained 30%+ then fell back should trigger trailing stop."""
    model = StubModel(prob=0.60)  # Still some edge
    strategy = EdgeStrategy(model, min_edge=0.05, bankroll=1000)
    market = _make_market(price_yes=0.53)  # Slight gain from 0.50 entry

    pos = _make_position(entry_price=0.50, size=20, cost_basis=10.0)
    # Simulate: price went up to 0.65 (peak pnl = 3.0), now back to 0.53 (pnl = 0.6)
    pos.peak_unrealized_pnl = 3.0  # 30% of cost_basis

    signal = strategy.check_exit(market, pos)
    assert signal is not None
    assert signal.metadata["exit_reason"] == ExitReason.TRAILING_STOP.value


def test_trailing_stop_not_triggered_if_no_peak():
    """Position that never gained 20%+ should not trigger trailing stop."""
    model = StubModel(prob=0.60)
    strategy = EdgeStrategy(model, min_edge=0.05, bankroll=1000)
    market = _make_market(price_yes=0.52)

    pos = _make_position(entry_price=0.50, size=20, cost_basis=10.0)
    pos.peak_unrealized_pnl = 1.0  # Only 10% gain — below 20% threshold

    signal = strategy.check_exit(market, pos)
    assert signal is None


def test_trailing_stop_not_triggered_if_above_50pct_peak():
    """Position still above 50% of peak should not trigger trailing stop."""
    model = StubModel(prob=0.70)  # Enough edge to avoid edge_gone
    strategy = EdgeStrategy(model, min_edge=0.05, bankroll=1000)
    market = _make_market(price_yes=0.62)

    pos = _make_position(entry_price=0.50, size=20, cost_basis=10.0)
    pos.peak_unrealized_pnl = 4.0  # 40% gain peak
    # Current unrealized: (0.62 - 0.50) * 20 = 2.4, which is 60% of peak (above 50%)

    signal = strategy.check_exit(market, pos)
    assert signal is None


def test_peak_pnl_tracks_upward():
    """peak_unrealized_pnl should update when new high is set."""
    model = StubModel(prob=0.70)
    strategy = EdgeStrategy(model, min_edge=0.05, bankroll=1000)
    market = _make_market(price_yes=0.60)

    pos = _make_position(entry_price=0.50, size=20, cost_basis=10.0)
    pos.peak_unrealized_pnl = 1.0

    # check_exit updates pnl: (0.60 - 0.50) * 20 = 2.0 > 1.0 peak
    strategy.check_exit(market, pos)
    assert abs(pos.peak_unrealized_pnl - 2.0) < 0.01


# --- Edge Decay Tests ---

def test_edge_decay_triggers_after_3_checks():
    """Edge below 3% for 3 consecutive checks should trigger edge decay."""
    model = StubModel(prob=0.52)  # 2% edge at 0.50 market price
    strategy = EdgeStrategy(model, min_edge=0.05, bankroll=1000)
    market = _make_market(price_yes=0.50)

    pos = _make_position(entry_price=0.50, size=20, cost_basis=10.0)

    # First two checks: edge < 3% but not enough consecutive
    strategy.check_exit(market, pos)
    assert pos.low_edge_consecutive == 1

    strategy.check_exit(market, pos)
    assert pos.low_edge_consecutive == 2

    # Third check triggers
    signal = strategy.check_exit(market, pos)
    assert signal is not None
    assert signal.metadata["exit_reason"] == ExitReason.EDGE_DECAY.value


def test_edge_decay_resets_on_good_edge():
    """Counter should reset when edge goes back above 3%."""
    # Start with low edge
    model = StubModel(prob=0.52)
    strategy = EdgeStrategy(model, min_edge=0.05, bankroll=1000)
    market = _make_market(price_yes=0.50)
    pos = _make_position()

    strategy.check_exit(market, pos)
    assert pos.low_edge_consecutive == 1

    # Now edge recovers — clear exit cache so new prob is used
    model._prob = 0.58  # 8% edge — above 3%
    strategy._exit_cache.clear()
    strategy.check_exit(market, pos)
    assert pos.low_edge_consecutive == 0


def test_edge_decay_not_triggered_with_good_edge():
    """Edge above 3% should not trigger decay."""
    model = StubModel(prob=0.60)  # 10% edge
    strategy = EdgeStrategy(model, min_edge=0.05, bankroll=1000)
    market = _make_market(price_yes=0.50)
    pos = _make_position()

    signal = strategy.check_exit(market, pos)
    assert signal is None
    assert pos.low_edge_consecutive == 0


# --- Dynamic Stop Loss Tests ---

def test_dynamic_stop_30pct_default():
    """Default 30% stop loss for normal trades."""
    model = StubModel(prob=0.60)  # 8%+ edge
    strategy = EdgeStrategy(model, min_edge=0.05, bankroll=1000)
    market = _make_market(price_yes=0.35)  # 30% loss from 0.50 entry

    pos = _make_position(entry_price=0.50, size=20, cost_basis=10.0)

    signal = strategy.check_exit(market, pos)
    assert signal is not None
    assert signal.metadata["exit_reason"] == ExitReason.STOP_LOSS.value


def test_dynamic_stop_20pct_for_small_edge():
    """Small-edge trades (<8%) should use tighter 20% stop."""
    # Model edge: 0.44 - 0.39 = 5% (< 8% threshold for tighter stop)
    model = StubModel(prob=0.44)
    strategy = EdgeStrategy(model, min_edge=0.02, bankroll=1000)
    # Price dropped 22% from 0.50 entry = 0.39
    market = _make_market(price_yes=0.39)

    pos = _make_position(entry_price=0.50, size=20, cost_basis=10.0)

    signal = strategy.check_exit(market, pos)
    assert signal is not None
    assert signal.metadata["exit_reason"] == ExitReason.STOP_LOSS.value


def test_no_stop_loss_within_threshold():
    """Position within stop threshold should not exit."""
    model = StubModel(prob=0.60)
    strategy = EdgeStrategy(model, min_edge=0.05, bankroll=1000)
    market = _make_market(price_yes=0.45)  # 10% loss — within 30% threshold

    pos = _make_position(entry_price=0.50, size=20, cost_basis=10.0)

    signal = strategy.check_exit(market, pos)
    assert signal is None


# --- Time-Based Take Profit Tests ---

def test_take_profit_50pct_far_from_resolution():
    """Far from resolution (>72h): 50% TP threshold."""
    model = StubModel(prob=0.85)
    strategy = EdgeStrategy(model, min_edge=0.05, bankroll=1000)
    market = _make_market(price_yes=0.80, hours=200)

    pos = _make_position(entry_price=0.50, size=20, cost_basis=10.0)

    signal = strategy.check_exit(market, pos)
    assert signal is not None
    assert signal.metadata["exit_reason"] == ExitReason.TAKE_PROFIT.value


def test_take_profit_30pct_medium_resolution():
    """Medium timeframe (24-72h): 30% TP threshold."""
    model = StubModel(prob=0.72)
    strategy = EdgeStrategy(model, min_edge=0.05, bankroll=1000)
    market = _make_market(price_yes=0.70, hours=48)

    pos = _make_position(entry_price=0.50, size=20, cost_basis=10.0)
    # unrealized: (0.70 - 0.50) * 20 = 4.0, gain_pct = 4.0/10.0 = 40% > 30%

    signal = strategy.check_exit(market, pos)
    assert signal is not None
    assert signal.metadata["exit_reason"] == ExitReason.TAKE_PROFIT.value


def test_take_profit_15pct_near_resolution():
    """Near resolution (<24h): 15% TP threshold."""
    model = StubModel(prob=0.70)  # Enough edge to not trigger edge_gone
    strategy = EdgeStrategy(model, min_edge=0.05, bankroll=1000)
    market = _make_market(price_yes=0.60, hours=12)

    pos = _make_position(entry_price=0.50, size=20, cost_basis=10.0)
    # unrealized: (0.60 - 0.50) * 20 = 2.0, gain_pct = 2.0/10.0 = 20% > 15%

    signal = strategy.check_exit(market, pos)
    assert signal is not None
    assert signal.metadata["exit_reason"] == ExitReason.TAKE_PROFIT.value


def test_no_take_profit_below_threshold():
    """10% gain far from resolution should not trigger 50% TP."""
    model = StubModel(prob=0.70)
    strategy = EdgeStrategy(model, min_edge=0.05, bankroll=1000)
    market = _make_market(price_yes=0.55, hours=200)

    pos = _make_position(entry_price=0.50, size=20, cost_basis=10.0)
    # unrealized: (0.55 - 0.50) * 20 = 1.0, gain_pct = 10% < 50%

    signal = strategy.check_exit(market, pos)
    assert signal is None


# --- Near Resolution (unchanged) ---

def test_near_resolution_exit():
    """Position near resolution with uncertain price should exit."""
    model = StubModel(prob=0.55)
    strategy = EdgeStrategy(model, min_edge=0.05, bankroll=1000)
    market = _make_market(price_yes=0.50, hours=3)

    pos = _make_position()

    signal = strategy.check_exit(market, pos)
    assert signal is not None
    assert signal.metadata["exit_reason"] == ExitReason.NEAR_RESOLUTION.value


# --- Exit Priority Order ---

def test_edge_gone_takes_priority():
    """Edge gone should be checked first and win over other conditions."""
    model = StubModel(prob=0.35)  # Way below market — edge gone
    strategy = EdgeStrategy(model, min_edge=0.05, bankroll=1000)
    market = _make_market(price_yes=0.50)

    pos = _make_position(entry_price=0.50, size=20, cost_basis=10.0)

    signal = strategy.check_exit(market, pos)
    assert signal is not None
    assert signal.metadata["exit_reason"] == ExitReason.EDGE_GONE.value


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
