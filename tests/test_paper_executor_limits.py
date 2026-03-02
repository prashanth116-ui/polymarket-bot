"""Tests for paper executor limit order support."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.types import OpenOrder, OrderStatus, Outcome, Side, StrategyType
from execution.paper_executor import PaperExecutor


def test_place_limit_order_returns_id():
    exe = PaperExecutor(initial_balance=1000.0, slippage_bps=0, fee_bps=0)
    oid = exe.place_limit_order("m1", "t1", Outcome.YES, Side.BUY, 0.50, 10.0)
    assert oid is not None
    assert oid.startswith("paper-")


def test_place_limit_order_tracked():
    exe = PaperExecutor(initial_balance=1000.0, slippage_bps=0, fee_bps=0)
    exe.place_limit_order("m1", "t1", Outcome.YES, Side.BUY, 0.50, 10.0)
    orders = exe.get_open_orders()
    assert len(orders) == 1
    assert orders[0].side == Side.BUY
    assert orders[0].price == 0.50
    assert orders[0].size == 10.0


def test_limit_order_does_not_deduct_balance():
    exe = PaperExecutor(initial_balance=1000.0, slippage_bps=0, fee_bps=0)
    exe.place_limit_order("m1", "t1", Outcome.YES, Side.BUY, 0.50, 10.0)
    # Balance unchanged until fill
    assert exe.balance == 1000.0


def test_limit_buy_fills_when_price_drops():
    exe = PaperExecutor(initial_balance=1000.0, slippage_bps=0, fee_bps=0)
    exe.place_limit_order("m1", "t1", Outcome.YES, Side.BUY, 0.50, 10.0)
    fills = exe.check_limit_fills("t1", current_price=0.48)
    assert len(fills) == 1
    assert fills[0].side == Side.BUY
    assert fills[0].price == 0.50
    # Position should exist
    assert len(exe.get_positions()) == 1


def test_limit_buy_no_fill_when_price_above():
    exe = PaperExecutor(initial_balance=1000.0, slippage_bps=0, fee_bps=0)
    exe.place_limit_order("m1", "t1", Outcome.YES, Side.BUY, 0.50, 10.0)
    fills = exe.check_limit_fills("t1", current_price=0.55)
    assert len(fills) == 0
    assert len(exe.get_positions()) == 0


def test_limit_sell_fills_when_price_rises():
    exe = PaperExecutor(initial_balance=1000.0, slippage_bps=0, fee_bps=0)
    # First buy to have a position to sell
    exe.buy("m1", "t1", Outcome.YES, price=0.40, size=10.0)
    # Place limit sell
    exe.place_limit_order("m1", "t1", Outcome.YES, Side.SELL, 0.60, 10.0)
    fills = exe.check_limit_fills("t1", current_price=0.65)
    assert len(fills) == 1
    assert fills[0].side == Side.SELL


def test_limit_sell_no_fill_when_price_below():
    exe = PaperExecutor(initial_balance=1000.0, slippage_bps=0, fee_bps=0)
    exe.buy("m1", "t1", Outcome.YES, price=0.40, size=10.0)
    exe.place_limit_order("m1", "t1", Outcome.YES, Side.SELL, 0.60, 10.0)
    fills = exe.check_limit_fills("t1", current_price=0.55)
    assert len(fills) == 0


def test_cancel_all_orders():
    exe = PaperExecutor(initial_balance=1000.0, slippage_bps=0, fee_bps=0)
    exe.place_limit_order("m1", "t1", Outcome.YES, Side.BUY, 0.50, 10.0)
    exe.place_limit_order("m2", "t2", Outcome.NO, Side.BUY, 0.30, 20.0)
    cancelled = exe.cancel_all_orders()
    assert cancelled == 2
    assert len(exe.get_open_orders()) == 0


def test_cancel_orders_by_market():
    exe = PaperExecutor(initial_balance=1000.0, slippage_bps=0, fee_bps=0)
    exe.place_limit_order("m1", "t1", Outcome.YES, Side.BUY, 0.50, 10.0)
    exe.place_limit_order("m2", "t2", Outcome.NO, Side.BUY, 0.30, 20.0)
    cancelled = exe.cancel_all_orders(market_id="m1")
    assert cancelled == 1
    orders = exe.get_open_orders()
    assert len(orders) == 1
    assert orders[0].market_id == "m2"


def test_filled_order_removed_from_open():
    exe = PaperExecutor(initial_balance=1000.0, slippage_bps=0, fee_bps=0)
    exe.place_limit_order("m1", "t1", Outcome.YES, Side.BUY, 0.50, 10.0)
    exe.check_limit_fills("t1", current_price=0.45)
    assert len(exe.get_open_orders()) == 0


def test_get_open_orders_filter_by_market():
    exe = PaperExecutor(initial_balance=1000.0, slippage_bps=0, fee_bps=0)
    exe.place_limit_order("m1", "t1", Outcome.YES, Side.BUY, 0.50, 10.0)
    exe.place_limit_order("m2", "t2", Outcome.NO, Side.BUY, 0.30, 20.0)
    orders_m1 = exe.get_open_orders(market_id="m1")
    assert len(orders_m1) == 1
    assert orders_m1[0].market_id == "m1"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
