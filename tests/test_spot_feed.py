"""Tests for BinanceSpotFeed price buffer and momentum calculation."""

import time

import pytest

from data.spot_feed import BinanceSpotFeed, BUFFER_SECONDS


class TestMomentumCalculation:
    def test_momentum_with_known_prices(self):
        """Feed with known price history should return correct momentum."""
        feed = BinanceSpotFeed("btcusdt")

        # Manually inject price buffer (bypass WebSocket)
        now = time.time()
        from datetime import datetime, timezone

        with feed._lock:
            # Price went from 50000 to 50100 over ~28 seconds
            # Use now-28 (not now-30) so it's safely inside the 30s window
            feed._price_buffer.append((now - 28, 50000.0))
            feed._price_buffer.append((now - 20, 50050.0))
            feed._price_buffer.append((now - 10, 50075.0))
            feed._price_buffer.append((now, 50100.0))
            feed._latest_price = 50100.0
            feed._latest_ts = datetime.now(timezone.utc)

        momentum = feed.get_momentum(window_secs=30)
        assert momentum is not None
        # 50100 / 50000 - 1 = 0.002 = 0.2%
        assert abs(momentum - 0.002) < 0.0001

    def test_momentum_negative(self):
        """Negative momentum when price drops."""
        feed = BinanceSpotFeed("btcusdt")

        now = time.time()
        from datetime import datetime, timezone

        with feed._lock:
            # Use now-28 so it's safely inside the 30s window
            feed._price_buffer.append((now - 28, 50000.0))
            feed._price_buffer.append((now, 49900.0))
            feed._latest_price = 49900.0
            feed._latest_ts = datetime.now(timezone.utc)

        momentum = feed.get_momentum(window_secs=30)
        assert momentum is not None
        assert momentum < 0
        # 49900 / 50000 - 1 = -0.002
        assert abs(momentum - (-0.002)) < 0.0001

    def test_momentum_none_empty_buffer(self):
        """No data → None momentum."""
        feed = BinanceSpotFeed("btcusdt")
        assert feed.get_momentum() is None


class TestPriceBuffer:
    def test_buffer_rolling(self):
        """Buffer should not grow beyond BUFFER_SECONDS."""
        feed = BinanceSpotFeed("btcusdt")

        from datetime import datetime, timezone

        now = time.time()

        # Add entries spanning more than BUFFER_SECONDS
        with feed._lock:
            for i in range(200):
                ts = now - BUFFER_SECONDS - 50 + i  # Start 50s before cutoff
                feed._price_buffer.append((ts, 50000.0 + i))

        # Trigger cleanup via _on_trade
        feed._on_trade(51000.0, datetime.now(timezone.utc))

        # Buffer should have trimmed old entries
        with feed._lock:
            oldest_ts = feed._price_buffer[0][0]
            assert now - oldest_ts <= BUFFER_SECONDS + 1  # +1 for timing slack

    def test_get_price_history(self):
        """get_price_history returns entries within time window."""
        feed = BinanceSpotFeed("btcusdt")

        now = time.time()
        with feed._lock:
            feed._price_buffer.append((now - 90, 50000.0))  # Outside 60s window
            feed._price_buffer.append((now - 30, 50050.0))  # Inside 60s window
            feed._price_buffer.append((now - 10, 50100.0))  # Inside 60s window

        history = feed.get_price_history(seconds=60)
        assert len(history) == 2

    def test_get_price_returns_none_initially(self):
        """get_price returns None before any data arrives."""
        feed = BinanceSpotFeed("btcusdt")
        assert feed.get_price() is None
        assert not feed.connected
