"""Tests for crypto scalper strategy V3 (contrarian) and helpers."""

import time

import pytest

from core.types import Outcome, SignalAction, StrategyType
from runners.run_crypto import current_window_slug, window_seconds_remaining
from strategies.crypto_scalper import CryptoScalper, crypto_fee_rate


@pytest.fixture
def scalper():
    return CryptoScalper(
        min_streak=2,
        min_entry_price=0.05,
        max_entry_price=0.55,
        base_position_size=20.0,
        entry_window_secs=300,
    )


class TestContrarianSignals:
    def test_signal_after_2_streak(self, scalper):
        """After 2x UP streak → signal to buy DOWN token."""
        signal = scalper.evaluate(
            streak_direction="UP",
            streak_length=2,
            window_seconds_remaining=200,
            up_price=0.60,
            down_price=0.40,
            up_token_id="up_token_123",
            down_token_id="down_token_456",
            market_id="market_001",
        )
        assert signal is not None
        assert signal.action == SignalAction.BUY
        assert signal.strategy == StrategyType.CRYPTO_SCALPER
        assert signal.outcome == Outcome.NO  # DOWN maps to NO
        assert signal.metadata["direction"] == "DOWN"
        assert signal.metadata["token_id"] == "down_token_456"
        assert signal.metadata["streak_length"] == 2

    def test_signal_after_3_down_streak(self, scalper):
        """After 3x DOWN streak → signal to buy UP token."""
        signal = scalper.evaluate(
            streak_direction="DOWN",
            streak_length=3,
            window_seconds_remaining=200,
            up_price=0.40,
            down_price=0.60,
            up_token_id="up_token_123",
            down_token_id="down_token_456",
            market_id="market_001",
        )
        assert signal is not None
        assert signal.outcome == Outcome.YES  # UP maps to YES
        assert signal.metadata["direction"] == "UP"
        assert signal.metadata["token_id"] == "up_token_123"

    def test_no_signal_streak_too_short(self, scalper):
        """Streak of 1 (below min_streak=2) → None."""
        signal = scalper.evaluate(
            streak_direction="UP",
            streak_length=1,
            window_seconds_remaining=200,
            up_price=0.60,
            down_price=0.40,
            up_token_id="up_token_123",
            down_token_id="down_token_456",
            market_id="market_001",
        )
        assert signal is None

    def test_no_signal_no_streak(self, scalper):
        """No streak data → None."""
        signal = scalper.evaluate(
            streak_direction=None,
            streak_length=0,
            window_seconds_remaining=200,
            up_price=0.50,
            down_price=0.50,
            up_token_id="up_token_123",
            down_token_id="down_token_456",
            market_id="market_001",
        )
        assert signal is None

    def test_no_signal_outside_entry_window(self, scalper):
        """10 min remaining → None (entry window is 300s)."""
        signal = scalper.evaluate(
            streak_direction="UP",
            streak_length=3,
            window_seconds_remaining=600,
            up_price=0.60,
            down_price=0.40,
            up_token_id="up_token_123",
            down_token_id="down_token_456",
            market_id="market_001",
        )
        assert signal is None

    def test_no_signal_price_too_high(self, scalper):
        """Target token at $0.60 → None (max_entry_price = 0.55)."""
        signal = scalper.evaluate(
            streak_direction="UP",
            streak_length=2,
            window_seconds_remaining=200,
            up_price=0.40,
            down_price=0.60,  # Would buy DOWN at $0.60 > $0.55 max
            up_token_id="up_token_123",
            down_token_id="down_token_456",
            market_id="market_001",
        )
        assert signal is None

    def test_no_signal_price_too_low(self, scalper):
        """Target token at $0.03 → None (min_entry_price = 0.05)."""
        signal = scalper.evaluate(
            streak_direction="DOWN",
            streak_length=2,
            window_seconds_remaining=200,
            up_price=0.03,  # Would buy UP at $0.03 < $0.05 min
            down_price=0.97,
            up_token_id="up_token_123",
            down_token_id="down_token_456",
            market_id="market_001",
        )
        assert signal is None

    def test_no_signal_negative_edge(self, scalper):
        """Target price > $0.50 after fees → negative edge → None."""
        # Create scalper with high max_entry_price to test edge calc
        wide_scalper = CryptoScalper(
            min_streak=2,
            min_entry_price=0.05,
            max_entry_price=0.95,
            base_position_size=20.0,
            entry_window_secs=300,
        )
        signal = wide_scalper.evaluate(
            streak_direction="UP",
            streak_length=2,
            window_seconds_remaining=200,
            up_price=0.30,
            down_price=0.70,  # Would buy DOWN at $0.70 — negative edge
            up_token_id="up_token_123",
            down_token_id="down_token_456",
            market_id="market_001",
        )
        assert signal is None


