"""Executor that communicates with the TypeScript bridge via HTTP."""

import logging
import time
from typing import Optional

import requests

from core.constants import DEFAULT_BRIDGE_URL, TAKER_FEE_BPS
from core.types import (
    OpenOrder,
    OrderBook,
    OrderBookLevel,
    OrderStatus,
    Outcome,
    Position,
    Side,
    StrategyType,
    TradeResult,
)
from execution.executor_interface import ExecutorInterface

logger = logging.getLogger(__name__)


class BridgeExecutor(ExecutorInterface):
    """Sends orders to the TypeScript bridge at localhost:8420."""

    def __init__(
        self,
        bridge_url: str = DEFAULT_BRIDGE_URL,
        timeout: int = 10,
        max_retries: int = 2,
        retry_delay: float = 1.0,
    ):
        self.bridge_url = bridge_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._health_ok: bool = False
        self._last_health_check: float = 0
        self._health_check_interval: float = 60.0

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.bridge_url}{path}"
        kwargs.setdefault("timeout", self.timeout)
        last_exc = None

        for attempt in range(self.max_retries + 1):
            try:
                resp = getattr(requests, method)(url, **kwargs)
                resp.raise_for_status()
                return resp.json()
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                last_exc = e
                if attempt < self.max_retries:
                    delay = self.retry_delay * (2 ** attempt)
                    logger.warning(
                        f"Bridge request {method.upper()} {path} failed (attempt {attempt + 1}/"
                        f"{self.max_retries + 1}): {e} — retrying in {delay:.1f}s"
                    )
                    time.sleep(delay)
                else:
                    logger.error(f"Bridge not reachable at {url} after {self.max_retries + 1} attempts")
            except requests.exceptions.HTTPError as e:
                status = resp.status_code
                if 500 <= status < 600 and attempt < self.max_retries:
                    last_exc = e
                    delay = self.retry_delay * (2 ** attempt)
                    logger.warning(
                        f"Bridge {status} on {method.upper()} {path} (attempt {attempt + 1}/"
                        f"{self.max_retries + 1}) — retrying in {delay:.1f}s"
                    )
                    time.sleep(delay)
                else:
                    # 4xx errors: don't retry — client error
                    logger.error(f"Bridge error {status}: {resp.text}")
                    raise

        raise last_exc

    @property
    def health_ok(self) -> bool:
        """Cached health status, refreshed every 60s."""
        now = time.time()
        if now - self._last_health_check >= self._health_check_interval:
            self._refresh_health()
        return self._health_ok

    def _refresh_health(self):
        """Check bridge health and cache result."""
        try:
            self.health()
            self._health_ok = True
        except Exception:
            self._health_ok = False
        self._last_health_check = time.time()

    def health(self) -> dict:
        return self._request("get", "/health")

    def buy(
        self,
        market_id: str,
        token_id: str,
        outcome: Outcome,
        price: float,
        size: float,
    ) -> TradeResult:
        data = self._request("post", "/order", json={
            "token_id": token_id,
            "price": price,
            "size": size,
            "side": "BUY",
            "type": "GTC",
        })

        fee = size * price * (TAKER_FEE_BPS / 10000)
        return TradeResult(
            market_id=market_id,
            outcome=outcome,
            side=Side.BUY,
            price=price,
            size=size,
            cost=size * price,
            fee=fee,
            order_id=data.get("order_id"),
            paper=False,
        )

    def sell(
        self,
        market_id: str,
        token_id: str,
        outcome: Outcome,
        price: float,
        size: float,
    ) -> TradeResult:
        data = self._request("post", "/order", json={
            "token_id": token_id,
            "price": price,
            "size": size,
            "side": "SELL",
            "type": "GTC",
        })

        fee = size * price * (TAKER_FEE_BPS / 10000)
        return TradeResult(
            market_id=market_id,
            outcome=outcome,
            side=Side.SELL,
            price=price,
            size=size,
            cost=size * price,
            fee=fee,
            order_id=data.get("order_id"),
            paper=False,
        )

    def cancel(self, order_id: str) -> bool:
        try:
            self._request("delete", f"/order/{order_id}")
            return True
        except Exception:
            logger.error(f"Failed to cancel order {order_id}")
            return False

    def get_positions(self) -> list[Position]:
        data = self._request("get", "/positions")
        return []  # Positions tracked in Python layer

    def get_balance(self) -> float:
        data = self._request("get", "/balance")
        return data.get("usdc", 0.0)

    def get_order_book(self, token_id: str) -> Optional[OrderBook]:
        try:
            data = self._request("get", f"/market/book/{token_id}")
            return OrderBook(
                token_id=token_id,
                bids=[OrderBookLevel(price=l["price"], size=l["size"]) for l in data.get("bids", [])],
                asks=[OrderBookLevel(price=l["price"], size=l["size"]) for l in data.get("asks", [])],
            )
        except Exception:
            logger.error(f"Failed to get book for {token_id}")
            return None

    def place_limit_order(
        self,
        market_id: str,
        token_id: str,
        outcome: Outcome,
        side: Side,
        price: float,
        size: float,
        strategy: StrategyType = StrategyType.MARKET_MAKING,
    ) -> Optional[str]:
        """Place a GTC limit order via the bridge."""
        try:
            data = self._request("post", "/order", json={
                "token_id": token_id,
                "price": price,
                "size": size,
                "side": side.value,
                "type": "GTC",
            })
            return data.get("order_id")
        except Exception:
            logger.error(f"Failed to place limit order for {token_id}")
            return None

    def get_open_orders(self, market_id: str = None) -> list[OpenOrder]:
        """Get open orders from the bridge."""
        try:
            data = self._request("get", "/orders")
            orders = []
            for o in data.get("orders", []):
                orders.append(OpenOrder(
                    order_id=o["order_id"],
                    market_id=o.get("market_id", ""),
                    token_id=o["token_id"],
                    outcome=Outcome(o.get("outcome", "YES")),
                    side=Side(o["side"]),
                    price=o["price"],
                    size=o["size"],
                    filled_size=o.get("filled_size", 0.0),
                    status=OrderStatus(o.get("status", "open")),
                ))
            if market_id:
                orders = [o for o in orders if o.market_id == market_id]
            return orders
        except Exception:
            logger.error("Failed to get open orders")
            return []

    def cancel_all_orders(self, market_id: str = None) -> int:
        """Cancel all open orders via the bridge."""
        try:
            params = {}
            if market_id:
                params["market_id"] = market_id
            data = self._request("delete", "/orders", params=params)
            cancelled = data.get("cancelled", 0)
            logger.info(f"Cancelled {cancelled} orders via bridge")
            return cancelled
        except Exception:
            logger.error("Failed to cancel orders")
            return 0
