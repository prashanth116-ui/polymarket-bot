"""WebSocket client for real-time Polymarket price/book updates."""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

import websockets

from core.constants import WS_URL

logger = logging.getLogger(__name__)


class PolymarketWebSocket:
    """Async WebSocket client for real-time market data.

    Subscribes to price and order book updates for watched markets.
    """

    def __init__(self, url: str = WS_URL):
        self.url = url
        self.ws = None
        self._running = False
        self._subscriptions: set[str] = set()  # token_ids
        self._callbacks: dict[str, list[Callable]] = {
            "price": [],
            "book": [],
            "trade": [],
        }
        self._reconnect_delay = 5
        self._max_reconnect_delay = 60

    def on_price(self, callback: Callable):
        """Register callback for price updates. callback(token_id, price, timestamp)."""
        self._callbacks["price"].append(callback)

    def on_book(self, callback: Callable):
        """Register callback for book updates. callback(token_id, bids, asks, timestamp)."""
        self._callbacks["book"].append(callback)

    def on_trade(self, callback: Callable):
        """Register callback for trade updates. callback(token_id, price, size, side, timestamp)."""
        self._callbacks["trade"].append(callback)

    async def subscribe(self, token_ids: list[str]):
        """Subscribe to updates for given token IDs."""
        self._subscriptions.update(token_ids)
        if self.ws:
            for token_id in token_ids:
                await self._send_subscribe(token_id)

    async def unsubscribe(self, token_ids: list[str]):
        """Unsubscribe from updates for given token IDs."""
        self._subscriptions -= set(token_ids)
        if self.ws:
            for token_id in token_ids:
                await self._send_unsubscribe(token_id)

    async def _send_subscribe(self, token_id: str):
        if self.ws:
            msg = json.dumps({
                "type": "subscribe",
                "channel": "market",
                "assets_id": token_id,
            })
            await self.ws.send(msg)
            logger.debug(f"WS subscribed to {token_id[:20]}...")

    async def _send_unsubscribe(self, token_id: str):
        if self.ws:
            msg = json.dumps({
                "type": "unsubscribe",
                "channel": "market",
                "assets_id": token_id,
            })
            await self.ws.send(msg)

    async def _handle_message(self, raw: str):
        """Parse and dispatch incoming WS message."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"WS: invalid JSON: {raw[:100]}")
            return

        msg_type = data.get("type", "")
        timestamp = datetime.now(timezone.utc)

        if msg_type in ("book", "price_change"):
            token_id = data.get("asset_id", "")

            # Price update
            if "price" in data:
                price = float(data["price"])
                for cb in self._callbacks["price"]:
                    try:
                        cb(token_id, price, timestamp)
                    except Exception as e:
                        logger.error(f"WS price callback error: {e}")

            # Book update
            if "bids" in data or "asks" in data:
                bids = data.get("bids", [])
                asks = data.get("asks", [])
                for cb in self._callbacks["book"]:
                    try:
                        cb(token_id, bids, asks, timestamp)
                    except Exception as e:
                        logger.error(f"WS book callback error: {e}")

        elif msg_type == "last_trade_price":
            token_id = data.get("asset_id", "")
            price = float(data.get("price", 0))
            size = float(data.get("size", 0))
            side = data.get("side", "")
            for cb in self._callbacks["trade"]:
                try:
                    cb(token_id, price, size, side, timestamp)
                except Exception as e:
                    logger.error(f"WS trade callback error: {e}")

    async def run(self):
        """Main WebSocket loop with auto-reconnect."""
        self._running = True
        delay = self._reconnect_delay

        while self._running:
            try:
                logger.info(f"WS connecting to {self.url}")
                async with websockets.connect(self.url) as ws:
                    self.ws = ws
                    delay = self._reconnect_delay
                    logger.info("WS connected")

                    # Re-subscribe to all watched tokens
                    for token_id in self._subscriptions:
                        await self._send_subscribe(token_id)

                    async for message in ws:
                        if not self._running:
                            break
                        await self._handle_message(message)

            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"WS connection closed: {e}")
            except Exception as e:
                logger.error(f"WS error: {e}")
            finally:
                self.ws = None

            if self._running:
                logger.info(f"WS reconnecting in {delay}s...")
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_reconnect_delay)

    async def stop(self):
        """Stop the WebSocket client."""
        self._running = False
        if self.ws:
            await self.ws.close()
            self.ws = None
        logger.info("WS stopped")

    @property
    def connected(self) -> bool:
        if self.ws is None:
            return False
        try:
            # websockets v13+: use .state
            from websockets.protocol import State
            return self.ws.state is State.OPEN
        except (ImportError, AttributeError):
            # websockets <v13: use .open
            return getattr(self.ws, "open", False)

    @property
    def subscription_count(self) -> int:
        return len(self._subscriptions)
