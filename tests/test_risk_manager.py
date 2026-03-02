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
