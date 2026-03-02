"""Tests for in-memory market cache."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.types import Market, OrderBookLevel
from data.market_cache import MarketCache


def _make_market(cid="m1", yes_token="t1", no_token="t2"):
    return Market(
        condition_id=cid,
        question="Test?",
        description="",
        category="test",
        end_date=None,
        tokens={"YES": yes_token, "NO": no_token},
        last_price_yes=0.60,
        last_price_no=0.40,
    )


def test_add_and_get_market():
    cache = MarketCache()
    m = _make_market()
    cache.add_market(m)
    assert cache.get_market("m1") is not None
    assert cache.market_count == 1


def test_bulk_add():
    cache = MarketCache()
    markets = [_make_market(f"m{i}", f"ty{i}", f"tn{i}") for i in range(5)]
    cache.add_markets(markets)
    assert cache.market_count == 5


def test_update_price():
    cache = MarketCache()
    cache.add_market(_make_market())
    cache.update_price("t1", 0.75)
    m = cache.get_market("m1")
    assert m.last_price_yes == 0.75
    assert m.last_price_no == 0.25


def test_update_book():
    cache = MarketCache()
    cache.add_market(_make_market())
    bids = [{"price": 0.60, "size": 100}]
    asks = [{"price": 0.65, "size": 100}]
    cache.update_book("t1", bids, asks)

    book = cache.get_book("t1")
    assert book is not None
    assert book.best_bid == 0.60
    assert book.best_ask == 0.65

    m = cache.get_market("m1")
    assert m.best_bid_yes == 0.60
    assert m.best_ask_yes == 0.65
    assert abs(m.spread_yes - 0.05) < 0.001


def test_get_market_for_token():
    cache = MarketCache()
    cache.add_market(_make_market())
    m = cache.get_market_for_token("t1")
    assert m is not None
    assert m.condition_id == "m1"


def test_remove_market():
    cache = MarketCache()
    cache.add_market(_make_market())
    cache.update_price("t1", 0.70)
    cache.remove_market("m1")
    assert cache.get_market("m1") is None
    assert cache.get_price("t1") is None
    assert cache.market_count == 0


def test_clear():
    cache = MarketCache()
    cache.add_markets([_make_market(f"m{i}", f"y{i}", f"n{i}") for i in range(3)])
    cache.clear()
    assert cache.market_count == 0
    assert cache.token_count == 0


def test_get_active_markets():
    cache = MarketCache()
    m1 = _make_market("m1", "y1", "n1")
    m1.active = True
    m2 = _make_market("m2", "y2", "n2")
    m2.active = False
    cache.add_markets([m1, m2])
    active = cache.get_active_markets()
    assert len(active) == 1
    assert active[0].condition_id == "m1"


def test_summary():
    cache = MarketCache()
    cache.add_market(_make_market())
    cache.update_price("t1", 0.70)
    s = cache.summary()
    assert s["markets"] == 1
    assert s["tokens"] == 2
    assert s["prices_cached"] == 1


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
