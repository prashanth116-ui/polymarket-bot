"""Tests for edge-based trading strategy."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone, timedelta
from core.types import Market, Outcome, Position, ProbabilityEstimate, Side, StrategyType
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


def test_edge_found():
    """Model thinks 70%, market says 50% -> 20% edge -> signal."""
    model = StubModel(prob=0.70)
    strategy = EdgeStrategy(model, min_edge=0.05, bankroll=1000)
    market = _make_market(price_yes=0.50)

    signal = strategy.evaluate(market)
    assert signal is not None
    assert signal.outcome == Outcome.YES
    assert signal.edge >= 0.19
    assert signal.size > 0


def test_no_edge():
    """Model agrees with market -> no signal."""
    model = StubModel(prob=0.50)
    strategy = EdgeStrategy(model, min_edge=0.05, bankroll=1000)
    market = _make_market(price_yes=0.50)

    signal = strategy.evaluate(market)
    assert signal is None


def test_edge_below_threshold():
    """Model thinks 53%, market says 50% -> 3% edge < 5% threshold."""
    model = StubModel(prob=0.53)
    strategy = EdgeStrategy(model, min_edge=0.05, bankroll=1000)
    market = _make_market(price_yes=0.50)

    signal = strategy.evaluate(market)
    assert signal is None


def test_low_confidence_rejected():
    """Low confidence predictions should be rejected."""
    model = StubModel(prob=0.80, confidence=0.3)
    strategy = EdgeStrategy(model, min_edge=0.05, min_confidence=0.6, bankroll=1000)
    market = _make_market(price_yes=0.50)

    signal = strategy.evaluate(market)
    assert signal is None


def test_low_liquidity_rejected():
    """Markets with insufficient liquidity should be skipped."""
    model = StubModel(prob=0.80)
    strategy = EdgeStrategy(model, min_edge=0.05, min_liquidity=500, bankroll=1000)
    market = _make_market(price_yes=0.50, liquidity=100)

    signal = strategy.evaluate(market)
    assert signal is None


def test_near_resolution_rejected():
    """Markets resolving within 24h should be skipped."""
    model = StubModel(prob=0.80)
    strategy = EdgeStrategy(model, min_edge=0.05, bankroll=1000)
    market = _make_market(price_yes=0.50, hours=12)

    signal = strategy.evaluate(market)
    assert signal is None


def test_extreme_price_rejected():
    """Prices above 0.95 should be skipped."""
    model = StubModel(prob=0.99)
    strategy = EdgeStrategy(model, min_edge=0.01, bankroll=1000)
    market = _make_market(price_yes=0.96)

    signal = strategy.evaluate(market)
    assert signal is None


def test_kelly_sizing():
    """Position size should follow Kelly criterion."""
    model = StubModel(prob=0.70)
    strategy = EdgeStrategy(model, min_edge=0.05, kelly_mult=0.25, bankroll=1000, max_position=200)
    market = _make_market(price_yes=0.50)

    signal = strategy.evaluate(market)
    assert signal is not None
    assert signal.size > 0
    assert signal.size <= 200  # Respects max_position


def test_position_size_respects_max():
    """Large edge + large bankroll should be capped at max_position."""
    model = StubModel(prob=0.90)
    strategy = EdgeStrategy(model, min_edge=0.01, kelly_mult=0.25, bankroll=100000, max_position=100)
    market = _make_market(price_yes=0.10)

    signal = strategy.evaluate(market)
    assert signal is not None
    assert signal.size <= 100


def test_no_token_ids_rejected():
    """Markets without token IDs can't be traded."""
    model = StubModel(prob=0.80)
    strategy = EdgeStrategy(model, min_edge=0.05, bankroll=1000)
    market = _make_market(price_yes=0.50)
    market.tokens = {}

    signal = strategy.evaluate(market)
    assert signal is None


def test_inactive_market_rejected():
    model = StubModel(prob=0.80)
    strategy = EdgeStrategy(model, min_edge=0.05, bankroll=1000)
    market = _make_market(price_yes=0.50)
    market.active = False

    signal = strategy.evaluate(market)
    assert signal is None


def test_evaluates_both_outcomes():
    """Strategy should check both YES and NO for edge."""
    # Model thinks 30% YES (= 70% NO), market says 50% YES (50% NO)
    # Edge on NO: 0.70 - 0.50 = 0.20
    model = StubModel(prob=0.30)
    strategy = EdgeStrategy(model, min_edge=0.05, bankroll=1000)
    market = _make_market(price_yes=0.50)

    # YES=0.30, NO=0.70. NO has 20% edge against 0.50 market price.
    signal = strategy.evaluate(market)
    assert signal is not None
    assert signal.outcome == Outcome.NO
    assert signal.edge > 0.15  # ~20% edge


def test_exit_stop_loss():
    """Position down >30% should trigger stop loss."""
    model = StubModel(prob=0.40)  # Model revised down
    strategy = EdgeStrategy(model, bankroll=1000)
    market = _make_market(price_yes=0.35)  # Market dropped

    pos = Position(
        market_id="test-market",
        condition_id="test-market",
        outcome=Outcome.YES,
        token_id="token_yes",
        side=Side.BUY,
        entry_price=0.50,
        size=20,
        cost_basis=10.0,
    )

    signal = strategy.check_exit(market, pos)
    assert signal is not None
    assert "stop loss" in signal.reasoning.lower() or "edge gone" in signal.reasoning.lower()


def test_exit_take_profit():
    """Position up >50% should trigger take profit."""
    model = StubModel(prob=0.85)
    strategy = EdgeStrategy(model, bankroll=1000)
    market = _make_market(price_yes=0.80)

    pos = Position(
        market_id="test-market",
        condition_id="test-market",
        outcome=Outcome.YES,
        token_id="token_yes",
        side=Side.BUY,
        entry_price=0.50,
        size=20,
        cost_basis=10.0,
    )

    signal = strategy.check_exit(market, pos)
    assert signal is not None
    assert "take profit" in signal.reasoning.lower()


def test_no_exit_when_edge_holds():
    """Healthy position with edge should NOT exit."""
    model = StubModel(prob=0.70)
    strategy = EdgeStrategy(model, min_edge=0.05, bankroll=1000)
    market = _make_market(price_yes=0.55)

    pos = Position(
        market_id="test-market",
        condition_id="test-market",
        outcome=Outcome.YES,
        token_id="token_yes",
        side=Side.BUY,
        entry_price=0.50,
        size=20,
        cost_basis=10.0,
    )

    signal = strategy.check_exit(market, pos)
    assert signal is None


def test_update_bankroll():
    model = StubModel(prob=0.70)
    strategy = EdgeStrategy(model, bankroll=1000)
    strategy.update_bankroll(5000)
    assert strategy.bankroll == 5000


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
