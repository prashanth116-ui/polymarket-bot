"""Tests for Kelly criterion calculations."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.kelly import kelly_fraction, size_position, expected_value


def test_kelly_positive_edge():
    """When true prob > market price, Kelly should be positive."""
    f = kelly_fraction(true_prob=0.70, market_price=0.50, fee_bps=0)
    assert f > 0
    # f* = (0.70 - 0.50) / (1 - 0.50) = 0.40
    assert abs(f - 0.40) < 0.01


def test_kelly_no_edge():
    """When true prob <= market price, Kelly should be zero."""
    f = kelly_fraction(true_prob=0.50, market_price=0.50, fee_bps=0)
    assert f == 0.0


def test_kelly_negative_edge():
    """When true prob < market price, Kelly should be zero."""
    f = kelly_fraction(true_prob=0.30, market_price=0.50, fee_bps=0)
    assert f == 0.0


def test_kelly_with_fees():
    """Fees should reduce the Kelly fraction."""
    f_no_fee = kelly_fraction(true_prob=0.70, market_price=0.50, fee_bps=0)
    f_with_fee = kelly_fraction(true_prob=0.70, market_price=0.50, fee_bps=200)
    assert f_with_fee < f_no_fee
    assert f_with_fee > 0


def test_kelly_boundary_prices():
    """Kelly should return 0 for boundary prices."""
    assert kelly_fraction(0.5, 0.0) == 0.0
    assert kelly_fraction(0.5, 1.0) == 0.0
    assert kelly_fraction(0.0, 0.5) == 0.0
    assert kelly_fraction(1.0, 0.5) == 0.0


def test_kelly_near_certain():
    """High confidence at low price should give large Kelly."""
    f = kelly_fraction(true_prob=0.95, market_price=0.10, fee_bps=0)
    # f* = (0.95 - 0.10) / (1 - 0.10) = 0.944
    assert f > 0.9


def test_size_position_basic():
    """Quarter Kelly sizing with $1000 bankroll."""
    size = size_position(
        bankroll=1000.0,
        true_prob=0.70,
        market_price=0.50,
        kelly_mult=0.25,
        fee_bps=0,
    )
    # Full Kelly = 40% of $1000 = $400
    # Quarter Kelly = $100
    assert abs(size - 100.0) < 1.0


def test_size_position_with_cap():
    """Position size should respect max_position cap."""
    size = size_position(
        bankroll=10000.0,
        true_prob=0.90,
        market_price=0.50,
        kelly_mult=0.25,
        max_position=100.0,
        fee_bps=0,
    )
    assert size <= 100.0


def test_size_position_zero_bankroll():
    """Zero bankroll should give zero size."""
    size = size_position(bankroll=0, true_prob=0.70, market_price=0.50)
    assert size == 0.0


def test_expected_value_positive():
    """EV should be positive when true_prob > market_price."""
    ev = expected_value(true_prob=0.70, market_price=0.50, size=100, fee_bps=0)
    # EV = 0.70 * 100 - 100 * 0.50 = 70 - 50 = 20
    assert abs(ev - 20.0) < 0.01


def test_expected_value_negative():
    """EV should be negative when true_prob < market_price."""
    ev = expected_value(true_prob=0.30, market_price=0.50, size=100, fee_bps=0)
    # EV = 0.30 * 100 - 100 * 0.50 = 30 - 50 = -20
    assert abs(ev - (-20.0)) < 0.01


def test_expected_value_with_fees():
    """Fees should reduce EV."""
    ev_no_fee = expected_value(true_prob=0.70, market_price=0.50, size=100, fee_bps=0)
    ev_with_fee = expected_value(true_prob=0.70, market_price=0.50, size=100, fee_bps=200)
    assert ev_with_fee < ev_no_fee


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
