"""Crypto scalper strategy — momentum-based signals for Polymarket up/down markets.

Trades in the last ~60 seconds of each 15-minute window when BTC spot direction
is clear but Polymarket odds haven't caught up. The edge comes from the speed gap:
Binance spot moves instantly, but Polymarket order book lags 10-30 seconds behind.
"""

import logging
from typing import Optional

from core.constants import (
    CRYPTO_DEFAULT_ENTRY_WINDOW_SECS,
    CRYPTO_DEFAULT_MAX_ENTRY_PRICE,
    CRYPTO_DEFAULT_MIN_MOMENTUM,
    CRYPTO_DEFAULT_MIN_PRICE_GAP,
    CRYPTO_DEFAULT_POSITION_SIZE,
)
from core.types import Outcome, Signal, SignalAction, StrategyType

logger = logging.getLogger(__name__)


def crypto_fee_rate(price: float) -> float:
    """Calculate Polymarket dynamic fee rate for a given price.

    Fee = 0.25 * (p * (1 - p))^2
    Peaks at ~1.56% at p=0.50, drops to ~0.20% at p=0.90, ~0.06% at p=0.95.
    """
    return 0.25 * (price * (1 - price)) ** 2


class CryptoScalper:
    """Signal generator for crypto up/down markets.

    Pure signal logic — no market discovery, no execution.
    Takes spot price + Polymarket odds, returns a Signal or None.
    """

    def __init__(
        self,
        min_momentum: float = CRYPTO_DEFAULT_MIN_MOMENTUM,
        min_price_gap: float = CRYPTO_DEFAULT_MIN_PRICE_GAP,
        max_entry_price: float = CRYPTO_DEFAULT_MAX_ENTRY_PRICE,
        position_size: float = CRYPTO_DEFAULT_POSITION_SIZE,
        entry_window_secs: int = CRYPTO_DEFAULT_ENTRY_WINDOW_SECS,
    ):
        self.min_momentum = min_momentum
        self.min_price_gap = min_price_gap
        self.max_entry_price = max_entry_price
        self.position_size = position_size
        self.entry_window_secs = entry_window_secs

    def evaluate(
        self,
        spot_momentum: Optional[float],
        window_seconds_remaining: int,
        up_price: float,
        down_price: float,
        up_token_id: str,
        down_token_id: str,
        market_id: str,
    ) -> Optional[Signal]:
        """Evaluate whether to enter a trade in the current window.

        Args:
            spot_momentum: BTC % change over last 30s (e.g. 0.001 = 0.1%)
            window_seconds_remaining: Seconds until window closes
            up_price: Current Polymarket price of "Up" token
            down_price: Current Polymarket price of "Down" token
            up_token_id: Token ID for "Up" outcome
            down_token_id: Token ID for "Down" outcome
            market_id: Polymarket condition_id for this window

        Returns:
            Signal to BUY the winning side's token, or None if no trade.
        """
        # 1. Only trade in entry window (last N seconds of the 15-min window)
        if window_seconds_remaining > self.entry_window_secs:
            logger.debug(
                f"Outside entry window: {window_seconds_remaining}s remaining "
                f"(need <= {self.entry_window_secs}s)"
            )
            return None

        # 2. Need momentum data
        if spot_momentum is None:
            logger.debug("No momentum data available")
            return None

        # 3. Determine direction from spot momentum
        if spot_momentum > self.min_momentum:
            direction = "UP"
            target_price = up_price
            target_token_id = up_token_id
            target_outcome = Outcome.YES  # Up maps to YES semantically
            implied_prob = 0.5 + abs(spot_momentum) * 1000  # Scale momentum to probability
        elif spot_momentum < -self.min_momentum:
            direction = "DOWN"
            target_price = down_price
            target_token_id = down_token_id
            target_outcome = Outcome.NO  # Down maps to NO semantically
            implied_prob = 0.5 + abs(spot_momentum) * 1000
        else:
            logger.debug(
                f"Momentum too weak: {spot_momentum:.6f} "
                f"(need > {self.min_momentum:.6f})"
            )
            return None

        # Cap implied probability at 0.99
        implied_prob = min(implied_prob, 0.99)

        # 4. Check entry price ceiling
        if target_price > self.max_entry_price:
            logger.debug(
                f"Price too high: {direction} @ ${target_price:.4f} "
                f"(max ${self.max_entry_price:.4f})"
            )
            return None

        # 5. Check price gap — spot momentum implies direction but Polymarket hasn't caught up
        price_gap = implied_prob - target_price
        if price_gap < self.min_price_gap:
            logger.debug(
                f"Price gap too small: {price_gap:.4f} "
                f"(implied={implied_prob:.4f}, market={target_price:.4f}, "
                f"need >= {self.min_price_gap:.4f})"
            )
            return None

        # Calculate fee impact
        fee_rate = crypto_fee_rate(target_price)
        edge_after_fee = price_gap - fee_rate

        logger.info(
            f"SIGNAL: {direction} | momentum={spot_momentum:.6f} | "
            f"implied={implied_prob:.4f} | market={target_price:.4f} | "
            f"gap={price_gap:.4f} | fee={fee_rate:.4f} | "
            f"net_edge={edge_after_fee:.4f} | "
            f"window={window_seconds_remaining}s remaining"
        )

        return Signal(
            market_id=market_id,
            action=SignalAction.BUY,
            outcome=target_outcome,
            strategy=StrategyType.CRYPTO_SCALPER,
            price=target_price,
            size=self.position_size,
            edge=price_gap,
            confidence=min(abs(spot_momentum) / self.min_momentum, 1.0),
            reasoning=(
                f"BTC {direction} momentum={spot_momentum:.4%}, "
                f"gap={price_gap:.1%}, fee={fee_rate:.2%}"
            ),
            metadata={
                "direction": direction,
                "spot_momentum": spot_momentum,
                "implied_prob": implied_prob,
                "target_price": target_price,
                "fee_rate": fee_rate,
                "edge_after_fee": edge_after_fee,
                "token_id": target_token_id,
                "window_secs_remaining": window_seconds_remaining,
            },
        )
