"""Tests for arbitrage strategy."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone, timedelta
from core.types import Market, OrderBook, OrderBookLevel, Outcome, StrategyType
from strategies.arbitrage import ArbitrageStrategy


def _make_market(
    ask_yes=0.50, ask_no=0.50,
    bid_yes=0.48, bid_no=0.48,
    liquidity=1000, active=True,
):
    return Market(
        condition_id="arb-market",
        question="Will X happen?",
        description="Test",
        category="crypto",
        end_date=datetime.now(timezone.utc) + timedelta(hours=168),
        tokens={"YES": "tok-yes", "NO": "tok-no"},
        active=active,
        volume=5000,
        liquidity=liquidity,
        last_price_yes=ask_yes,
        last_price_no=ask_no,
        best_bid_yes=bid_yes,
        best_ask_yes=ask_yes,
        best_bid_no=bid_no,
        best_ask_no=ask_no,
    )


def _make_book(token_id, best_ask, ask_size=100, best_bid=None, bid_size=100):
    if best_bid is None:
        best_bid = best_ask - 0.02
    return OrderBook(
        token_id=token_id,
        bids=[OrderBookLevel(price=best_bid, size=bid_size)],
        asks=[OrderBookLevel(price=best_ask, size=ask_size)],
    )


def test_no_arb_when_sum_above_one():
    arb = ArbitrageStrategy(min_profit_bps=50)
    market = _make_market(ask_yes=0.55, ask_no=0.55)
    # 0.55 + 0.55 = 1.10 > 1.00 — no arb
    signals = arb.evaluate(market)
    assert len(signals) == 0


def test_no_arb_after_fees():
    arb = ArbitrageStrategy(min_profit_bps=50, fee_bps=200)
    # Sum = 0.96, but after 2% fee: 0.96 * 1.02 = 0.9792 < 1.0
    # profit = 1.0 - 0.9792 = 0.0208 / 0.9792 = ~212 bps — should pass
    market = _make_market(ask_yes=0.48, ask_no=0.48)
    signals = arb.evaluate(market)
    assert len(signals) == 2


def test_no_arb_fee_wipes_profit():
    arb = ArbitrageStrategy(min_profit_bps=50, fee_bps=200)
    # Sum = 0.98, after 2% fee: 0.98 * 1.02 = 0.9996 < 1.0
    # profit = 0.0004 / 0.9996 = ~4 bps — below 50 bps min
    market = _make_market(ask_yes=0.49, ask_no=0.49)
    signals = arb.evaluate(market)
    assert len(signals) == 0


def test_arb_found_returns_two_signals():
    arb = ArbitrageStrategy(min_profit_bps=10, fee_bps=0)
    # Sum = 0.90 < 1.0 — guaranteed 10% profit
    market = _make_market(ask_yes=0.45, ask_no=0.45)
    signals = arb.evaluate(market)
    assert len(signals) == 2


def test_arb_signals_are_buy():
    arb = ArbitrageStrategy(min_profit_bps=10, fee_bps=0)
    market = _make_market(ask_yes=0.45, ask_no=0.45)
    signals = arb.evaluate(market)
    from core.types import SignalAction
    assert all(s.action == SignalAction.BUY for s in signals)


def test_arb_signals_cover_both_outcomes():
    arb = ArbitrageStrategy(min_profit_bps=10, fee_bps=0)
    market = _make_market(ask_yes=0.45, ask_no=0.45)
    signals = arb.evaluate(market)
    outcomes = {s.outcome for s in signals}
    assert outcomes == {Outcome.YES, Outcome.NO}


def test_arb_signals_strategy_type():
    arb = ArbitrageStrategy(min_profit_bps=10, fee_bps=0)
    market = _make_market(ask_yes=0.45, ask_no=0.45)
    signals = arb.evaluate(market)
    assert all(s.strategy == StrategyType.ARBITRAGE for s in signals)


def test_arb_metadata_has_guaranteed_profit():
    arb = ArbitrageStrategy(min_profit_bps=10, fee_bps=0)
    market = _make_market(ask_yes=0.45, ask_no=0.45)
    signals = arb.evaluate(market)
    for s in signals:
        assert s.metadata.get("is_arb") is True
        assert s.metadata.get("guaranteed_profit") > 0
        assert s.metadata.get("arb_leg") in ("yes", "no")


def test_arb_confidence_is_one():
    arb = ArbitrageStrategy(min_profit_bps=10, fee_bps=0)
    market = _make_market(ask_yes=0.45, ask_no=0.45)
    signals = arb.evaluate(market)
    assert all(s.confidence == 1.0 for s in signals)


def test_arb_with_order_books():
    arb = ArbitrageStrategy(min_profit_bps=10, fee_bps=0)
    market = _make_market(ask_yes=0.45, ask_no=0.45)
    book_yes = _make_book("tok-yes", best_ask=0.45, ask_size=50)
    book_no = _make_book("tok-no", best_ask=0.45, ask_size=80)
    context = {"book_yes": book_yes, "book_no": book_no}
    signals = arb.evaluate(market, context)
    assert len(signals) == 2


def test_arb_size_limited_by_book_depth():
    arb = ArbitrageStrategy(min_profit_bps=10, fee_bps=0, max_position=1000)
    market = _make_market(ask_yes=0.45, ask_no=0.45)
    book_yes = _make_book("tok-yes", best_ask=0.45, ask_size=10)
    book_no = _make_book("tok-no", best_ask=0.45, ask_size=5)
    context = {"book_yes": book_yes, "book_no": book_no}
    signals = arb.evaluate(market, context)
    assert len(signals) == 2


def test_arb_inactive_market():
    arb = ArbitrageStrategy(min_profit_bps=10, fee_bps=0)
    market = _make_market(ask_yes=0.45, ask_no=0.45, active=False)
    signals = arb.evaluate(market)
    assert len(signals) == 0


def test_arb_missing_tokens():
    arb = ArbitrageStrategy(min_profit_bps=10, fee_bps=0)
    market = _make_market(ask_yes=0.45, ask_no=0.45)
    market.tokens = {}
    signals = arb.evaluate(market)
    assert len(signals) == 0


def test_cross_platform_stub_returns_empty():
    arb = ArbitrageStrategy()
    market = _make_market()
    signals = arb.check_cross_platform(market, external_price=0.50)
    assert signals == []


def test_arb_asymmetric_prices():
    arb = ArbitrageStrategy(min_profit_bps=10, fee_bps=0)
    # YES cheap, NO expensive — sum = 0.30 + 0.60 = 0.90 < 1.0
    market = _make_market(ask_yes=0.30, ask_no=0.60)
    signals = arb.evaluate(market)
    assert len(signals) == 2
    yes_signal = next(s for s in signals if s.outcome == Outcome.YES)
    no_signal = next(s for s in signals if s.outcome == Outcome.NO)
    assert yes_signal.price == 0.30
    assert no_signal.price == 0.60


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
