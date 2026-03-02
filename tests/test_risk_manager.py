"""Tests for the risk manager."""

import pytest

from core.types import Market, Outcome, Signal, SignalAction, StrategyType
from risk.risk_manager import RiskManager


@pytest.fixture
def rm():
    return RiskManager(
        max_daily_loss=50.0,
        max_positions=3,
        max_exposure=500.0,
        max_exposure_per_category=200.0,
        max_consecutive_losses=3,
        min_hours_to_resolution=24.0,
        max_position_size=100.0,
    )


@pytest.fixture
def market():
    from datetime import datetime, timezone, timedelta

    return Market(
        condition_id="test-cid",
        question="Will it rain?",
        description="",
        category="weather",
        end_date=datetime.now(timezone.utc) + timedelta(days=7),
        tokens={"YES": "y-tok", "NO": "n-tok"},
        volume=50000,
        liquidity=10000,
    )


@pytest.fixture
def signal():
    return Signal(
        market_id="test-cid",
        action=SignalAction.BUY,
        outcome=Outcome.YES,
        strategy=StrategyType.EDGE,
        price=0.60,
        size=50.0,
        edge=0.10,
        confidence=0.7,
        reasoning="test",
    )


def test_trade_allowed_basic(rm, signal, market):
    allowed, reason = rm.check_trade(signal, market)
    assert allowed is True
    assert reason == "OK"


def test_kill_switch_blocks(rm, signal, market):
    rm.activate_kill_switch("test")
    allowed, reason = rm.check_trade(signal, market)
    assert allowed is False
    assert "Kill switch" in reason


def test_kill_switch_toggle(rm, signal, market):
    rm.activate_kill_switch("test")
    assert not rm.is_trading_allowed
    rm.deactivate_kill_switch()
    allowed, _ = rm.check_trade(signal, market)
    assert allowed is True


def test_daily_loss_limit(rm, signal, market):
    rm.record_trade_close(50.0, -60.0, "weather")
    allowed, reason = rm.check_trade(signal, market)
    assert allowed is False
    assert "Daily loss" in reason


def test_consecutive_loss_circuit_breaker(rm, signal, market):
    for _ in range(3):
        rm.record_trade_close(10.0, -5.0, "weather")
    allowed, reason = rm.check_trade(signal, market)
    assert allowed is False
    assert "Consecutive loss" in reason


def test_consecutive_loss_resets_on_win(rm, signal, market):
    rm.record_trade_close(10.0, -5.0, "weather")
    rm.record_trade_close(10.0, -5.0, "weather")
    # Win resets counter
    rm.record_trade_close(10.0, 10.0, "weather")
    # Two more losses still below limit of 3
    rm.record_trade_close(10.0, -5.0, "weather")
    rm.record_trade_close(10.0, -5.0, "weather")
    # Daily pnl is -5-5+10-5-5 = -10, still above -50
    allowed, _ = rm.check_trade(signal, market)
    assert allowed is True


def test_max_positions_blocks(rm, signal, market):
    for _ in range(3):
        rm.record_trade_open(10.0, "weather")
    allowed, reason = rm.check_trade(signal, market)
    assert allowed is False
    assert "Max positions" in reason


def test_max_exposure_blocks(rm, signal, market):
    signal.size = 501.0
    allowed, reason = rm.check_trade(signal, market)
    assert allowed is False
    assert "exposure exceeded" in reason


def test_category_exposure_blocks(rm, signal, market):
    # Fill up category exposure
    rm._category_exposure["weather"] = 180.0
    signal.size = 30.0
    allowed, reason = rm.check_trade(signal, market)
    assert allowed is False
    assert "Category" in reason


def test_position_size_cap(rm, signal, market):
    signal.size = 150.0
    allowed, reason = rm.check_trade(signal, market)
    assert allowed is False
    assert "Position size" in reason


def test_time_to_resolution_blocks(rm, signal, market):
    from datetime import datetime, timezone, timedelta

    market.end_date = datetime.now(timezone.utc) + timedelta(hours=6)
    allowed, reason = rm.check_trade(signal, market)
    assert allowed is False
    assert "Too close to resolution" in reason


def test_no_end_date_allows(rm, signal, market):
    market.end_date = None
    allowed, _ = rm.check_trade(signal, market)
    assert allowed is True


