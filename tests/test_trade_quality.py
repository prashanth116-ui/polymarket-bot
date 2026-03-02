"""Tests for trade quality filters: min_ev, disagreement gate, edge_decay hold time."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone, timedelta
from core.types import Market, Outcome, Position, ProbabilityEstimate, Side, StrategyType
from models.base import ProbabilityModel
from models.ensemble import EnsembleModel
from strategies.edge_strategy import EdgeStrategy


# --- Helpers ---

class StubModel(ProbabilityModel):
    """Returns a fixed probability."""

    def __init__(self, model_name: str = "stub", prob: float = 0.70, confidence: float = 0.8):
        self._name = model_name
        self._prob = prob
        self._conf = confidence

    @property
    def name(self) -> str:
        return self._name

    def predict(self, market, outcome, context=None, **kwargs):
        return ProbabilityEstimate(
            market_id=market.condition_id,
            outcome=outcome,
            probability=self._prob,
            confidence=self._conf,
            reasoning=f"Stub: {self._prob}",
            model_name=self._name,
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


def _make_position(entry_price=0.50, size=100, opened_minutes_ago=5):
    return Position(
        market_id="test-market",
        condition_id="test-market",
        outcome=Outcome.YES,
        token_id="token_yes",
        side=Side.BUY,
        entry_price=entry_price,
        size=size,
        cost_basis=entry_price * size,
        opened_at=datetime.now(timezone.utc) - timedelta(minutes=opened_minutes_ago),
    )


# --- min_ev tests ---

def test_min_ev_rejects_low_ev():
    """Small edge + small bankroll -> EV < $2 -> rejected."""
    # Model says 58%, market at 50% -> ~7% edge after fees
    # With small bankroll ($100), Kelly sizes small -> low EV
    model = StubModel(prob=0.58)
    strategy = EdgeStrategy(model, min_edge=0.05, bankroll=100, min_ev=2.0)
    market = _make_market(price_yes=0.50)

    signal = strategy.evaluate(market)
    assert signal is None


def test_min_ev_allows_high_ev():
    """Large edge + decent bankroll -> EV > $2 -> passes."""
    # Model says 75%, market at 50% -> ~24% edge -> large size -> high EV
    model = StubModel(prob=0.75)
    strategy = EdgeStrategy(model, min_edge=0.05, bankroll=1000, min_ev=2.0)
    market = _make_market(price_yes=0.50)

    signal = strategy.evaluate(market)
    assert signal is not None
    assert signal.metadata["expected_value"] >= 2.0


def test_min_ev_zero_backwards_compat():
    """min_ev=0 (default) doesn't reject anything that passes edge check."""
    model = StubModel(prob=0.58)
    strategy = EdgeStrategy(model, min_edge=0.05, bankroll=100, min_ev=0.0)
    market = _make_market(price_yes=0.50)

    signal = strategy.evaluate(market)
    # With 0.0 min_ev, even small EV should pass (as long as edge passes)
    assert signal is not None


# --- disagreement tests ---

def test_disagreement_rejects_divergent_models():
    """Models at 0.90 vs 0.30 -> high disagreement -> rejected."""
    m1 = StubModel("model_a", prob=0.90, confidence=0.8)
    m2 = StubModel("model_b", prob=0.30, confidence=0.8)
    ensemble = EnsembleModel(models=[m1, m2], max_disagreement=0.15)
    market = _make_market()

    estimate = ensemble.predict(market, Outcome.YES)
    assert estimate is None


def test_disagreement_allows_agreement():
    """Models at 0.70 vs 0.72 -> low disagreement -> passes."""
    m1 = StubModel("model_a", prob=0.70, confidence=0.8)
    m2 = StubModel("model_b", prob=0.72, confidence=0.8)
    ensemble = EnsembleModel(models=[m1, m2], max_disagreement=0.15)
    market = _make_market()

    estimate = ensemble.predict(market, Outcome.YES)
    assert estimate is not None
    assert abs(estimate.probability - 0.71) < 0.02


def test_disagreement_default_never_rejects():
    """max_disagreement=1.0 (default) never rejects, even with huge disagreement."""
    m1 = StubModel("model_a", prob=0.95, confidence=0.8)
    m2 = StubModel("model_b", prob=0.10, confidence=0.8)
    ensemble = EnsembleModel(models=[m1, m2], max_disagreement=1.0)
    market = _make_market()

    estimate = ensemble.predict(market, Outcome.YES)
    assert estimate is not None


# --- edge_decay hold time tests ---

def test_edge_decay_respects_hold_time():
    """Edge decay fires on checks but position held < min_hold -> no exit."""
    # Model returns low edge (below decay threshold)
    model = StubModel(prob=0.52)  # 52% model, 50% market -> ~1% edge (below 3% decay threshold)
    strategy = EdgeStrategy(
        model, min_edge=0.05, bankroll=1000,
        exit_config={"edge_decay_checks": 3, "min_hold_minutes": 30},
    )
    market = _make_market(price_yes=0.50)

    # Position opened 10 minutes ago (< 30 min hold requirement)
    pos = _make_position(entry_price=0.50, size=100, opened_minutes_ago=10)

    # Simulate enough consecutive low-edge checks
    pos.low_edge_consecutive = 5  # Well above the 3-check threshold

    exit_signal = strategy.check_exit(market, pos)
    # Should NOT exit because hold time < min_hold_minutes
    assert exit_signal is None or exit_signal.metadata.get("exit_reason") != "edge_decay"


def test_edge_decay_fires_after_hold():
    """Edge decay fires after both check count AND hold time met."""
    model = StubModel(prob=0.52)  # Low edge
    strategy = EdgeStrategy(
        model, min_edge=0.05, bankroll=1000,
        exit_config={"edge_decay_checks": 3, "min_hold_minutes": 30},
    )
    market = _make_market(price_yes=0.50)

    # Position opened 60 minutes ago (> 30 min hold requirement)
    pos = _make_position(entry_price=0.50, size=100, opened_minutes_ago=60)

    # Simulate enough consecutive low-edge checks
    pos.low_edge_consecutive = 5

    exit_signal = strategy.check_exit(market, pos)
    assert exit_signal is not None
    assert exit_signal.metadata.get("exit_reason") == "edge_decay"
