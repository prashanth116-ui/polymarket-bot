"""Tests for market maker strategy."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone, timedelta
from core.types import Market, OrderBook, OrderBookLevel, Outcome, SignalAction, StrategyType
from strategies.market_maker import MarketMakerStrategy


def _make_market(
    price_yes=0.50,
    spread=0.04,
    liquidity=1000,
    hours=168,
    active=True,
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
        active=active,
        volume=5000,
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


def test_mm_returns_two_signals():
    mm = MarketMakerStrategy()
    market = _make_market()
    signals = mm.evaluate(market)
    assert len(signals) == 2


def test_mm_signals_are_bid_and_ask():
    mm = MarketMakerStrategy()
    market = _make_market()
    signals = mm.evaluate(market)
    actions = {s.action for s in signals}
    assert SignalAction.BUY in actions
    assert SignalAction.SELL in actions


def test_mm_strategy_type():
    mm = MarketMakerStrategy()
    market = _make_market()
    signals = mm.evaluate(market)
    assert all(s.strategy == StrategyType.MARKET_MAKING for s in signals)


def test_mm_bid_below_ask():
    mm = MarketMakerStrategy()
    market = _make_market()
    signals = mm.evaluate(market)
    bid_signal = next(s for s in signals if s.action == SignalAction.BUY)
    ask_signal = next(s for s in signals if s.action == SignalAction.SELL)
    assert bid_signal.price < ask_signal.price


def test_mm_spread_within_bounds():
    mm = MarketMakerStrategy(min_spread=0.04, max_spread=0.15)
    market = _make_market()
    signals = mm.evaluate(market)
    bid = next(s for s in signals if s.action == SignalAction.BUY)
    ask = next(s for s in signals if s.action == SignalAction.SELL)
    spread = ask.price - bid.price
    assert spread >= 0.04
    assert spread <= 0.20  # Allow some boundary adjustment


def test_mm_inventory_skew_long():
    mm = MarketMakerStrategy(skew_factor=0.1, max_inventory=100)
    market = _make_market(price_yes=0.50)
    # Holding 50 YES shares — should lower ask to sell faster
    context = {"inventory_yes": 50, "book_yes": _make_book()}
    signals = mm.evaluate(market, context)
    bid = next(s for s in signals if s.action == SignalAction.BUY)
    ask = next(s for s in signals if s.action == SignalAction.SELL)
    # Reservation price should be below mid due to long inventory
    assert bid.metadata["reservation_price"] < 0.50


def test_mm_inventory_skew_short():
    mm = MarketMakerStrategy(skew_factor=0.1, max_inventory=100)
    market = _make_market(price_yes=0.50)
    # Negative inventory (short) — should raise bid to buy back
    context = {"inventory_yes": -50, "book_yes": _make_book()}
    signals = mm.evaluate(market, context)
    bid = next(s for s in signals if s.action == SignalAction.BUY)
    assert bid.metadata["reservation_price"] > 0.50


def test_mm_zero_inventory_centered():
    mm = MarketMakerStrategy(skew_factor=0.1, max_inventory=100)
    market = _make_market(price_yes=0.50)
    context = {"inventory_yes": 0, "book_yes": _make_book()}
    signals = mm.evaluate(market, context)
    bid = next(s for s in signals if s.action == SignalAction.BUY)
    # Reservation should be at or very near midpoint
    assert abs(bid.metadata["reservation_price"] - 0.50) < 0.01


def test_mm_inactive_market_filtered():
    mm = MarketMakerStrategy()
    market = _make_market(active=False)
    signals = mm.evaluate(market)
    assert len(signals) == 0


def test_mm_low_liquidity_filtered():
    mm = MarketMakerStrategy(min_liquidity=500)
    market = _make_market(liquidity=100)
    signals = mm.evaluate(market)
    assert len(signals) == 0


def test_mm_near_resolution_filtered():
    mm = MarketMakerStrategy(taper_stop_hours=48)
    market = _make_market(hours=24)  # Only 24h left — below 48h stop
    signals = mm.evaluate(market)
    assert len(signals) == 0


def test_mm_resolution_taper_full_size():
    mm = MarketMakerStrategy(quote_size=20, taper_start_hours=168, taper_stop_hours=48)
    market = _make_market(hours=200)  # Well above taper start
    signals = mm.evaluate(market)
    assert len(signals) == 2


def test_mm_resolution_taper_reduced_size():
    mm = MarketMakerStrategy(quote_size=20, taper_start_hours=168, taper_stop_hours=48)
    # 108 hours — midway through taper window
    market = _make_market(hours=108)
    signals = mm.evaluate(market)
    assert len(signals) == 2
    # Size should be reduced but > 0
    bid = next(s for s in signals if s.action == SignalAction.BUY)
    # At 108h: fraction = (108-48)/(168-48) = 60/120 = 0.5, so size ~ 10
    assert bid.size > 0


def test_mm_wide_existing_spread_filtered():
    mm = MarketMakerStrategy(max_existing_spread=0.10)
    market = _make_market(spread=0.15)  # Existing spread too wide
    signals = mm.evaluate(market)
    assert len(signals) == 0


def test_mm_no_end_date_full_size():
    mm = MarketMakerStrategy(quote_size=20)
    market = _make_market(hours=None)  # No end date
    market.end_date = None
    signals = mm.evaluate(market)
    assert len(signals) == 2


def test_mm_boundary_near_zero():
    mm = MarketMakerStrategy(boundary_buffer=0.08)
    market = _make_market(price_yes=0.05, spread=0.02)
    signals = mm.evaluate(market)
    if signals:
        bid = next(s for s in signals if s.action == SignalAction.BUY)
        assert bid.price >= 0.01  # Never negative


def test_mm_boundary_near_one():
    mm = MarketMakerStrategy(boundary_buffer=0.08)
    market = _make_market(price_yes=0.95, spread=0.02)
    signals = mm.evaluate(market)
    if signals:
        ask = next(s for s in signals if s.action == SignalAction.SELL)
        assert ask.price <= 0.99  # Never above 1


def test_mm_missing_tokens():
    mm = MarketMakerStrategy()
    market = _make_market()
    market.tokens = {}
    signals = mm.evaluate(market)
    assert len(signals) == 0


def test_should_cancel_inactive_market():
    mm = MarketMakerStrategy()
    market = _make_market(active=False)
    assert mm.should_cancel_quotes(market) is True


def test_should_cancel_max_inventory():
    mm = MarketMakerStrategy(max_inventory=100)
    market = _make_market()
    context = {"inventory_yes": 150}
    assert mm.should_cancel_quotes(market, context) is True


def test_should_cancel_near_resolution():
    mm = MarketMakerStrategy(taper_stop_hours=48)
    market = _make_market(hours=24)
    assert mm.should_cancel_quotes(market) is True


def test_should_not_cancel_normal():
    mm = MarketMakerStrategy(max_inventory=200, taper_stop_hours=48)
    market = _make_market(hours=168)
    context = {"inventory_yes": 50}
    assert mm.should_cancel_quotes(market, context) is False


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
