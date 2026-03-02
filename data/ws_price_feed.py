"""Thread-safe WebSocket price feed wrapper.

Runs PolymarketWebSocket in a background daemon thread with its own
asyncio event loop. Price updates flow into a thread-safe dict that
the main loop drains each cycle. REST polling remains as fallback.
"""

import asyncio
import logging
import threading
import time
from typing import Optional

from data.websocket_client import PolymarketWebSocket

logger = logging.getLogger(__name__)


class WebSocketPriceFeed:
    """Wraps PolymarketWebSocket in a background thread for the sync main loop.

    Usage:
        feed = WebSocketPriceFeed()
        feed.start(["token_id_1", "token_id_2"])

        # In main loop:
        updates = feed.drain_updates()  # {token_id: latest_price}

        feed.subscribe(["token_id_3"])
        feed.stop()
    """

    def __init__(self, ws_url: str = None):
        self._ws: Optional[PolymarketWebSocket] = None
        self._ws_url = ws_url
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = threading.Lock()
        self._prices: dict[str, float] = {}  # token_id -> latest price
        self._connected = False
        self._started = False

    def start(self, token_ids: list[str] = None):
        """Launch the WebSocket in a background daemon thread."""
        if self._started:
            logger.warning("WebSocketPriceFeed already started")
            return

        self._started = True
        initial_tokens = token_ids or []

        self._thread = threading.Thread(
            target=self._run_loop,
            args=(initial_tokens,),
            daemon=True,
            name="ws-price-feed",
        )
        self._thread.start()
        logger.info(f"WebSocket price feed started (initial tokens: {len(initial_tokens)})")

    def _run_loop(self, initial_tokens: list[str]):
        """Background thread entry: create event loop and run WebSocket."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        try:
            self._loop.run_until_complete(self._async_run(initial_tokens))
        except Exception as e:
            logger.error(f"WebSocket feed thread error: {e}")
        finally:
            self._loop.close()
            self._loop = None

    async def _async_run(self, initial_tokens: list[str]):
        """Async entry: set up WebSocket, subscribe, and run."""
        kwargs = {}
        if self._ws_url:
            kwargs["url"] = self._ws_url
        self._ws = PolymarketWebSocket(**kwargs)

        # Register price callback
        self._ws.on_price(self._on_price_update)

        # Pre-subscribe before connecting
        if initial_tokens:
            await self._ws.subscribe(initial_tokens)

        # This blocks until stop() is called
        await self._ws.run()

    def _on_price_update(self, token_id: str, price: float, timestamp):
        """Called from WebSocket message handler — writes to thread-safe dict."""
        with self._lock:
            self._prices[token_id] = price
        self._connected = True

    def drain_updates(self) -> dict[str, float]:
        """Return all buffered price updates and clear the buffer.

        Thread-safe. Called from the main loop each cycle.
        Returns {token_id: latest_price} for all tokens that got an update.
        """
        with self._lock:
            updates = self._prices.copy()
            self._prices.clear()
        return updates

    def subscribe(self, token_ids: list[str]):
        """Subscribe to new token IDs (thread-safe)."""
        if not self._loop or not self._ws:
            return
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._ws.subscribe(token_ids), self._loop
            )
            future.result(timeout=5)
        except Exception as e:
            logger.error(f"WS subscribe error: {e}")

    def unsubscribe(self, token_ids: list[str]):
        """Unsubscribe from token IDs (thread-safe)."""
        if not self._loop or not self._ws:
            return
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._ws.unsubscribe(token_ids), self._loop
            )
            future.result(timeout=5)
        except Exception as e:
            logger.error(f"WS unsubscribe error: {e}")

    def stop(self):
        """Signal shutdown and wait for the background thread to exit."""
        if not self._started:
            return

        self._started = False

        if self._loop and self._ws:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._ws.stop(), self._loop
                )
                future.result(timeout=5)
            except Exception as e:
                logger.warning(f"WS stop error: {e}")

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

        self._connected = False
        logger.info("WebSocket price feed stopped")

    @property
    def connected(self) -> bool:
        """Whether the WebSocket is currently connected."""
        if self._ws:
            return self._ws.connected
        return False

    @property
    def subscription_count(self) -> int:
        """Number of active subscriptions."""
        if self._ws:
            return self._ws.subscription_count
        return 0
