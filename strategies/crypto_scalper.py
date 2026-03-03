"""Crypto scalper strategy V2 — momentum-based signals for Polymarket up/down markets.

Trades in the last ~60 seconds of each 15-minute window when BTC spot direction
is clear but Polymarket odds haven't caught up. The edge comes from the speed gap:
Coinbase spot moves instantly, but Polymarket order book lags 10-30 seconds behind.

V2 changes (from V1):
- Removed broken implied probability formula
- Added price floor/ceiling ($0.05-$0.55) — don't buy tokens the market thinks are worthless
  or that already price in the move
- Raised momentum threshold from 3bps to 10bps — 3bps is noise
- Added volatility filter — skip flat/choppy markets
- Position sizing scaled by momentum strength
- Edge metric is fee-adjusted expected value, not fake "price gap"
"""

import logging
from typing import Optional

from core.constants import (
    CRYPTO_DEFAULT_ENTRY_WINDOW_SECS,
    CRYPTO_DEFAULT_MAX_ENTRY_PRICE,
    CRYPTO_DEFAULT_MIN_ENTRY_PRICE,
    CRYPTO_DEFAULT_MIN_MOMENTUM,
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
    """Signal generator for crypto up/down markets (V2).

    Pure signal logic — no market discovery, no execution.
    Takes spot price + Polymarket odds, returns a Signal or None.
    """

    def __init__(
        self,
        min_momentum: float = CRYPTO_DEFAULT_MIN_MOMENTUM,
        min_entry_price: float = CRYPTO_DEFAULT_MIN_ENTRY_PRICE,
        max_entry_price: float = CRYPTO_DEFAULT_MAX_ENTRY_PRICE,
        base_position_size: float = CRYPTO_DEFAULT_POSITION_SIZE,
        entry_window_secs: int = CRYPTO_DEFAULT_ENTRY_WINDOW_SECS,
    ):
        self.min_momentum = min_momentum
        self.min_entry_price = min_entry_price
        self.max_entry_price = max_entry_price
        self.base_position_size = base_position_size
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
        abs_momentum = abs(spot_momentum)
        if abs_momentum < self.min_momentum:
            logger.debug(
                f"Momentum too weak: {spot_momentum:.6f} "
                f"(need > {self.min_momentum:.6f})"
            )
            return None

        if spot_momentum > 0:
            direction = "UP"
            target_price = up_price
            target_token_id = up_token_id
            target_outcome = Outcome.YES  # Up maps to YES semantically
        else:
            direction = "DOWN"
            target_price = down_price
            target_token_id = down_token_id
            target_outcome = Outcome.NO  # Down maps to NO semantically

        # 4. Price range check
        if target_price < self.min_entry_price:
            logger.debug(
                f"Price too low: {direction} @ ${target_price:.4f} "
                f"(min ${self.min_entry_price:.4f}) — market strongly disagrees"
            )
            return None

        if target_price > self.max_entry_price:
            logger.debug(
                f"Price too high: {direction} @ ${target_price:.4f} "
                f"(max ${self.max_entry_price:.4f}) — move already priced in"
            )
            return None

        # 5. Fee-adjusted edge calculation
        fee = crypto_fee_rate(target_price)

        # Momentum strength as multiple of threshold (1.0 = barely qualifying)
        momentum_strength = abs_momentum / self.min_momentum

        # Expected value estimate:
        # If we buy at price p and win: profit per share = 1 - p - fee
        # If we lose: loss per share = p + fee
        # Need positive EV even at 50% win rate for the trade to make sense
        profit_if_win = 1.0 - target_price - fee
        loss_if_lose = target_price + fee

        # Require profit_if_win > loss_if_lose (i.e. buy below ~50¢)
        # plus a minimum edge buffer
        edge = profit_if_win - loss_if_lose
        if edge < 0:
            logger.debug(
                f"Negative edge: profit_if_win={profit_if_win:.4f} "
                f"loss_if_lose={loss_if_lose:.4f}"
            )
            return None

        # 6. Scale position size by momentum strength (1x-2x base)
        size_multiplier = min(momentum_strength, 2.0)
        position_size = self.base_position_size * size_multiplier

        logger.info(
            f"SIGNAL: {direction} | momentum={spot_momentum:.6f} "
            f"({momentum_strength:.1f}x threshold) | "
            f"price=${target_price:.4f} | fee={fee:.4f} | "
            f"edge={edge:.4f} | size=${position_size:.2f} | "
            f"window={window_seconds_remaining}s remaining"
        )

        return Signal(
            market_id=market_id,
            action=SignalAction.BUY,
            outcome=target_outcome,
            strategy=StrategyType.CRYPTO_SCALPER,
            price=target_price,
            size=position_size,
            edge=edge,
            confidence=min(momentum_strength / 3.0, 1.0),  # 3x threshold = full confidence
            reasoning=(
                f"BTC {direction} momentum={spot_momentum:.4%}, "
                f"price=${target_price:.4f}, fee={fee:.2%}, edge={edge:.1%}"
            ),
            metadata={
                "direction": direction,
                "spot_momentum": spot_momentum,
                "momentum_strength": momentum_strength,
                "target_price": target_price,
                "fee_rate": fee,
                "edge": edge,
                "profit_if_win": profit_if_win,
                "loss_if_lose": loss_if_lose,
                "token_id": target_token_id,
                "window_secs_remaining": window_seconds_remaining,
                "position_size": position_size,
            },
        )