def test_daily_reset(rm, signal, market):
    rm.record_trade_close(50.0, -60.0, "weather")
    assert not rm.is_trading_allowed
    rm.reset_daily()
    assert rm.is_trading_allowed
    allowed, _ = rm.check_trade(signal, market)
    assert allowed is True


def test_record_open_close_tracking(rm):
    rm.record_trade_open(50.0, "crypto")
    assert rm._open_positions == 1
    assert rm._open_exposure == 50.0
    assert rm._category_exposure["crypto"] == 50.0

    rm.record_trade_close(50.0, 10.0, "crypto")
    assert rm._open_positions == 0
    assert rm._open_exposure == 0.0
    assert rm._category_exposure["crypto"] == 0.0
    assert rm._daily_pnl == 10.0


def test_summary(rm):
    rm.record_trade_open(50.0, "politics")
    rm.record_trade_close(50.0, -20.0, "politics")
    s = rm.summary()
    assert s["daily_pnl"] == -20.0
    assert s["daily_trades"] == 1
    assert s["consecutive_losses"] == 1
    assert s["trading_allowed"] is True
    assert "max_daily_loss" in s["limits"]


def test_is_trading_allowed(rm):
    assert rm.is_trading_allowed is True
    rm.activate_kill_switch()
    assert rm.is_trading_allowed is False


# --- Portfolio-based risk checks ---

def _make_market(condition_id, question, category, hours=168):
    from datetime import datetime, timezone, timedelta
    return Market(
        condition_id=condition_id,
        question=question,
        description="",
        category=category,
        end_date=datetime.now(timezone.utc) + timedelta(hours=hours),
        tokens={"YES": f"{condition_id}_y", "NO": f"{condition_id}_n"},
        volume=50000,
        liquidity=10000,
    )


def _make_signal(market_id, outcome=Outcome.YES, size=50.0):
    return Signal(
        market_id=market_id,
        action=SignalAction.BUY,
        outcome=outcome,
        strategy=StrategyType.EDGE,
        price=0.60,
        size=size,
        edge=0.10,
        confidence=0.7,
        reasoning="test",
    )


def _make_position(market_id, outcome=Outcome.YES, cost_basis=50.0):
    from core.types import Position, Side
    return Position(
        market_id=market_id,
        condition_id=market_id,
        outcome=outcome,
        token_id=f"{market_id}_tok",
        side=Side.BUY,
        entry_price=0.50,
        size=100,
        cost_basis=cost_basis,
    )


def _rm_with_portfolio():
    from risk.portfolio import Portfolio
    portfolio = Portfolio()
    rm = RiskManager(
        max_daily_loss=500.0,
        max_positions=10,
        max_exposure=5000.0,
        max_exposure_per_category=2000.0,
        max_consecutive_losses=3,
        max_position_size=200.0,
        max_positions_per_category=3,
        max_correlated_positions=2,
        max_same_outcome_per_category=2,
        portfolio=portfolio,
    )
    return rm, portfolio


def test_category_position_limit():
    """Block entry when category has too many open positions."""
    rm, portfolio = _rm_with_portfolio()

    # Open 3 positions in politics
    for i in range(3):
        mkt = _make_market(f"pol-{i}", f"Will X{i} happen?", "politics")
        pos = _make_position(f"pol-{i}")
        portfolio.add_position(pos, mkt)

    # 4th politics entry should be blocked
    new_market = _make_market("pol-new", "Will Y happen?", "politics")
    signal = _make_signal("pol-new")
    allowed, reason = rm.check_trade(signal, new_market)
    assert allowed is False
    assert "position limit" in reason.lower()


def test_category_position_limit_allows_different_category():
    """Different category should not be blocked by another category's limit."""
    rm, portfolio = _rm_with_portfolio()

    # Fill up politics
    for i in range(3):
        mkt = _make_market(f"pol-{i}", f"Will X{i} happen?", "politics")
        pos = _make_position(f"pol-{i}")
        portfolio.add_position(pos, mkt)

    # Crypto entry should be allowed
    crypto_market = _make_market("crypto-1", "Will BTC hit 100k?", "crypto")
    signal = _make_signal("crypto-1")
    allowed, _ = rm.check_trade(signal, crypto_market)
    assert allowed is True


