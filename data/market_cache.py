"""Thread-safe in-memory market state cache."""

import logging
import threading
from datetime import datetime, timezone
from typing import Optional

from core.types import Market, OrderBook, OrderBookLevel

logger = logging.getLogger(__name__)


class MarketCache:
    """Thread-safe in-memory cache for market data.

    Updated by WebSocket callbacks and REST polling.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._markets: dict[str, Market] = {}  # condition_id -> Market
        self._books: dict[str, OrderBook] = {}  # token_id -> OrderBook
        self._prices: dict[str, float] = {}  # token_id -> last price
        self._token_to_market: dict[str, str] = {}  # token_id -> condition_id
        self._last_update: dict[str, datetime] = {}  # token_id -> timestamp

    def add_market(self, market: Market):
        """Add or update a market in the cache."""
        with self._lock:
            self._markets[market.condition_id] = market
            for outcome, token_id in market.tokens.items():
                self._token_to_market[token_id] = market.condition_id

    def add_markets(self, markets: list[Market]):
        """Bulk add/update markets."""
        with self._lock:
            for market in markets:
                self._markets[market.condition_id] = market
                for outcome, token_id in market.tokens.items():
                    self._token_to_market[token_id] = market.condition_id

    def get_market(self, condition_id: str) -> Optional[Market]:
        """Get a market by condition ID."""
        with self._lock:
            return self._markets.get(condition_id)

    def get_all_markets(self) -> list[Market]:
        """Get all cached markets."""
        with self._lock:
            return list(self._markets.values())

    def get_active_markets(self) -> list[Market]:
        """Get all active markets."""
        with self._lock:
            return [m for m in self._markets.values() if m.active]

    def update_price(self, token_id: str, price: float, timestamp: datetime = None):
        """Update price for a token (called by WS or REST poller)."""
        ts = timestamp or datetime.now(timezone.utc)
        with self._lock:
            self._prices[token_id] = price
            self._last_update[token_id] = ts

            # Update the parent market's price fields
            cid = self._token_to_market.get(token_id)
            if cid and cid in self._markets:
                market = self._markets[cid]
                if market.yes_token_id == token_id:
                    market.last_price_yes = price
                    market.last_price_no = 1.0 - price
                elif market.no_token_id == token_id:
                    market.last_price_no = price
                    market.last_price_yes = 1.0 - price
                market.updated_at = ts

    def update_book(self, token_id: str, bids: list, asks: list, timestamp: datetime = None):
        """Update order book for a token (called by WS or REST)."""
        ts = timestamp or datetime.now(timezone.utc)
        with self._lock:
            parsed_bids = []
            parsed_asks = []

            for b in bids:
                if isinstance(b, dict):
                    parsed_bids.append(OrderBookLevel(
                        price=float(b.get("price", 0)),
                        size=float(b.get("size", 0)),
                    ))
                elif isinstance(b, OrderBookLevel):
                    parsed_bids.append(b)

            for a in asks:
                if isinstance(a, dict):
                    parsed_asks.append(OrderBookLevel(
                        price=float(a.get("price", 0)),
                        size=float(a.get("size", 0)),
                    ))
                elif isinstance(a, OrderBookLevel):
                    parsed_asks.append(a)

            parsed_bids.sort(key=lambda l: l.price, reverse=True)
            parsed_asks.sort(key=lambda l: l.price)

            book = OrderBook(
                token_id=token_id,
                bids=parsed_bids,
                asks=parsed_asks,
                timestamp=ts,
            )
            self._books[token_id] = book
            self._last_update[token_id] = ts

            # Update market bid/ask/spread
            cid = self._token_to_market.get(token_id)
            if cid and cid in self._markets:
                market = self._markets[cid]
                if market.yes_token_id == token_id:
                    market.best_bid_yes = book.best_bid
                    market.best_ask_yes = book.best_ask
                    market.spread_yes = book.spread
                elif market.no_token_id == token_id:
                    market.best_bid_no = book.best_bid
                    market.best_ask_no = book.best_ask
                    market.spread_no = book.spread

    def get_price(self, token_id: str) -> Optional[float]:
        """Get latest cached price for a token."""
        with self._lock:
            return self._prices.get(token_id)

    def get_book(self, token_id: str) -> Optional[OrderBook]:
        """Get latest cached order book for a token."""
        with self._lock:
            return self._books.get(token_id)

    def get_market_for_token(self, token_id: str) -> Optional[Market]:
        """Look up market by token ID."""
        with self._lock:
            cid = self._token_to_market.get(token_id)
            if cid:
                return self._markets.get(cid)
            return None

    def remove_market(self, condition_id: str):
        """Remove a market and its tokens from cache."""
        with self._lock:
            market = self._markets.pop(condition_id, None)
            if market:
                for token_id in market.tokens.values():
                    self._token_to_market.pop(token_id, None)
                    self._prices.pop(token_id, None)
                    self._books.pop(token_id, None)
                    self._last_update.pop(token_id, None)

    def clear(self):
        """Clear all cached data."""
        with self._lock:
            self._markets.clear()
            self._books.clear()
            self._prices.clear()
            self._token_to_market.clear()
            self._last_update.clear()

    @property
    def market_count(self) -> int:
        with self._lock:
            return len(self._markets)

    @property
    def token_count(self) -> int:
        with self._lock:
            return len(self._token_to_market)

    def summary(self) -> dict:
        with self._lock:
            return {
                "markets": len(self._markets),
                "tokens": len(self._token_to_market),
                "prices_cached": len(self._prices),
                "books_cached": len(self._books),
            }
