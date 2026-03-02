"""Position sizer — Kelly criterion with practical caps and adjustments."""

import logging

from core.kelly import kelly_fraction, size_position

logger = logging.getLogger(__name__)


class PositionSizer:
    """Converts edge + probability into a dollar-sized position.

    Uses fractional Kelly with additional safety constraints:
    - Max position cap (absolute dollar amount)
    - Max fraction of bankroll per trade
    - Minimum trade size
    - Scale down when approaching exposure limits
    """

    def __init__(
        self,
        bankroll: float = 1000.0,
        kelly_mult: float = 0.25,
        max_position: float = 100.0,
        max_bankroll_pct: float = 0.10,
        min_trade_size: float = 1.0,
    ):
        self.bankroll = bankroll
        self.kelly_mult = kelly_mult
        self.max_position = max_position
        self.max_bankroll_pct = max_bankroll_pct
        self.min_trade_size = min_trade_size

    def size(
        self,
        true_prob: float,
        market_price: float,
        remaining_exposure: float = None,
    ) -> float:
        """Calculate position size in USDC.

        Args:
            true_prob: Model's estimated probability
            market_price: Current market price
            remaining_exposure: How much more exposure we can take (optional cap)

        Returns:
            Dollar size (0.0 if trade shouldn't be taken)
        """
        if self.bankroll <= 0:
            return 0.0

        # Kelly sizing
        raw_size = size_position(
            bankroll=self.bankroll,
            true_prob=true_prob,
            market_price=market_price,
            kelly_mult=self.kelly_mult,
            max_position=self.max_position,
        )

        if raw_size < self.min_trade_size:
            return 0.0

        # Cap at max bankroll percentage
        max_by_pct = self.bankroll * self.max_bankroll_pct
        sized = min(raw_size, max_by_pct)

        # Cap at remaining exposure if provided
        if remaining_exposure is not None and remaining_exposure > 0:
            sized = min(sized, remaining_exposure)

        # Final minimum check
        if sized < self.min_trade_size:
            return 0.0

        return round(sized, 2)

    def update_bankroll(self, bankroll: float):
        """Update bankroll (e.g., after trades or daily reset)."""
        self.bankroll = bankroll
