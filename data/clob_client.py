"""Python CLOB REST client — prices, order books, price history."""

import logging
from datetime import datetime, timezone
from typing import Optional

import requests

from core.constants import CLOB_API_URL
from core.types import OrderBook, OrderBookLevel

logger = logging.getLogger(__name__)


class ClobReader:
    """Read-only client for the Polymarket CLOB REST API."""

    def __init__(self, base_url: str = CLOB_API_URL, timeout: int = 10):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, params: dict = None) -> Optional[dict]:
        url = f"{self.base_url}{path}"
        try:
            resp = requests.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"CLOB API error ({path}): {e}")
            return None

    def get_midpoint(self, token_id: str) -> Optional[float]:
        """Get midpoint price for a token."""
        data = self._get("/midpoint", {"token_id": token_id})
        if data and "mid" in data:
            return float(data["mid"])
        return None

    def get_price(self, token_id: str, side: str = "buy") -> Optional[float]:
        """Get best price for a side (buy or sell)."""
        data = self._get("/price", {"token_id": token_id, "side": side})
        if data and "price" in data:
            return float(data["price"])
        return None

    def get_order_book(self, token_id: str) -> Optional[OrderBook]:
        """Get full order book for a token."""
        data = self._get("/book", {"token_id": token_id})
        if not data:
            return None

        bids = []
        for level in data.get("bids", []):
            bids.append(OrderBookLevel(
                price=float(level["price"]),
                size=float(level["size"]),
            ))
        # Sort bids descending (best first)
        bids.sort(key=lambda l: l.price, reverse=True)

        asks = []
        for level in data.get("asks", []):
            asks.append(OrderBookLevel(
                price=float(level["price"]),
                size=float(level["size"]),
            ))
        # Sort asks ascending (best first)
        asks.sort(key=lambda l: l.price)

        return OrderBook(
            token_id=token_id,
            bids=bids,
            asks=asks,
        )

    def get_spread(self, token_id: str) -> Optional[float]:
        """Get bid-ask spread for a token."""
        data = self._get("/spread", {"token_id": token_id})
        if data and "spread" in data:
            return float(data["spread"])
        return None

    def get_prices(self, token_ids: list[str]) -> dict[str, float]:
        """Get midpoint prices for multiple tokens.

        Returns:
            Dict mapping token_id -> midpoint price
        """
        result = {}
        for token_id in token_ids:
            mid = self.get_midpoint(token_id)
            if mid is not None:
                result[token_id] = mid
        return result

    def get_last_trade_price(self, token_id: str) -> Optional[float]:
        """Get last trade price for a token."""
        data = self._get("/last-trade-price", {"token_id": token_id})
        if data and "price" in data:
            return float(data["price"])
        return None

    def get_market_info(self, condition_id: str) -> Optional[dict]:
        """Get market info from CLOB by condition ID."""
        data = self._get(f"/markets/{condition_id}")
        return data

    def get_book_summary(self, token_id: str, depth: int = 5) -> dict:
        """Get a summary of the order book (top N levels).

        Returns:
            Dict with best_bid, best_ask, spread, midpoint,
            bid_depth, ask_depth (total size at top N levels)
        """
        book = self.get_order_book(token_id)
        if not book:
            return {}

        top_bids = book.bids[:depth]
        top_asks = book.asks[:depth]

        return {
            "best_bid": book.best_bid,
            "best_ask": book.best_ask,
            "spread": book.spread,
            "midpoint": book.midpoint,
            "bid_depth": sum(l.size for l in top_bids),
            "ask_depth": sum(l.size for l in top_asks),
            "bid_levels": len(top_bids),
            "ask_levels": len(top_asks),
        }

    def check_connectivity(self) -> bool:
        """Check if the CLOB API is reachable."""
        try:
            resp = requests.get(f"{self.base_url}/time", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False
