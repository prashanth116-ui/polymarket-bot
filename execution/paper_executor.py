"""Paper trading executor with simulated fills."""

import logging
import random
from datetime import datetime
from typing import Optional

from core.constants import TAKER_FEE_BPS
from core.types import (
    ExitReason,
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


class PaperExecutor(ExecutorInterface):
    """Simulated executor for paper trading."""

    def __init__(
        self,
        initial_balance: float = 1000.0,
        slippage_bps: int = 50,
        fee_bps: int = TAKER_FEE_BPS,
        storage=None,
    ):
        self.balance = initial_balance
        self.initial_balance = initial_balance
        self.slippage_bps = slippage_bps
        self.fee_bps = fee_bps
        self.positions: dict[str, Position] = {}  # key: market_id:outcome
        self.trade_history: list[TradeResult] = []
        self.total_fees = 0.0
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self._next_order_id = 1
        self.open_orders: dict[str, OpenOrder] = {}  # order_id -> OpenOrder
        self._storage = storage

    def _position_key(self, market_id: str, outcome: Outcome) -> str:
        return f"{market_id}:{outcome.value}"

    def _apply_slippage(self, price: float, side: Side, size: float = 0.0) -> float:
        """Apply simulated slippage scaled by order size.

        Larger orders get worse slippage: base bps + 10bps per $100 of order.
        """
        if self.slippage_bps == 0:
            return price
        base_slip = price * (self.slippage_bps / 10000)
        # Size impact: +10bps per $100 order value
        order_value = size * price if size > 0 else 0
        size_slip = price * (order_value / 100) * 0.001  # 10bps per $100
        total_slip = base_slip + size_slip
        if side == Side.BUY:
            return min(0.99, price + total_slip)
        return max(0.01, price - total_slip)

    def _generate_order_id(self) -> str:
        oid = f"paper-{self._next_order_id}"
        self._next_order_id += 1
        return oid

    def buy(
        self,
        market_id: str,
        token_id: str,
        outcome: Outcome,
        price: float,
        size: float,
        strategy: StrategyType = StrategyType.EDGE,
    ) -> TradeResult:
        fill_price = self._apply_slippage(price, Side.BUY, size)
        cost = size * fill_price
        fee = cost * (self.fee_bps / 10000)
        total_cost = cost + fee

        if total_cost > self.balance:
            logger.warning(
                f"Insufficient balance: need ${total_cost:.2f}, have ${self.balance:.2f}"
            )
            # Reduce size to fit
            max_cost = self.balance / (1 + self.fee_bps / 10000)
            size = max_cost / fill_price
            cost = size * fill_price
            fee = cost * (self.fee_bps / 10000)
            total_cost = cost + fee

        self.balance -= total_cost
        self.total_fees += fee

        # Update or create position
        key = self._position_key(market_id, outcome)
        if key in self.positions:
            pos = self.positions[key]
            total_size = pos.size + size
            pos.entry_price = (pos.entry_price * pos.size + fill_price * size) / total_size
            pos.size = total_size
            pos.cost_basis += total_cost
        else:
            self.positions[key] = Position(
                market_id=market_id,
                condition_id=market_id,
                outcome=outcome,
                token_id=token_id,
                side=Side.BUY,
                entry_price=fill_price,
                size=size,
                cost_basis=total_cost,
                current_price=fill_price,
                strategy=strategy,
            )

        # Persist to DB
        pos = self.positions[key]
        if self._storage:
            self._storage.save_position(
                market_id=market_id, outcome=outcome.value, token_id=token_id,
                entry_price=pos.entry_price, size=pos.size, cost_basis=pos.cost_basis,
                strategy=strategy.value if hasattr(strategy, 'value') else str(strategy),
            )

        result = TradeResult(
            market_id=market_id,
            outcome=outcome,
            side=Side.BUY,
            price=fill_price,
            size=size,
            cost=cost,
            fee=fee,
            order_id=self._generate_order_id(),
            strategy=strategy,
            paper=True,
        )
        self.trade_history.append(result)
        self.daily_trades += 1

        logger.info(
            f"PAPER BUY {outcome.value} @ ${fill_price:.4f} x {size:.2f} "
            f"= ${total_cost:.2f} (fee ${fee:.2f}) | Balance: ${self.balance:.2f}"
        )
        return result

    def sell(
        self,
        market_id: str,
        token_id: str,
        outcome: Outcome,
        price: float,
        size: float,
        exit_reason: Optional[ExitReason] = None,
    ) -> TradeResult:
        fill_price = self._apply_slippage(price, Side.SELL, size)
        revenue = size * fill_price
        fee = revenue * (self.fee_bps / 10000)
        net_revenue = revenue - fee

        key = self._position_key(market_id, outcome)
        pos = self.positions.get(key)

        realized_pnl = 0.0
        if pos:
            realized_pnl = (fill_price - pos.entry_price) * size
            pos.size -= size
            if pos.size <= 0.01:  # Effectively closed
                del self.positions[key]
                if self._storage:
                    self._storage.remove_position(market_id, outcome.value)
            else:
                pos.realized_pnl += realized_pnl
                if self._storage:
                    self._storage.save_position(
                        market_id=market_id, outcome=outcome.value,
                        token_id=pos.token_id, entry_price=pos.entry_price,
                        size=pos.size, cost_basis=pos.cost_basis,
                        strategy=pos.strategy.value if hasattr(pos.strategy, 'value') else str(pos.strategy),
                    )

        self.balance += net_revenue
        self.total_fees += fee
        self.daily_pnl += realized_pnl

        result = TradeResult(
            market_id=market_id,
            outcome=outcome,
            side=Side.SELL,
            price=fill_price,
            size=size,
            cost=revenue,
            fee=fee,
            order_id=self._generate_order_id(),
            exit_reason=exit_reason,
            paper=True,
        )
        self.trade_history.append(result)
        self.daily_trades += 1

        logger.info(
            f"PAPER SELL {outcome.value} @ ${fill_price:.4f} x {size:.2f} "
            f"= ${net_revenue:.2f} (P/L: ${realized_pnl:.2f}) | Balance: ${self.balance:.2f}"
        )
        return result

    def resolve_position(self, market_id: str, outcome: Outcome, resolution: str):
        """Handle market resolution — payout 1.0 per share if correct, 0.0 if wrong."""
        key = self._position_key(market_id, outcome)
        pos = self.positions.get(key)
        if not pos:
            return

        if outcome.value == resolution:
            # Won — receive $1 per share
            payout = pos.size
            pnl = payout - pos.cost_basis
        else:
            # Lost — shares worth $0
            payout = 0.0
            pnl = -pos.cost_basis

        self.balance += payout
        self.daily_pnl += pnl

        logger.info(
            f"RESOLUTION {outcome.value} {'WON' if outcome.value == resolution else 'LOST'} "
            f"| Payout: ${payout:.2f} | P/L: ${pnl:.2f} | Balance: ${self.balance:.2f}"
        )

        del self.positions[key]
        if self._storage:
            self._storage.remove_position(market_id, outcome.value)

    def cancel(self, order_id: str) -> bool:
        logger.info(f"PAPER CANCEL order {order_id}")
        return True

    def get_positions(self) -> list[Position]:
        return list(self.positions.values())

    def get_balance(self) -> float:
        return self.balance

    def get_order_book(self, token_id: str) -> Optional[OrderBook]:
        # Paper mode generates a synthetic book
        mid = 0.5
        spread = 0.02
        return OrderBook(
            token_id=token_id,
            bids=[
                OrderBookLevel(price=mid - spread / 2, size=100),
                OrderBookLevel(price=mid - spread, size=200),
            ],
            asks=[
                OrderBookLevel(price=mid + spread / 2, size=100),
                OrderBookLevel(price=mid + spread, size=200),
            ],
        )

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
        """Place a resting limit order (simulated)."""
        order_id = self._generate_order_id()
        order = OpenOrder(
            order_id=order_id,
            market_id=market_id,
            token_id=token_id,
            outcome=outcome,
            side=side,
            price=price,
            size=size,
            strategy=strategy,
        )
        self.open_orders[order_id] = order
        logger.info(
            f"PAPER LIMIT {side.value} {outcome.value} @ ${price:.4f} x {size:.1f} "
            f"(order {order_id})"
        )
        return order_id

    def check_limit_fills(self, token_id: str, current_price: float) -> list[TradeResult]:
        """Check if any resting orders should fill at the current price.

        BUY fills if current_price <= order.price
        SELL fills if current_price >= order.price
        """
        filled = []
        to_remove = []

        for oid, order in self.open_orders.items():
            if order.token_id != token_id or not order.is_active:
                continue

            should_fill = (
                (order.side == Side.BUY and current_price <= order.price) or
                (order.side == Side.SELL and current_price >= order.price)
            )

            if not should_fill:
                continue

            # Execute the fill — maker fee (0 bps)
            fill_size = order.remaining_size
            if order.side == Side.BUY:
                result = self.buy(
                    market_id=order.market_id,
                    token_id=order.token_id,
                    outcome=order.outcome,
                    price=order.price,
                    size=fill_size,
                    strategy=order.strategy,
                )
            else:
                result = self.sell(
                    market_id=order.market_id,
                    token_id=order.token_id,
                    outcome=order.outcome,
                    price=order.price,
                    size=fill_size,
                )

            order.filled_size = order.size
            order.status = OrderStatus.FILLED
            to_remove.append(oid)
            filled.append(result)

            logger.info(f"LIMIT FILL {order.side.value} {order.outcome.value} @ ${order.price:.4f} x {fill_size:.1f}")

        for oid in to_remove:
            del self.open_orders[oid]

        return filled

    def get_open_orders(self, market_id: str = None) -> list[OpenOrder]:
        """Get all active resting orders."""
        orders = [o for o in self.open_orders.values() if o.is_active]
        if market_id:
            orders = [o for o in orders if o.market_id == market_id]
        return orders

    def cancel_all_orders(self, market_id: str = None) -> int:
        """Cancel all resting orders, optionally for a specific market."""
        to_cancel = []
        for oid, order in self.open_orders.items():
            if not order.is_active:
                continue
            if market_id and order.market_id != market_id:
                continue
            to_cancel.append(oid)

        for oid in to_cancel:
            self.open_orders[oid].status = OrderStatus.CANCELLED
            del self.open_orders[oid]

        if to_cancel:
            logger.info(f"Cancelled {len(to_cancel)} orders" + (f" for {market_id}" if market_id else ""))
        return len(to_cancel)

    def restore_positions(self) -> list[Position]:
        """Restore positions from database on startup."""
        if not self._storage:
            return []
        db_positions = self._storage.get_positions()
        restored = []
        for row in db_positions:
            outcome = Outcome(row["outcome"])
            strategy = StrategyType(row["strategy"]) if row.get("strategy") else StrategyType.EDGE
            pos = Position(
                market_id=row["market_id"],
                condition_id=row["market_id"],
                outcome=outcome,
                token_id=row.get("token_id", ""),
                side=Side.BUY,
                entry_price=row["entry_price"],
                size=row["size"],
                cost_basis=row["cost_basis"],
                current_price=row["entry_price"],
                strategy=strategy,
            )
            key = self._position_key(row["market_id"], outcome)
            self.positions[key] = pos
            restored.append(pos)
        return restored

    def reset_daily(self):
        """Reset daily tracking counters."""
        self.daily_pnl = 0.0
        self.daily_trades = 0

    @property
    def total_pnl(self) -> float:
        return self.balance - self.initial_balance

    @property
    def open_exposure(self) -> float:
        return sum(p.cost_basis for p in self.positions.values())

    def summary(self) -> dict:
        return {
            "balance": round(self.balance, 2),
            "initial_balance": self.initial_balance,
            "total_pnl": round(self.total_pnl, 2),
            "daily_pnl": round(self.daily_pnl, 2),
            "daily_trades": self.daily_trades,
            "open_positions": len(self.positions),
            "open_exposure": round(self.open_exposure, 2),
            "total_trades": len(self.trade_history),
            "total_fees": round(self.total_fees, 2),
        }
