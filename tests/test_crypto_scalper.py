"""Tests for crypto scalper strategy V2 and helpers."""

import time

import pytest

from core.types import Outcome, SignalAction, StrategyType
from runners.run_crypto import current_window_slug, window_seconds_remaining
from strategies.crypto_scalper import CryptoScalper, crypto_fee_rate


@pytest.fixture
def scalper():
    return CryptoScalper(
        min_momentum=0.002,      # 20bps
        min_entry_price=0.05,
        max_entry_price=0.55,
        base_position_size=20.0,
        entry_window_secs=60,
    )


class TestCryptoScalperSignals:
    def test_signal_generated_on_strong_momentum(self, scalper):
        """Strong BTC move + reasonable price → Signal."""
        signal = scalper.evaluate(
            spot_momentum=0.002,  # 20bps, 2x threshold
            window_seconds_remaining=30,
            up_price=0.30,        # Reasonable entry price
            down_price=0.70,
            up_token_id="up_token_123",
            down_token_id="down_token_456",
            market_id="market_001",
        )
        assert signal is not None
        assert signal.action == SignalAction.BUY
        assert signal.strategy == StrategyType.CRYPTO_SCALPER
        assert signal.market_id == "market_001"
        assert signal.edge > 0

    def test_no_signal_weak_momentum(self, scalper):
        """Weak BTC move (10bps < 20bps threshold) → None."""
        signal = scalper.evaluate(
            spot_momentum=0.001,  # 10bps, below 20bps threshold
            window_seconds_remaining=30,
            up_price=0.30,
            down_price=0.70,
            up_token_id="up_token_123",
            down_token_id="down_token_456",
            market_id="market_001",
        )
        assert signal is None

    def test_no_signal_outside_entry_window(self, scalper):
        """5 minutes remaining → None (outside 60s entry window)."""
        signal = scalper.evaluate(
            spot_momentum=0.002,
            window_seconds_remaining=300,  # 5 min > 60s window
            up_price=0.30,
            down_price=0.70,
            up_token_id="up_token_123",
            down_token_id="down_token_456",
            market_id="market_001",
        )
        assert signal is None

    def test_no_signal_price_too_high(self, scalper):
        """Up token at $0.60 → None (max_entry_price = 0.55)."""
        signal = scalper.evaluate(
            spot_momentum=0.002,
            window_seconds_remaining=30,
            up_price=0.60,
            down_price=0.40,
            up_token_id="up_token_123",
            down_token_id="down_token_456",
            market_id="market_001",
        )
        assert signal is None

    def test_no_signal_price_too_low(self, scalper):
        """Up token at $0.03 → None (min_entry_price = 0.05)."""
        signal = scalper.evaluate(
            spot_momentum=0.002,
            window_seconds_remaining=30,
            up_price=0.03,
            down_price=0.97,
            up_token_id="up_token_123",
            down_token_id="down_token_456",
            market_id="market_001",
        )
        assert signal is None

    def test_no_signal_none_momentum(self, scalper):
        """None momentum (no data) → None."""
        signal = scalper.evaluate(
            spot_momentum=None,
            window_seconds_remaining=30,
            up_price=0.30,
            down_price=0.70,
            up_token_id="up_token_123",
            down_token_id="down_token_456",
            market_id="market_001",
        )
        assert signal is None

    def test_direction_up_buys_up_token(self, scalper):
        """Positive momentum → BUY Up token (YES outcome)."""
        signal = scalper.evaluate(
            spot_momentum=0.002,
            window_seconds_remaining=30,
            up_price=0.30,
            down_price=0.70,
            up_token_id="up_token_123",
            down_token_id="down_token_456",
            market_id="market_001",
        )
        assert signal is not None
        assert signal.outcome == Outcome.YES
        assert signal.metadata["direction"] == "UP"
        assert signal.metadata["token_id"] == "up_token_123"

    def test_direction_down_buys_down_token(self, scalper):
        """Negative momentum → BUY Down token (NO outcome)."""
        signal = scalper.evaluate(
            spot_momentum=-0.002,
            window_seconds_remaining=30,
            up_price=0.70,
            down_price=0.30,
            up_token_id="up_token_123",
            down_token_id="down_token_456",
            market_id="market_001",
        )
        assert signal is not None
        assert signal.outcome == Outcome.NO
        assert signal.metadata["direction"] == "DOWN"
        assert signal.metadata["token_id"] == "down_token_456"

    def test_position_size_scales_with_momentum(self, scalper):
        """Stronger momentum → larger position (up to 2x base)."""
        # 1x threshold → 1x size
        sig_1x = scalper.evaluate(
            spot_momentum=0.002,  # Exactly at threshold
            window_seconds_remaining=30,
            up_price=0.30,
            down_price=0.70,
            up_token_id="up_token_123",
            down_token_id="down_token_456",
            market_id="market_001",
        )
        assert sig_1x is not None
        assert sig_1x.size == pytest.approx(20.0, rel=0.01)

        # 2x threshold → 2x size
        sig_2x = scalper.evaluate(
            spot_momentum=0.004,  # 2x threshold
            window_seconds_remaining=30,
            up_price=0.30,
            down_price=0.70,
            up_token_id="up_token_123",
            down_token_id="down_token_456",
            market_id="market_001",
        )
        assert sig_2x is not None
        assert sig_2x.size == pytest.approx(40.0, rel=0.01)

        # 5x threshold → capped at 2x size
        sig_5x = scalper.evaluate(
            spot_momentum=0.010,  # 5x threshold
            window_seconds_remaining=30,
            up_price=0.30,
            down_price=0.70,
            up_token_id="up_token_123",
            down_token_id="down_token_456",
            market_id="market_001",
        )
        assert sig_5x is not None
        assert sig_5x.size == pytest.approx(40.0, rel=0.01)  # Capped at 2x

    def test_edge_is_fee_adjusted(self, scalper):
        """Edge should be profit_if_win - loss_if_lose."""
        signal = scalper.evaluate(
            spot_momentum=0.002,
            window_seconds_remaining=30,
            up_price=0.30,
            down_price=0.70,
            up_token_id="up_token_123",
            down_token_id="down_token_456",
            market_id="market_001",
        )
        assert signal is not None
        fee = crypto_fee_rate(0.30)
        expected_edge = (1.0 - 0.30 - fee) - (0.30 + fee)
        assert abs(signal.edge - expected_edge) < 0.001


