"""Tests for ensemble model and statistical models."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone, timedelta
from core.types import Market, Outcome, ProbabilityEstimate
from models.base import ProbabilityModel
from models.ensemble import EnsembleModel
from models.statistical import MarketImpliedModel, BaseRateModel, TimeDecayModel


# --- Stub model for testing ---

class StubModel(ProbabilityModel):
    """Returns a fixed probability for testing."""

    def __init__(self, model_name: str, probability: float, confidence: float = 0.8):
        self._name = model_name
        self._prob = probability
        self._conf = confidence

    @property
    def name(self) -> str:
        return self._name

    def predict(self, market, outcome, context=None):
        return ProbabilityEstimate(
            market_id=market.condition_id,
            outcome=outcome,
            probability=self._prob,
            confidence=self._conf,
            reasoning=f"Stub prediction: {self._prob}",
            model_name=self._name,
        )


def _make_market(price_yes=0.50, liquidity=1000, category="politics", hours=168):
    end_date = datetime.now(timezone.utc) + timedelta(hours=hours) if hours else None
    return Market(
        condition_id="test-market",
        question="Will X happen?",
        description="Test market description",
        category=category,
        end_date=end_date,
        tokens={"YES": "token_yes", "NO": "token_no"},
        last_price_yes=price_yes,
        last_price_no=1 - price_yes,
        liquidity=liquidity,
        active=True,
    )


# --- Ensemble tests ---

def test_ensemble_single_model():
    m1 = StubModel("model_a", 0.70, 0.8)
    ensemble = EnsembleModel([m1])
    market = _make_market()

    est = ensemble.predict(market, Outcome.YES)
    assert est is not None
    assert abs(est.probability - 0.70) < 0.01


def test_ensemble_two_models_equal_weight():
    m1 = StubModel("model_a", 0.60, 0.8)
    m2 = StubModel("model_b", 0.80, 0.8)
    ensemble = EnsembleModel([m1, m2])
    market = _make_market()

    est = ensemble.predict(market, Outcome.YES)
    assert est is not None
    # Equal weights and confidence -> average = 0.70
    assert abs(est.probability - 0.70) < 0.01


def test_ensemble_confidence_weighted():
    """Higher confidence model should have more influence."""
    m1 = StubModel("model_a", 0.60, 0.2)  # Low confidence
    m2 = StubModel("model_b", 0.80, 1.0)  # High confidence
    ensemble = EnsembleModel([m1, m2])
    market = _make_market()

    est = ensemble.predict(market, Outcome.YES)
    assert est is not None
    # model_b has 5x the effective weight -> should be closer to 0.80
    assert est.probability > 0.70


def test_ensemble_custom_weights():
    m1 = StubModel("model_a", 0.60, 0.8)
    m2 = StubModel("model_b", 0.80, 0.8)
    ensemble = EnsembleModel()
    ensemble.add_model(m1, weight=1.0)
    ensemble.add_model(m2, weight=3.0)
    market = _make_market()

    est = ensemble.predict(market, Outcome.YES)
    assert est is not None
    # model_b has 3x weight -> closer to 0.80
    assert est.probability > 0.70


def test_ensemble_no_models():
    ensemble = EnsembleModel([])
    market = _make_market()
    est = ensemble.predict(market, Outcome.YES)
    assert est is None


def test_ensemble_brier_weight_update():
    m1 = StubModel("model_a", 0.70, 0.8)
    m2 = StubModel("model_b", 0.70, 0.8)
    ensemble = EnsembleModel([m1, m2])

    # model_a has good Brier (low), model_b has bad Brier (high)
    ensemble.update_weights_from_brier("model_a", 0.05)
    ensemble.update_weights_from_brier("model_b", 0.40)

    weights = ensemble.get_weights()
    assert weights["model_a"] > weights["model_b"]


def test_ensemble_disagreement():
    m1 = StubModel("model_a", 0.90, 0.8)
    m2 = StubModel("model_b", 0.10, 0.8)
    ensemble = EnsembleModel([m1, m2])
    market = _make_market()

    est = ensemble.predict(market, Outcome.YES)
    assert est is not None
    # High disagreement should lower confidence
    assert est.confidence < 0.5


def test_ensemble_agreement():
    m1 = StubModel("model_a", 0.70, 0.8)
    m2 = StubModel("model_b", 0.72, 0.8)
    ensemble = EnsembleModel([m1, m2])
    market = _make_market()

    est = ensemble.predict(market, Outcome.YES)
    assert est is not None
    assert est.confidence > 0.5


def test_ensemble_model_stats():
    m1 = StubModel("model_a", 0.70, 0.8)
    ensemble = EnsembleModel([m1])
    ensemble.update_weights_from_brier("model_a", 0.10)
    stats = ensemble.get_model_stats()
    assert len(stats) == 1
    assert stats[0]["model"] == "model_a"
    assert stats[0]["n_scores"] == 1


# --- Statistical model tests ---

def test_market_implied():
    model = MarketImpliedModel()
    market = _make_market(price_yes=0.65)
    est = model.predict(market, Outcome.YES)
    assert est is not None
    # Adjusted for FLB but should be close to 0.65
    assert 0.55 < est.probability < 0.70


def test_market_implied_extreme_price():
    model = MarketImpliedModel()
    market = _make_market(price_yes=0.95)
    est = model.predict(market, Outcome.YES)
    assert est is not None
    # FLB adjustment pulls extreme prices toward 0.5 slightly
    assert est.probability < 0.95


def test_market_implied_liquidity_confidence():
    model = MarketImpliedModel()

    low_liq = _make_market(liquidity=50)
    high_liq = _make_market(liquidity=50000)

    est_low = model.predict(low_liq, Outcome.YES)
    est_high = model.predict(high_liq, Outcome.YES)

    assert est_low.confidence < est_high.confidence


def test_base_rate_politics():
    model = BaseRateModel()
    market = _make_market(category="politics")
    market.question = "Will the incumbent be reelected?"
    est = model.predict(market, Outcome.YES)
    assert est is not None
    assert abs(est.probability - 0.60) < 0.01  # incumbent_wins base rate


def test_base_rate_unknown():
    model = BaseRateModel()
    market = _make_market(category="other")
    market.question = "Some random question?"
    est = model.predict(market, Outcome.YES)
    assert est is None  # No matching base rate


def test_time_decay_near_resolution():
    model = TimeDecayModel()
    market = _make_market(price_yes=0.80, hours=12)
    est = model.predict(market, Outcome.YES)
    assert est is not None
    # Near resolution, small adjustment
    assert abs(est.probability - 0.80) < 0.05


def test_time_decay_far_resolution():
    model = TimeDecayModel()
    market = _make_market(price_yes=0.80, hours=1000)
    est = model.predict(market, Outcome.YES)
    assert est is not None
    # Far from resolution, more pull toward 0.5
    assert est.probability < 0.80


def test_time_decay_no_end_date():
    model = TimeDecayModel()
    market = _make_market(hours=None)
    market.end_date = None
    est = model.predict(market, Outcome.YES)
    assert est is None


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
