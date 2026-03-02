"""Tests for the portfolio tracker."""

import pytest

from core.types import Market, Outcome, Position, Side, StrategyType
from risk.portfolio import Portfolio


@pytest.fixture
def portfolio():
    return Portfolio()


def _make_position(
    market_id="m1",
    outcome=Outcome.YES,
    entry_price=0.50,
    size=100.0,
    cost_basis=50.0,
    strategy=StrategyType.EDGE,
):
    return Position(
        market_id=market_id,
        condition_id=market_id,
        outcome=outcome,
        token_id=f"{market_id}-{outcome.value}",
        side=Side.BUY,
        entry_price=entry_price,
        size=size,
        cost_basis=cost_basis,
        current_price=entry_price,
        strategy=strategy,
    )


def _make_market(
    condition_id="m1",
    question="Will X happen?",
    category="politics",
):
    return Market(
        condition_id=condition_id,
        question=question,
        description="",
        category=category,
        end_date=None,
        tokens={"YES": f"{condition_id}-yes", "NO": f"{condition_id}-no"},
    )


def test_add_position(portfolio):
    pos = _make_position()
    portfolio.add_position(pos)
    assert portfolio.position_count == 1
    assert portfolio.total_exposure == 50.0


def test_remove_position(portfolio):
    pos = _make_position()
    portfolio.add_position(pos)
    portfolio.remove_position("m1", Outcome.YES)
    assert portfolio.position_count == 0


def test_has_position(portfolio):
    pos = _make_position()
    portfolio.add_position(pos)
    assert portfolio.has_position("m1") is True
    assert portfolio.has_position("m2") is False


def test_get_position(portfolio):
    pos = _make_position()
    portfolio.add_position(pos)
    got = portfolio.get_position("m1", Outcome.YES)
    assert got is pos
    assert portfolio.get_position("m1", Outcome.NO) is None


def test_update_pnl(portfolio):
    pos = _make_position(entry_price=0.50, size=100.0, cost_basis=50.0)
    portfolio.add_position(pos)
    portfolio.update_position("m1", Outcome.YES, 0.60)
    assert pos.current_price == 0.60
    assert pos.unrealized_pnl == pytest.approx(10.0)


def test_multiple_positions(portfolio):
    p1 = _make_position("m1", Outcome.YES, cost_basis=50.0)
    p2 = _make_position("m2", Outcome.NO, cost_basis=30.0)
    portfolio.add_position(p1)
    portfolio.add_position(p2)
    assert portfolio.position_count == 2
    assert portfolio.total_exposure == 80.0


def test_exposure_by_category(portfolio):
    m1 = _make_market("m1", category="politics")
    m2 = _make_market("m2", category="crypto")
    p1 = _make_position("m1", cost_basis=50.0)
    p2 = _make_position("m2", cost_basis=30.0)
    portfolio.add_position(p1, m1)
    portfolio.add_position(p2, m2)
    by_cat = portfolio.exposure_by_category()
    assert by_cat["politics"] == 50.0
    assert by_cat["crypto"] == 30.0


def test_exposure_by_strategy(portfolio):
    p1 = _make_position("m1", cost_basis=50.0, strategy=StrategyType.EDGE)
    p2 = _make_position("m2", cost_basis=30.0, strategy=StrategyType.ARBITRAGE)
    portfolio.add_position(p1)
    portfolio.add_position(p2)
    by_strat = portfolio.exposure_by_strategy()
    assert by_strat["edge"] == 50.0
    assert by_strat["arbitrage"] == 30.0


def test_remaining_exposure(portfolio):
    pos = _make_position(cost_basis=200.0)
    portfolio.add_position(pos)
    assert portfolio.remaining_exposure(500.0) == 300.0
    assert portfolio.remaining_exposure(100.0) == 0  # Already over cap


def test_remaining_category_exposure(portfolio):
    m = _make_market("m1", category="politics")
    p = _make_position("m1", cost_basis=150.0)
    portfolio.add_position(p, m)
    assert portfolio.remaining_category_exposure("politics", 200.0) == 50.0
    assert portfolio.remaining_category_exposure("crypto", 200.0) == 200.0


def test_find_correlated_by_category(portfolio):
    m1 = _make_market("m1", "Will Biden win?", "politics")
    m2 = _make_market("m2", "Will Trump win?", "politics")
    p1 = _make_position("m1", cost_basis=50.0)
    portfolio.add_position(p1, m1)
    correlated = portfolio.find_correlated(m2)
    assert len(correlated) == 1
    assert correlated[0].market_id == "m1"


def test_find_correlated_by_keywords(portfolio):
    m1 = _make_market("m1", "Will Bitcoin hit 100k?", "crypto")
    m2 = _make_market("m2", "Will Bitcoin hit 200k?", "other")
    p1 = _make_position("m1", cost_basis=50.0)
    portfolio.add_position(p1, m1)
    correlated = portfolio.find_correlated(m2)
    assert len(correlated) == 1


def test_find_correlated_skips_same_market(portfolio):
    m = _make_market("m1", "Test market", "politics")
    p = _make_position("m1", cost_basis=50.0)
    portfolio.add_position(p, m)
    correlated = portfolio.find_correlated(m)
    assert len(correlated) == 0


def test_summary(portfolio):
    m = _make_market("m1", category="politics")
    p = _make_position("m1", cost_basis=50.0, entry_price=0.50, size=100.0)
    portfolio.add_position(p, m)
    s = portfolio.summary()
    assert s["positions"] == 1
    assert s["total_exposure"] == 50.0
    assert "by_category" in s
    assert "by_strategy" in s


def test_market_value(portfolio):
    p = _make_position(entry_price=0.50, size=100.0, cost_basis=50.0)
    portfolio.add_position(p)
    p.update_pnl(0.60)
    assert portfolio.total_market_value == pytest.approx(60.0)
    assert portfolio.total_unrealized_pnl == pytest.approx(10.0)