class TestPositionSizing:
    def test_size_scales_with_streak(self, scalper):
        """Longer streak → larger position (up to 2x)."""
        # streak=2 (min_streak): 1x base
        sig_2 = scalper.evaluate(
            streak_direction="UP",
            streak_length=2,
            window_seconds_remaining=200,
            up_price=0.60,
            down_price=0.40,
            up_token_id="up", down_token_id="down",
            market_id="mkt",
        )
        assert sig_2 is not None
        assert sig_2.size == pytest.approx(20.0, rel=0.01)  # 1.0x

        # streak=3: 1.5x base
        sig_3 = scalper.evaluate(
            streak_direction="UP",
            streak_length=3,
            window_seconds_remaining=200,
            up_price=0.60,
            down_price=0.40,
            up_token_id="up", down_token_id="down",
            market_id="mkt",
        )
        assert sig_3 is not None
        assert sig_3.size == pytest.approx(30.0, rel=0.01)  # 1.5x

        # streak=4: 2x base (capped)
        sig_4 = scalper.evaluate(
            streak_direction="UP",
            streak_length=4,
            window_seconds_remaining=200,
            up_price=0.60,
            down_price=0.40,
            up_token_id="up", down_token_id="down",
            market_id="mkt",
        )
        assert sig_4 is not None
        assert sig_4.size == pytest.approx(40.0, rel=0.01)  # 2.0x capped

        # streak=10: still 2x (capped)
        sig_10 = scalper.evaluate(
            streak_direction="UP",
            streak_length=10,
            window_seconds_remaining=200,
            up_price=0.60,
            down_price=0.40,
            up_token_id="up", down_token_id="down",
            market_id="mkt",
        )
        assert sig_10 is not None
        assert sig_10.size == pytest.approx(40.0, rel=0.01)  # 2.0x capped

    def test_edge_is_fee_adjusted(self, scalper):
        """Edge should be profit_if_win - loss_if_lose."""
        signal = scalper.evaluate(
            streak_direction="UP",
            streak_length=2,
            window_seconds_remaining=200,
            up_price=0.60,
            down_price=0.40,
            up_token_id="up", down_token_id="down",
            market_id="mkt",
        )
        assert signal is not None
        fee = crypto_fee_rate(0.40)  # Buying DOWN token at $0.40
        expected_edge = (1.0 - 0.40 - fee) - (0.40 + fee)
        assert abs(signal.edge - expected_edge) < 0.001


class TestFeeCalculation:
    def test_fee_at_midpoint(self):
        fee = crypto_fee_rate(0.50)
        assert abs(fee - 0.015625) < 0.0001

    def test_fee_at_90_cents(self):
        fee = crypto_fee_rate(0.90)
        assert fee < 0.003
        assert fee > 0.0001

    def test_fee_at_boundaries(self):
        assert crypto_fee_rate(0.0) == 0.0
        assert crypto_fee_rate(1.0) == 0.0

    def test_fee_symmetry(self):
        assert abs(crypto_fee_rate(0.3) - crypto_fee_rate(0.7)) < 1e-10


class TestSlugGeneration:
    def test_slug_format(self):
        slug = current_window_slug("btc", 900)
        assert slug.startswith("btc-updown-15m-")
        ts_str = slug.split("-")[-1]
        ts = int(ts_str)
        assert ts > 0
        assert ts % 900 == 0

    def test_slug_different_assets(self):
        btc_slug = current_window_slug("btc", 900)
        eth_slug = current_window_slug("eth", 900)
        assert btc_slug.startswith("btc-")
        assert eth_slug.startswith("eth-")

    def test_slug_different_intervals(self):
        slug_15m = current_window_slug("btc", 900)
        slug_5m = current_window_slug("btc", 300)
        assert "15m" in slug_15m
        assert "5m" in slug_5m

    def test_window_seconds_remaining(self):
        remaining = window_seconds_remaining(900)
        assert 0 <= remaining <= 900
