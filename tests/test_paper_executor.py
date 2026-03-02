"""Tests for the paper trading executor."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.types import Outcome, Side, ExitReason, StrategyType
from execution.paper_executor import PaperExecutor


def test_buy_deducts_balance():
    exe = PaperExecutor(initial_balance=1000.0, slippage_bps=0, fee_bps=0)
    exe.buy("m1", "t1", Outcome.YES, price=0.50, size=10.0)
    # Cost = 10 * 0.50 = $5.00
    assert abs(exe.balance - 995.0) < 0.01


def test_buy_with_fees():
    exe = PaperExecutor(initial_balance=1000.0, slippage_bps=0, fee_bps=200)
    exe.buy("m1", "t1", Outcome.YES, price=0.50, size=10.0)
    # Cost = 10 * 0.50 = $5.00, fee = $5 * 0.02 = $0.10
    assert abs(exe.balance - 994.90) < 0.01


def test_buy_creates_position():
    exe = PaperExecutor(initial_balance=1000.0, slippage_bps=0, fee_bps=0)
    exe.buy("m1", "t1", Outcome.YES, price=0.50, size=10.0)
    positions = exe.get_positions()
    assert len(positions) == 1
    assert positions[0].outcome == Outcome.YES
    assert positions[0].size == 10.0


def test_sell_closes_position():
    exe = PaperExecutor(initial_balance=1000.0, slippage_bps=0, fee_bps=0)
    exe.buy("m1", "t1", Outcome.YES, price=0.50, size=10.0)
    exe.sell("m1", "t1", Outcome.YES, price=0.60, size=10.0)
    assert len(exe.get_positions()) == 0


def test_profitable_round_trip():
    exe = PaperExecutor(initial_balance=1000.0, slippage_bps=0, fee_bps=0)
    exe.buy("m1", "t1", Outcome.YES, price=0.50, size=10.0)
    exe.sell("m1", "t1", Outcome.YES, price=0.60, size=10.0)
    # Bought at 0.50, sold at 0.60 = +$0.10 * 10 = $1.00 profit
    assert exe.balance > 1000.0
    assert abs(exe.total_pnl - 1.0) < 0.01


def test_losing_round_trip():
    exe = PaperExecutor(initial_balance=1000.0, slippage_bps=0, fee_bps=0)
    exe.buy("m1", "t1", Outcome.YES, price=0.50, size=10.0)
    exe.sell("m1", "t1", Outcome.YES, price=0.40, size=10.0)
    # Bought at 0.50, sold at 0.40 = -$0.10 * 10 = -$1.00 loss
    assert exe.balance < 1000.0
    assert abs(exe.total_pnl - (-1.0)) < 0.01


def test_multiple_positions():
    exe = PaperExecutor(initial_balance=1000.0, slippage_bps=0, fee_bps=0)
    exe.buy("m1", "t1", Outcome.YES, price=0.50, size=10.0)
    exe.buy("m2", "t2", Outcome.NO, price=0.30, size=20.0)
    assert len(exe.get_positions()) == 2


def test_position_averaging():
    exe = PaperExecutor(initial_balance=1000.0, slippage_bps=0, fee_bps=0)
    exe.buy("m1", "t1", Outcome.YES, price=0.40, size=10.0)
    exe.buy("m1", "t1", Outcome.YES, price=0.60, size=10.0)
    positions = exe.get_positions()
    assert len(positions) == 1
    assert positions[0].size == 20.0
    assert abs(positions[0].entry_price - 0.50) < 0.01  # Average of 0.40 and 0.60


def test_resolution_win():
    exe = PaperExecutor(initial_balance=1000.0, slippage_bps=0, fee_bps=0)
    exe.buy("m1", "t1", Outcome.YES, price=0.50, size=10.0)
    exe.resolve_position("m1", Outcome.YES, "YES")
    # Won: receive $10 (1.0 per share), paid $5 = +$5 profit
    assert len(exe.get_positions()) == 0
    assert exe.balance > 1000.0


def test_resolution_loss():
    exe = PaperExecutor(initial_balance=1000.0, slippage_bps=0, fee_bps=0)
    exe.buy("m1", "t1", Outcome.YES, price=0.50, size=10.0)
    exe.resolve_position("m1", Outcome.YES, "NO")
    # Lost: receive $0, paid $5 = -$5 loss
    assert len(exe.get_positions()) == 0
    assert exe.balance < 1000.0


def test_insufficient_balance():
    exe = PaperExecutor(initial_balance=10.0, slippage_bps=0, fee_bps=0)
    result = exe.buy("m1", "t1", Outcome.YES, price=0.50, size=100.0)
    # Should reduce size to fit balance
    assert result.size < 100.0
    assert exe.balance >= 0


def test_summary():
    exe = PaperExecutor(initial_balance=1000.0)
    summary = exe.summary()
    assert summary["balance"] == 1000.0
    assert summary["total_pnl"] == 0.0
    assert summary["open_positions"] == 0


def test_daily_reset():
    exe = PaperExecutor(initial_balance=1000.0, slippage_bps=0, fee_bps=0)
    exe.buy("m1", "t1", Outcome.YES, price=0.50, size=10.0)
    exe.sell("m1", "t1", Outcome.YES, price=0.60, size=10.0)
    assert exe.daily_pnl != 0
    exe.reset_daily()
    assert exe.daily_pnl == 0.0
    assert exe.daily_trades == 0


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
