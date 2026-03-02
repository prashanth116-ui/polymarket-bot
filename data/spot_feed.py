"""Binance WebSocket feed for real-time BTC/USDT spot price.

Thread-safe background feed following the same pattern as ws_price_feed.py.
Connects to Binance public trade stream, maintains a rolling price buffer
for momentum calculation.
"""

import asyncio
import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from core.constants import BINANCE_WS_URL

logger = logging.getLogger(__name__)

# Rolling buffer holds 120 seconds of price data
BUFFER_SECONDS = 120


class BinanceSpotFeed:
    """Real-time BTC/USDT price feed from Binance WebSocket.

    Usage:
        feed = BinanceSpotFeed("btcusdt")
        feed.start()

        price, ts = feed.get_price()
        momentum = feed.get_momentum(window_secs=30)

        feed.stop()
    """

    def __init__(self, symbol: str = "btcusdt"):
        self.symbol = symbol.lower()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = threading.Lock()
        self._latest_price: Optional[float] = None
        self._latest_ts: Optional[datetime] = None
        self._price_buffer: deque = deque()  # (timestamp_float, price)
        self._started = False
        self._stop_event = threading.Event()

    def start(self):
        """Launch WebSocket in a background daemon thread."""
        if self._started:
            logger.warning("BinanceSpotFeed already started")
            return

        self._started = True
        self._stop_event.clear()

        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name=f"binance-spot-{self.symbol}",
        )
        self._thread.start()
        logger.info(f"BinanceSpotFeed started for {self.symbol.upper()}")

    def _run_loop(self):
        """Background thread entry: create event loop and run WebSocket."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        try:
            self._loop.run_until_complete(self._async_run())
        except Exception as e:
            if self._started:
                logger.error(f"BinanceSpotFeed thread error: {e}")
        finally:
            self._loop.close()
            self._loop = None

    async def _async_run(self):
        """Connect to Binance trade stream with auto-reconnect."""
        try:
            import websockets
        except ImportError:
            logger.error("websockets package required: pip install websockets")
            return

        url = f"{BINANCE_WS_URL}/{self.symbol}@trade"

        while self._started and not self._stop_event.is_set():
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                    logger.info(f"Connected to Binance {self.symbol}@trade stream")
                    while self._started and not self._stop_event.is_set():
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                            data = json.loads(msg)
                            price = float(data["p"])
                            trade_time_ms = data["T"]
                            ts = datetime.fromtimestamp(trade_time_ms / 1000, tz=timezone.utc)
                            self._on_trade(price, ts)
                        except asyncio.TimeoutError:
                            continue
                        except Exception as e:
                            if self._started:
                                logger.warning(f"Binance message error: {e}")
                            break
            except Exception as e:
                if self._started and not self._stop_event.is_set():
                    logger.warning(f"Binance WS disconnected: {e}, reconnecting in 5s...")
                    await asyncio.sleep(5)

    def _on_trade(self, price: float, ts: datetime):
        """Process incoming trade — update latest price and rolling buffer."""
        now = time.time()
        with self._lock:
            self._latest_price = price
            self._latest_ts = ts
            self._price_buffer.append((now, price))
            # Trim buffer to BUFFER_SECONDS
            cutoff = now - BUFFER_SECONDS
            while self._price_buffer and self._price_buffer[0][0] < cutoff:
                self._price_buffer.popleft()

    def get_price(self) -> Optional[tuple[float, datetime]]:
        """Return (price, timestamp) of the latest trade, or None if no data."""
        with self._lock:
            if self._latest_price is not None and self._latest_ts is not None:
                return (self._latest_price, self._latest_ts)
        return None

    def get_momentum(self, window_secs: int = 30) -> Optional[float]:
        """Calculate price momentum (% change) over the last window_secs.

        Returns:
            Fractional change (e.g. 0.001 = 0.1% up), or None if insufficient data.
        """
        now = time.time()
        cutoff = now - window_secs

        with self._lock:
            if not self._price_buffer:
                return None

            # Current price is the latest in buffer
            current_price = self._price_buffer[-1][1]

            # Find the oldest price within the window
            oldest_price = None
            for ts, price in self._price_buffer:
                if ts >= cutoff:
                    oldest_price = price
                    break

            if oldest_price is None or oldest_price == 0:
                return None

        return (current_price - oldest_price) / oldest_price

    def get_price_history(self, seconds: int = 60) -> list[tuple[float, float]]:
        """Return buffered prices within the last N seconds.

        Returns:
            List of (timestamp, price) tuples.
        """
        now = time.time()
        cutoff = now - seconds
        with self._lock:
            return [(ts, p) for ts, p in self._price_buffer if ts >= cutoff]

    def stop(self):
        """Signal shutdown and wait for background thread to exit."""
        if not self._started:
            return

        self._started = False
        self._stop_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)

        logger.info("BinanceSpotFeed stopped")

    @property
    def connected(self) -> bool:
        """Whether we have received at least one price update."""
        return self._latest_price is not None

    @property
    def buffer_size(self) -> int:
        """Number of entries in the rolling price buffer."""
        with self._lock:
            return len(self._price_buffer)
