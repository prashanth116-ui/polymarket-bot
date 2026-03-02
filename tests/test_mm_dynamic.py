"""Tests for dynamic market maker improvements — historical volatility, volume spread, dynamic sizing."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone, timedelta
from core.types import Market, OrderBook, OrderBookLevel, Outcome, SignalAction
from strategies.market_maker import MarketMakerStrategy


def _make_market(
    price_yes=0.50,
    spread=0.04,
    liquidity=1000,
    hours=168,
    volume=5000,
):
    end_date = datetime.now(timezone.utc) + timedelta(hours=hours) if hours else None
    bid = price_yes - spread / 2
    ask = price_yes + spread / 2
    return Market(
        condition_id="mm-market",
        question="Will X happen?",
        description="Test",
        category="crypto",
        end_date=end_date,
        tokens={"YES": "tok-yes", "NO": "tok-no"},
        active=True,
        volume=volume,
        liquidity=liquidity,
        last_price_yes=price_yes,
        last_price_no=1 - price_yes,
        best_bid_yes=bid,
        best_ask_yes=ask,
        best_bid_no=1 - ask,
        best_ask_no=1 - bid,
        spread_yes=spread,
        spread_no=spread,
    )


def _make_book(mid=0.50, spread=0.04):
    return OrderBook(
        token_id="tok-yes",
        bids=[
            OrderBookLevel(price=mid - spread / 2, size=100),
            OrderBookLevel(price=mid - spread, size=200),
        ],
        asks=[
            OrderBookLevel(price=mid + spread / 2, size=100),
            OrderBookLevel(price=mid + spread, size=200),
        ],
    )


# --- Historical Volatility Tests ---

def test_record_price_stores_history():
    mm = MarketMakerStrategy()
    mm.record_price("market1", 0.50)
    mm.record_price("market1", 0.52)
    mm.record_price("market1", 0.51)
    assert len(mm._price_history["market1"]) == 3


def test_price_history_limited_to_max():
    mm = MarketMakerStrategy()
    mm._max_price_history = 5
    for i in range(10):
        mm.record_price("m", 0.50 + i * 0.01)
    assert len(mm._price_history["m"]) == 5


def test_volatility_from_history():
    """With enough price history, volatility should be based on returns std dev."""
    mm = MarketMakerStrategy()
    book = _make_book(mid=0.50, spread=0.04)

    # No history — should fall back to book spread / 2
    vol_no_history = mm._estimate_volatility(book)
    assert abs(vol_no_history - 0.02) < 1e-10  # book.spread / 2

    # Add price history
    prices = [0.50, 0.52, 0.48, 0.51, 0.53]
    for p in prices:
        mm.record_price("tok-yes", p)

    vol_with_history = mm._estimate_volatility(book)
    # Should be based on returns, not book spread
    assert abs(vol_with_history - 0.02) > 1e-6
    assert vol_with_history > 0


def test_volatility_fallback_with_few_prices():
    """With < 3 prices, should fall back to book spread."""
    mm = MarketMakerStrategy()
    mm.record_price("tok-yes", 0.50)
    mm.record_price("tok-yes", 0.52)
    book = _make_book(mid=0.50, spread=0.04)

    vol = mm._estimate_volatility(book)
    assert abs(vol - 0.02) < 1e-10  # Fallback to book spread / 2


# --- Volume-Weighted Spread Tests ---

def test_high_volume_tighter_spread():
    """High-volume markets should get tighter spreads."""
    mm = MarketMakerStrategy(base_spread=0.06)

    market_low = _make_market(volume=5000)
    market_high = _make_market(volume=100000)

    signals_low = mm.evaluate(market_low)
    signals_high = mm.evaluate(market_high)

    assert len(signals_low) == 2
    assert len(signals_high) == 2

    spread_low = (
        next(s for s in signals_low if s.action == SignalAction.SELL).price -
        next(s for s in signals_low if s.action == SignalAction.BUY).price
    )
    spread_high = (
        next(s for s in signals_high if s.action == SignalAction.SELL).price -
        next(s for s in signals_high if s.action == SignalAction.BUY).price
    )

    # High-volume market should have tighter spread
    assert spread_high < spread_low


def test_low_volume_no_spread_reduction():
    """Markets below $10k volume should not get spread reduction."""
    mm = MarketMakerStrategy(base_spread=0.06)

    market1 = _make_market(volume=5000)
    market2 = _make_market(volume=8000)

    signals1 = mm.evaluate(market1)
    signals2 = mm.evaluate(market2)

    spread1 = (
        next(s for s in signals1 if s.action == SignalAction.SELL).price -
        next(s for s in signals1 if s.action == SignalAction.BUY).price
    )
    spread2 = (
        next(s for s in signals2 if s.action == SignalAction.SELL).price -
        next(s for s in signals2 if s.action == SignalAction.BUY).price
    )

    # Both below $10k — same spread
    assert abs(spread1 - spread2) < 0.001


# --- Dynamic Quote Sizing Tests ---

def test_dynamic_size_scales_with_liquidity():
    """Quote size should scale with market liquidity."""
    mm = MarketMakerStrategy()

    market_low = _make_market(liquidity=500, hours=500)
    market_high = _make_market(liquidity=5000, hours=500)

    size_low = mm._dynamic_size(market_low)
    size_high = mm._dynamic_size(market_high)

    assert size_high > size_low
    assert size_low >= 5  # Minimum


def test_dynamic_size_min_5():
    """Quote size should never go below 5 (before taper)."""
    mm = MarketMakerStrategy()
    market = _make_market(liquidity=100, hours=500)

    size = mm._dynamic_size(market)
    assert size >= 5


def test_dynamic_size_max_100():
    """Quote size should never exceed 100."""
    mm = MarketMakerStrategy()
    market = _make_market(liquidity=50000)

    size = mm._dynamic_size(market)
    assert size <= 100


def test_dynamic_size_reduces_with_inventory():
    """Quote size should decrease as inventory grows."""
    mm = MarketMakerStrategy(max_inventory=200)
    market = _make_market(liquidity=2000)

    size_no_inv = mm._dynamic_size(market, inventory=0)
    size_half_inv = mm._dynamic_size(market, inventory=100)
    size_full_inv = mm._dynamic_size(market, inventory=200)

    assert size_no_inv > size_half_inv
    assert size_half_inv > size_full_inv
    assert size_full_inv >= 1


def test_dynamic_size_resolution_taper():
    """Quote size should reduce as resolution approaches."""
    mm = MarketMakerStrategy(taper_start_hours=168, taper_stop_hours=48)

    market_far = _make_market(hours=200, liquidity=2000)
    market_mid = _make_market(hours=108, liquidity=2000)
    market_near = _make_market(hours=24, liquidity=2000)

    size_far = mm._dynamic_size(market_far)
    size_mid = mm._dynamic_size(market_mid)
    size_near = mm._dynamic_size(market_near)

    assert size_far > size_mid
    assert size_near == 0  # Below taper stop


def test_dynamic_size_no_end_date():
    """No end date should use full liquidity-based size."""
    mm = MarketMakerStrategy()
    market = _make_market(liquidity=2000)
    market.end_date = None

    size = mm._dynamic_size(market)
    assert size == max(5, min(100, int(2000 / 100)))


# --- Integration: Evaluate with New Features ---

def test_evaluate_with_book_and_history():
    """Full evaluate with order book and price history should work."""
    mm = MarketMakerStrategy()
    market = _make_market(liquidity=2000, volume=20000)
    book = _make_book(mid=0.50, spread=0.04)

    # Feed price history
    for p in [0.48, 0.49, 0.50, 0.51, 0.52]:
        mm.record_price("tok-yes", p)

    context = {"book_yes": book, "inventory_yes": 0}
    signals = mm.evaluate(market, context)

    assert len(signals) == 2
    bid = next(s for s in signals if s.action == SignalAction.BUY)
    ask = next(s for s in signals if s.action == SignalAction.SELL)
    assert bid.price < ask.price


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