class TestFeeCalculation:
    def test_fee_at_midpoint(self):
        """Fee at p=0.50 should be ~1.5625%."""
        fee = crypto_fee_rate(0.50)
        assert abs(fee - 0.015625) < 0.0001

    def test_fee_at_90_cents(self):
        """Fee at p=0.90 should be ~0.20%."""
        fee = crypto_fee_rate(0.90)
        assert fee < 0.003  # Less than 0.3%
        assert fee > 0.0001  # But not zero

    def test_fee_at_95_cents(self):
        """Fee at p=0.95 should be very low (~0.06%)."""
        fee = crypto_fee_rate(0.95)
        assert fee < 0.001  # Less than 0.1%

    def test_fee_at_boundaries(self):
        """Fee at p=0 and p=1 should be 0."""
        assert crypto_fee_rate(0.0) == 0.0
        assert crypto_fee_rate(1.0) == 0.0

    def test_fee_symmetry(self):
        """Fee at p=0.3 should equal fee at p=0.7."""
        assert abs(crypto_fee_rate(0.3) - crypto_fee_rate(0.7)) < 1e-10


class TestSlugGeneration:
    def test_slug_format(self):
        """Slug should match expected format."""
        slug = current_window_slug("btc", 900)
        assert slug.startswith("btc-updown-15m-")
        # Timestamp part should be a valid integer
        ts_str = slug.split("-")[-1]
        ts = int(ts_str)
        assert ts > 0
        # Should be aligned to 15-min boundary
        assert ts % 900 == 0

    def test_slug_different_assets(self):
        """Different assets produce different slugs."""
        btc_slug = current_window_slug("btc", 900)
        eth_slug = current_window_slug("eth", 900)
        assert btc_slug.startswith("btc-")
        assert eth_slug.startswith("eth-")

    def test_slug_different_intervals(self):
        """Different intervals produce different slugs."""
        slug_15m = current_window_slug("btc", 900)
        slug_5m = current_window_slug("btc", 300)
        assert "15m" in slug_15m
        assert "5m" in slug_5m

    def test_window_seconds_remaining(self):
        """Seconds remaining should be between 0 and interval."""
        remaining = window_seconds_remaining(900)
        assert 0 <= remaining <= 900
