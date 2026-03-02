"""Tests for WebSocket price feed wrapper."""

import sys
import os
import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.ws_price_feed import WebSocketPriceFeed


class FakeWebSocket:
    """Fake PolymarketWebSocket for testing."""

    def __init__(self, **kwargs):
        self.url = kwargs.get("url", "wss://fake")
        self.ws = None
        self._running = False
        self._subscriptions = set()
        self._callbacks = {"price": [], "book": [], "trade": []}

    def on_price(self, callback):
        self._callbacks["price"].append(callback)

    def on_book(self, callback):
        self._callbacks["book"].append(callback)

    def on_trade(self, callback):
        self._callbacks["trade"].append(callback)

    async def subscribe(self, token_ids):
        self._subscriptions.update(token_ids)

    async def unsubscribe(self, token_ids):
        self._subscriptions -= set(token_ids)

    async def run(self):
        self._running = True
        # Simulate sending some price updates
        if self._callbacks["price"]:
            cb = self._callbacks["price"][0]
            cb("token-1", 0.65, None)
            cb("token-2", 0.35, None)
        # Wait briefly then stop
        for _ in range(50):
            if not self._running:
                break
            await asyncio.sleep(0.01)

    async def stop(self):
        self._running = False

    @property
    def connected(self):
        return self._running

    @property
    def subscription_count(self):
        return len(self._subscriptions)


def test_initial_state():
    feed = WebSocketPriceFeed()
    assert feed.connected is False
    assert feed.subscription_count == 0


def test_drain_empty():
    feed = WebSocketPriceFeed()
    updates = feed.drain_updates()
    assert updates == {}


def test_price_callback_writes_to_buffer():
    feed = WebSocketPriceFeed()
    # Directly call the internal callback
    feed._on_price_update("tok-1", 0.75, None)
    feed._on_price_update("tok-2", 0.25, None)

    updates = feed.drain_updates()
    assert updates == {"tok-1": 0.75, "tok-2": 0.25}

    # Drain again — should be empty
    updates = feed.drain_updates()
    assert updates == {}


def test_latest_price_wins():
    feed = WebSocketPriceFeed()
    feed._on_price_update("tok-1", 0.50, None)
    feed._on_price_update("tok-1", 0.60, None)
    feed._on_price_update("tok-1", 0.70, None)

    updates = feed.drain_updates()
    assert updates == {"tok-1": 0.70}


def test_thread_safety():
    """Multiple threads writing prices concurrently."""
    feed = WebSocketPriceFeed()
    errors = []

    def writer(prefix, count):
        try:
            for i in range(count):
                feed._on_price_update(f"{prefix}-{i}", 0.5, None)
        except Exception as e:
            errors.append(e)

    threads = [
        threading.Thread(target=writer, args=("a", 100)),
        threading.Thread(target=writer, args=("b", 100)),
        threading.Thread(target=writer, args=("c", 100)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0
    updates = feed.drain_updates()
    assert len(updates) == 300


@patch("data.ws_price_feed.PolymarketWebSocket", FakeWebSocket)
def test_start_and_stop():
    feed = WebSocketPriceFeed()
    feed.start(["token-1", "token-2"])

    # Give the background thread time to start and push prices
    time.sleep(0.2)

    # Should have received the fake prices
    updates = feed.drain_updates()
    assert "token-1" in updates
    assert updates["token-1"] == 0.65

    feed.stop()


@patch("data.ws_price_feed.PolymarketWebSocket", FakeWebSocket)
def test_subscribe_after_start():
    feed = WebSocketPriceFeed()
    feed.start()

    time.sleep(0.1)
    feed.subscribe(["token-3", "token-4"])

    # The fake WS tracks subscriptions
    assert feed.subscription_count >= 2

    feed.stop()


@patch("data.ws_price_feed.PolymarketWebSocket", FakeWebSocket)
def test_double_start_no_error():
    feed = WebSocketPriceFeed()
    feed.start()
    feed.start()  # Should warn but not crash

    time.sleep(0.1)
    feed.stop()


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