def test_correlated_position_limit():
    """Block when too many correlated markets (same category counts as correlated)."""
    rm, portfolio = _rm_with_portfolio()

    # Open 2 Iran-related positions (same category)
    mkt1 = _make_market("iran-1", "Will Iran attack Israel?", "politics")
    pos1 = _make_position("iran-1")
    portfolio.add_position(pos1, mkt1)

    mkt2 = _make_market("iran-2", "Will Iran close strait?", "politics")
    pos2 = _make_position("iran-2")
    portfolio.add_position(pos2, mkt2)

    # 3rd Iran entry should be blocked (2 correlated in same category)
    new_market = _make_market("iran-3", "Will Iran sanctions increase?", "politics")
    signal = _make_signal("iran-3")
    allowed, reason = rm.check_trade(signal, new_market)
    assert allowed is False
    assert "Correlated" in reason


def test_same_outcome_limit():
    """Block when too many same-outcome bets in one category."""
    from risk.portfolio import Portfolio
    portfolio = Portfolio()
    rm = RiskManager(
        max_daily_loss=500.0,
        max_positions=10,
        max_exposure=5000.0,
        max_exposure_per_category=2000.0,
        max_consecutive_losses=3,
        max_position_size=200.0,
        max_positions_per_category=5,      # High limit so this doesn't fire
        max_correlated_positions=5,         # High limit so this doesn't fire
        max_same_outcome_per_category=2,    # This is what we're testing
        portfolio=portfolio,
    )

    # Open 2 NO bets in politics
    mkt1 = _make_market("pol-1", "Will candidate A win?", "politics")
    pos1 = _make_position("pol-1", outcome=Outcome.NO)
    portfolio.add_position(pos1, mkt1)

    mkt2 = _make_market("pol-2", "Will candidate B win?", "politics")
    pos2 = _make_position("pol-2", outcome=Outcome.NO)
    portfolio.add_position(pos2, mkt2)

    # 3rd NO bet in politics should be blocked
    new_market = _make_market("pol-3", "Will candidate C win?", "politics")
    signal = _make_signal("pol-3", outcome=Outcome.NO)
    allowed, reason = rm.check_trade(signal, new_market)
    assert allowed is False
    assert "Same-outcome" in reason


def test_same_outcome_allows_opposite():
    """YES bet should be allowed even if 2 NO bets exist in same category."""
    from risk.portfolio import Portfolio
    portfolio = Portfolio()
    rm = RiskManager(
        max_daily_loss=500.0,
        max_positions=10,
        max_exposure=5000.0,
        max_exposure_per_category=2000.0,
        max_consecutive_losses=3,
        max_position_size=200.0,
        max_positions_per_category=5,
        max_correlated_positions=5,
        max_same_outcome_per_category=2,
        portfolio=portfolio,
    )

    # 2 NO bets in politics
    for i in range(2):
        mkt = _make_market(f"pol-{i}", f"Will X{i} happen?", "politics")
        pos = _make_position(f"pol-{i}", outcome=Outcome.NO)
        portfolio.add_position(pos, mkt)

    # YES bet should be fine
    new_market = _make_market("pol-new", "Will Y happen?", "politics")
    signal = _make_signal("pol-new", outcome=Outcome.YES)
    allowed, _ = rm.check_trade(signal, new_market)
    assert allowed is True


def test_no_portfolio_skips_checks():
    """Without portfolio wired, portfolio-based checks are skipped."""
    rm = RiskManager(
        max_daily_loss=500.0,
        max_positions=10,
        max_exposure=5000.0,
        max_exposure_per_category=2000.0,
        max_position_size=200.0,
    )
    market = _make_market("test", "Test?", "politics")
    signal = _make_signal("test")
    allowed, _ = rm.check_trade(signal, market)
    assert allowed is True


def test_arb_losses_excluded_from_circuit_breaker():
    """Arb losses should not count toward consecutive loss counter."""
    rm, _ = _rm_with_portfolio()
    rm.record_trade_close(50.0, -10.0, "crypto", strategy="arbitrage")
    rm.record_trade_close(50.0, -10.0, "crypto", strategy="arbitrage")
    rm.record_trade_close(50.0, -10.0, "crypto", strategy="arbitrage")
    assert rm._consecutive_losses == 0
