"""Crypto scalper strategy V3 — contrarian mean-reversion for Polymarket up/down markets.

After N consecutive same-direction window resolutions, bet on reversal.
Enter early (T-300s) when token prices are still near $0.50 for balanced risk:reward.

V3 changes (from V2):
- Replaced momentum-based signals with contrarian streak detection
- After 2+ consecutive same-direction resolutions, bet the opposite direction
- Enter at T-300s instead of T-60s (better prices)
- Position sizing scales with streak length (longer streak = higher confidence)
- Spot feed still used for monitoring, not for signal generation

Backtest (7 days, 673 windows):
- After 2 streak: 55% WR, +$8,599 P/L, $413 max DD
- After 3 streak: 57% WR, +$4,570 P/L, $249 max DD
- After 4 streak: 68% WR, +$1,597 P/L, $117 max DD
"""

import logging
from typing import Optional

from core.constants import (
    CRYPTO_DEFAULT_ENTRY_WINDOW_SECS,
    CRYPTO_DEFAULT_MAX_ENTRY_PRICE,
    CRYPTO_DEFAULT_MIN_ENTRY_PRICE,
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
    """Signal generator for crypto up/down markets (V3 — contrarian).

    Pure signal logic — no market discovery, no execution.
    Takes streak history + Polymarket odds, returns a Signal or None.
    """

    def __init__(
        self,
        min_streak: int = 2,
        min_entry_price: float = CRYPTO_DEFAULT_MIN_ENTRY_PRICE,
        max_entry_price: float = CRYPTO_DEFAULT_MAX_ENTRY_PRICE,
        base_position_size: float = CRYPTO_DEFAULT_POSITION_SIZE,
        entry_window_secs: int = CRYPTO_DEFAULT_ENTRY_WINDOW_SECS,
    ):
        self.min_streak = min_streak
        self.min_entry_price = min_entry_price
        self.max_entry_price = max_entry_price
        self.base_position_size = base_position_size
        self.entry_window_secs = entry_window_secs

    def evaluate(
        self,
        streak_direction: Optional[str],
        streak_length: int,
        window_seconds_remaining: int,
        up_price: float,
        down_price: float,
        up_token_id: str,
        down_token_id: str,
        market_id: str,
    ) -> Optional[Signal]:
        """Evaluate whether to enter a contrarian trade.

        Args:
            streak_direction: Direction of the current streak ("UP" or "DOWN"), or None
            streak_length: Number of consecutive same-direction resolutions
            window_seconds_remaining: Seconds until window closes
            up_price: Current Polymarket price of "Up" token
            down_price: Current Polymarket price of "Down" token
            up_token_id: Token ID for "Up" outcome
            down_token_id: Token ID for "Down" outcome
            market_id: Polymarket condition_id for this window

        Returns:
            Signal to BUY the opposing side's token, or None if no trade.
        """
        # 1. Only trade in entry window
        if window_seconds_remaining > self.entry_window_secs:
            logger.debug(
                f"Outside entry window: {window_seconds_remaining}s remaining "
                f"(need <= {self.entry_window_secs}s)"
            )
            return None

        # 2. Need streak data
        if streak_direction is None or streak_length < self.min_streak:
            logger.debug(
                f"Streak too short: {streak_length} "
                f"(need >= {self.min_streak})"
            )
            return None

        # 3. Bet AGAINST the streak (contrarian)
        if streak_direction == "UP":
            direction = "DOWN"
            target_price = down_price
            target_token_id = down_token_id
            target_outcome = Outcome.NO
        else:
            direction = "UP"
            target_price = up_price
            target_token_id = up_token_id
            target_outcome = Outcome.YES

        # 4. Price range check
        if target_price < self.min_entry_price:
            logger.debug(
                f"Price too low: {direction} @ ${target_price:.4f} "
                f"(min ${self.min_entry_price:.4f})"
            )
            return None

        if target_price > self.max_entry_price:
            logger.debug(
                f"Price too high: {direction} @ ${target_price:.4f} "
                f"(max ${self.max_entry_price:.4f})"
            )
            return None

        # 5. Fee-adjusted edge calculation
        fee = crypto_fee_rate(target_price)
        profit_if_win = 1.0 - target_price - fee
        loss_if_lose = target_price + fee
        edge = profit_if_win - loss_if_lose

        if edge < 0:
            logger.debug(
                f"Negative edge: profit_if_win={profit_if_win:.4f} "
                f"loss_if_lose={loss_if_lose:.4f}"
            )
            return None

        # 6. Scale position size by streak length
        # streak=2: 1x, streak=3: 1.5x, streak=4+: 2x
        size_multiplier = min(1.0 + (streak_length - self.min_streak) * 0.5, 2.0)
        position_size = self.base_position_size * size_multiplier

        # Confidence scales with streak length
        confidence = min(0.5 + streak_length * 0.1, 1.0)

        logger.info(
            f"SIGNAL: Contrarian {direction} (after {streak_length}x {streak_direction}) | "
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
            confidence=confidence,
            reasoning=(
                f"Contrarian: {streak_length}x {streak_direction} streak → bet {direction}, "
                f"price=${target_price:.4f}, fee={fee:.2%}, edge={edge:.1%}"
            ),
            metadata={
                "direction": direction,
                "streak_direction": streak_direction,
                "streak_length": streak_length,
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
