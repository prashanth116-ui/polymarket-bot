"""Tests for strategy coordinator."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone, timedelta
from core.types import (
    Market, OpenOrder, OrderBook, OrderBookLevel,
    Outcome, ProbabilityEstimate, Side, Signal, SignalAction, StrategyType,
)
from models.base import ProbabilityModel
from strategies.arbitrage import ArbitrageStrategy
from strategies.coordinator import StrategyCoordinator
from strategies.edge_strategy import EdgeStrategy
from strategies.market_maker import MarketMakerStrategy


class StubModel(ProbabilityModel):
    """Returns a configurable probability."""

    def __init__(self, prob=0.5, confidence=0.8):
        self._prob = prob
        self._conf = confidence

    @property
    def name(self):
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


def _make_market(
    cid="test-market",
    price_yes=0.50,
    ask_yes=0.50,
    ask_no=0.50,
    spread=0.06,
    liquidity=1000,
    hours=168,
):
    end_date = datetime.now(timezone.utc) + timedelta(hours=hours)
    return Market(
        condition_id=cid,
        question="Will X happen?",
        description="Test",
        category="crypto",
        end_date=end_date,
        tokens={"YES": "tok-yes", "NO": "tok-no"},
        active=True,
        volume=5000,
        liquidity=liquidity,
        last_price_yes=price_yes,
        last_price_no=1 - price_yes,
        best_bid_yes=price_yes - spread / 2,
        best_ask_yes=ask_yes,
        best_bid_no=(1 - price_yes) - spread / 2,
        best_ask_no=ask_no,
        spread_yes=spread,
        spread_no=spread,
    )


def _make_coordinator(model_prob=0.5, **kwargs):
    model = StubModel(prob=model_prob)
    edge = EdgeStrategy(model=model, min_edge=0.05, bankroll=1000)
    arb = ArbitrageStrategy(min_profit_bps=10, fee_bps=0)
    mm = MarketMakerStrategy()
    return StrategyCoordinator(
        edge_strategy=edge,
        arb_strategy=arb,
        mm_strategy=mm,
        **kwargs,
    )


def test_edge_signal_when_edge_exists():
    coord = _make_coordinator(model_prob=0.70)
    market = _make_market(price_yes=0.50)
    signals = coord.evaluate_market(market)
    assert len(signals) >= 1
    assert any(s.strategy == StrategyType.EDGE for s in signals)


def test_no_mm_when_edge_signal():
    coord = _make_coordinator(model_prob=0.70)
    market = _make_market(price_yes=0.50)
    signals = coord.evaluate_market(market)
    # Edge found — should block MM
    assert not any(s.strategy == StrategyType.MARKET_MAKING for s in signals)


def test_no_arb_when_edge_signal():
    coord = _make_coordinator(model_prob=0.70)
    market = _make_market(price_yes=0.50, ask_yes=0.40, ask_no=0.40)
    signals = coord.evaluate_market(market)
    # Edge found first — blocks arb
    assert not any(s.strategy == StrategyType.ARBITRAGE for s in signals)


def test_mm_when_no_edge():
    coord = _make_coordinator(model_prob=0.52)  # 2% edge — below 5% threshold
    market = _make_market(price_yes=0.50)
    signals = coord.evaluate_market(market)
    # No edge, no arb — should fall through to MM
    assert any(s.strategy == StrategyType.MARKET_MAKING for s in signals)


def test_arb_blocks_mm():
    coord = _make_coordinator(model_prob=0.52)  # No edge
    # Arb opportunity: YES + NO < 1.0
    market = _make_market(price_yes=0.50, ask_yes=0.40, ask_no=0.40)
    signals = coord.evaluate_market(market)
    # Should have arb signals but no MM
    assert any(s.strategy == StrategyType.ARBITRAGE for s in signals)
    assert not any(s.strategy == StrategyType.MARKET_MAKING for s in signals)


def test_edge_position_blocks_all():
    coord = _make_coordinator(model_prob=0.52)
    market = _make_market(cid="edge-pos-market")
    coord.record_edge_entry("edge-pos-market")
    signals = coord.evaluate_market(market)
    assert len(signals) == 0


def test_arb_position_blocks_mm():
    coord = _make_coordinator(model_prob=0.52)
    market = _make_market(cid="arb-pos-market")
    coord.record_arb_entry("arb-pos-market", 50.0)
    signals = coord.evaluate_market(market)
    # Arb position exists — no MM
    assert not any(s.strategy == StrategyType.MARKET_MAKING for s in signals)


def test_mm_market_limit():
    coord = _make_coordinator(model_prob=0.52, max_mm_markets=2)
    # Fill up MM slots
    for i in range(2):
        order = OpenOrder(
            order_id=f"ord-{i}",
            market_id=f"mm-market-{i}",
            token_id=f"tok-{i}",
            outcome=Outcome.YES,
            side=Side.BUY,
            price=0.48,
            size=10,
            strategy=StrategyType.MARKET_MAKING,
        )
        coord.record_order_placed(order)

    # 3rd market should be blocked
    market = _make_market(cid="mm-market-3")
    signals = coord.evaluate_market(market)
    assert not any(s.strategy == StrategyType.MARKET_MAKING for s in signals)


def test_mm_exposure_limit():
    coord = _make_coordinator(model_prob=0.52, max_mm_exposure=50)
    coord._mm_exposure = 50.0  # At limit
    market = _make_market()
    signals = coord.evaluate_market(market)
    assert not any(s.strategy == StrategyType.MARKET_MAKING for s in signals)


def test_arb_exposure_limit():
    coord = _make_coordinator(model_prob=0.52, max_arb_exposure=50)
    coord._arb_exposure = 50.0  # At limit
    market = _make_market(ask_yes=0.40, ask_no=0.40)
    signals = coord.evaluate_market(market)
    assert not any(s.strategy == StrategyType.ARBITRAGE for s in signals)


def test_record_edge_entry_and_exit():
    coord = _make_coordinator()
    coord.record_edge_entry("m1")
    assert "m1" in coord._edge_markets
    coord.record_edge_exit("m1")
    assert "m1" not in coord._edge_markets


def test_record_arb_entry_and_exit():
    coord = _make_coordinator()
    coord.record_arb_entry("m1", 50.0)
    assert "m1" in coord._arb_markets
    assert coord._arb_exposure == 50.0
    coord.record_arb_exit("m1", 50.0)
    assert "m1" not in coord._arb_markets
    assert coord._arb_exposure == 0.0


def test_record_order_placed():
    coord = _make_coordinator()
    order = OpenOrder(
        order_id="o1",
        market_id="m1",
        token_id="t1",
        outcome=Outcome.YES,
        side=Side.BUY,
        price=0.48,
        size=10,
        strategy=StrategyType.MARKET_MAKING,
    )
    coord.record_order_placed(order)
    assert "o1" in coord._open_orders
    assert "m1" in coord._mm_markets


def test_record_order_filled():
    coord = _make_coordinator()
    order = OpenOrder(
        order_id="o1", market_id="m1", token_id="t1",
        outcome=Outcome.YES, side=Side.BUY,
        price=0.48, size=10, strategy=StrategyType.MARKET_MAKING,
    )
    coord.record_order_placed(order)
    coord.record_order_filled("o1", 4.80)
    assert "o1" not in coord._open_orders
    assert coord._mm_exposure == 4.80


def test_record_order_cancelled():
    coord = _make_coordinator()
    order = OpenOrder(
        order_id="o1", market_id="m1", token_id="t1",
        outcome=Outcome.YES, side=Side.BUY,
        price=0.48, size=10, strategy=StrategyType.MARKET_MAKING,
    )
    coord.record_order_placed(order)
    coord.record_order_cancelled("o1")
    assert "o1" not in coord._open_orders
    # No more MM orders for m1 — should be removed from mm_markets
    assert "m1" not in coord._mm_markets


def test_cancel_mm_quotes():
    coord = _make_coordinator()
    for i in range(3):
        order = OpenOrder(
            order_id=f"o{i}", market_id="m1", token_id="t1",
            outcome=Outcome.YES, side=Side.BUY,
            price=0.48, size=10, strategy=StrategyType.MARKET_MAKING,
        )
        coord.record_order_placed(order)
    removed = coord.cancel_mm_quotes("m1")
    assert len(removed) == 3
    assert "m1" not in coord._mm_markets


def test_get_markets_to_mm():
    coord = _make_coordinator(max_mm_markets=2)
    markets = [
        _make_market(cid="m1", spread=0.10, liquidity=1000),  # score = 100
        _make_market(cid="m2", spread=0.05, liquidity=2000),  # score = 100
        _make_market(cid="m3", spread=0.02, liquidity=500),   # score = 10
    ]
    selected = coord.get_markets_to_mm(markets)
    assert len(selected) == 2
    assert selected[0].condition_id in ("m1", "m2")


def test_get_markets_to_mm_excludes_edge():
    coord = _make_coordinator(max_mm_markets=5)
    coord.record_edge_entry("m1")
    markets = [
        _make_market(cid="m1", spread=0.10, liquidity=1000),
        _make_market(cid="m2", spread=0.05, liquidity=2000),
    ]
    selected = coord.get_markets_to_mm(markets)
    assert all(m.condition_id != "m1" for m in selected)


def test_summary():
    coord = _make_coordinator()
    coord.record_edge_entry("m1")
    coord.record_arb_entry("m2", 50.0)
    s = coord.summary()
    assert s["edge_markets"] == 1
    assert s["arb_markets"] == 1
    assert s["arb_exposure"] == 50.0
    assert "mm_exposure" in s
    assert "open_orders" in s


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
