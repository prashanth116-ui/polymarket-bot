"""Tests for position reconciliation."""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.types import Outcome, Position, Side, StrategyType
from execution.reconciler import PositionReconciler, ReconciliationResult


def _make_position(
    market_id="m1",
    outcome=Outcome.YES,
    size=10.0,
    entry_price=0.50,
):
    return Position(
        market_id=market_id,
        condition_id=market_id,
        outcome=outcome,
        token_id=f"tok-{market_id}-{outcome.value}",
        side=Side.BUY,
        entry_price=entry_price,
        size=size,
        cost_basis=size * entry_price,
        current_price=entry_price,
        strategy=StrategyType.EDGE,
    )


def test_empty_positions():
    recon = PositionReconciler()
    result = recon.reconcile([], [])
    assert not result.has_mismatches
    assert result.mismatch_count == 0
    assert len(result.matched) == 0


def test_all_matched():
    local = [_make_position("m1", Outcome.YES, 10.0)]
    bridge = [_make_position("m1", Outcome.YES, 10.0)]
    recon = PositionReconciler()
    result = recon.reconcile(local, bridge)

    assert not result.has_mismatches
    assert len(result.matched) == 1
    assert "m1:YES" in result.matched


def test_local_only():
    local = [_make_position("m1", Outcome.YES, 10.0)]
    bridge = []
    recon = PositionReconciler()
    result = recon.reconcile(local, bridge)

    assert result.has_mismatches
    assert len(result.local_only) == 1
    assert result.local_only[0].market_id == "m1"
    assert result.local_only[0].mismatch_type == "local_only"
    assert result.local_only[0].local_size == 10.0
    assert result.local_only[0].bridge_size == 0.0


def test_bridge_only():
    local = []
    bridge = [_make_position("m2", Outcome.NO, 20.0)]
    recon = PositionReconciler()
    result = recon.reconcile(local, bridge)

    assert result.has_mismatches
    assert len(result.bridge_only) == 1
    assert result.bridge_only[0].market_id == "m2"
    assert result.bridge_only[0].outcome == Outcome.NO
    assert result.bridge_only[0].bridge_size == 20.0


def test_size_mismatch():
    local = [_make_position("m1", Outcome.YES, 10.0)]
    bridge = [_make_position("m1", Outcome.YES, 15.0)]
    recon = PositionReconciler()
    result = recon.reconcile(local, bridge)

    assert result.has_mismatches
    assert len(result.size_mismatches) == 1
    mm = result.size_mismatches[0]
    assert mm.local_size == 10.0
    assert mm.bridge_size == 15.0
    assert mm.mismatch_type == "size_mismatch"


def test_small_size_difference_is_match():
    """Sizes within 0.01 tolerance count as matched."""
    local = [_make_position("m1", Outcome.YES, 10.0)]
    bridge = [_make_position("m1", Outcome.YES, 10.005)]
    recon = PositionReconciler()
    result = recon.reconcile(local, bridge)

    assert not result.has_mismatches
    assert len(result.matched) == 1


def test_mixed_mismatches():
    local = [
        _make_position("m1", Outcome.YES, 10.0),  # matched
        _make_position("m2", Outcome.NO, 5.0),     # local only
        _make_position("m3", Outcome.YES, 20.0),   # size mismatch
    ]
    bridge = [
        _make_position("m1", Outcome.YES, 10.0),   # matched
        _make_position("m3", Outcome.YES, 25.0),    # size mismatch
        _make_position("m4", Outcome.YES, 30.0),    # bridge only
    ]
    recon = PositionReconciler()
    result = recon.reconcile(local, bridge)

    assert result.has_mismatches
    assert len(result.matched) == 1
    assert len(result.local_only) == 1
    assert len(result.bridge_only) == 1
    assert len(result.size_mismatches) == 1
    assert result.mismatch_count == 3


def test_multiple_outcomes_same_market():
    local = [
        _make_position("m1", Outcome.YES, 10.0),
        _make_position("m1", Outcome.NO, 5.0),
    ]
    bridge = [
        _make_position("m1", Outcome.YES, 10.0),
        _make_position("m1", Outcome.NO, 5.0),
    ]
    recon = PositionReconciler()
    result = recon.reconcile(local, bridge)

    assert not result.has_mismatches
    assert len(result.matched) == 2


def test_should_reconcile_timing():
    recon = PositionReconciler(reconcile_interval=0.1)
    assert recon.should_reconcile() is True

    # Reconcile updates the timer
    recon.reconcile([], [])
    assert recon.should_reconcile() is False

    time.sleep(0.15)
    assert recon.should_reconcile() is True


def test_summary_string():
    result = ReconciliationResult(
        matched=["m1:YES"],
        local_only=[],
        bridge_only=[],
        size_mismatches=[],
    )
    assert "matched=1" in result.summary()

    result2 = ReconciliationResult(
        matched=["m1:YES"],
        local_only=[],
        bridge_only=[],
        size_mismatches=[],
    )
    assert "local_only" not in result2.summary()  # Empty lists not shown


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
