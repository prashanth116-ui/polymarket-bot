"""Risk manager — circuit breakers, daily limits, kill switch.

Enforces position limits, daily loss caps, consecutive loss stops,
correlated position limits, directional diversity, and a manual kill switch.
All checks must pass before a trade is allowed.
"""

import logging
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

from core.types import Market, Signal, StrategyType

if TYPE_CHECKING:
    from risk.portfolio import Portfolio

logger = logging.getLogger(__name__)


class RiskManager:
    """Central risk control gate. Every trade must pass check_trade() before execution."""

    def __init__(
        self,
        max_daily_loss: float = 50.0,
        max_positions: int = 10,
        max_exposure: float = 500.0,
        max_exposure_per_category: float = 200.0,
        max_consecutive_losses: int = 3,
        min_hours_to_resolution: float = 24.0,
        max_position_size: float = 100.0,
        max_positions_per_category: int = 3,
        max_correlated_positions: int = 2,
        max_same_outcome_per_category: int = 2,
        portfolio: "Portfolio" = None,
    ):
        self.max_daily_loss = max_daily_loss
        self.max_positions = max_positions
        self.max_exposure = max_exposure
        self.max_exposure_per_category = max_exposure_per_category
        self.max_consecutive_losses = max_consecutive_losses
        self.min_hours_to_resolution = min_hours_to_resolution
        self.max_position_size = max_position_size
        self.max_positions_per_category = max_positions_per_category
        self.max_correlated_positions = max_correlated_positions
        self.max_same_outcome_per_category = max_same_outcome_per_category
        self._portfolio = portfolio

        # State
        self._daily_pnl: float = 0.0
        self._daily_trades: int = 0
        self._consecutive_losses: int = 0
        self._open_positions: int = 0
        self._open_exposure: float = 0.0
        self._category_exposure: dict[str, float] = {}
        self._kill_switch: bool = False
        self._circuit_broken: bool = False
        self._last_reset: Optional[datetime] = None

    def check_trade(self, signal: Signal, market: Market) -> tuple[bool, str]:
        """Check if a trade is allowed. Returns (allowed, reason).

        All checks must pass. First failure short-circuits.
        """
        # Kill switch
        if self._kill_switch:
            return False, "Kill switch active"

        # Circuit breaker
        if self._circuit_broken:
            return False, "Circuit breaker tripped"

        # Daily loss limit
        if self._daily_pnl <= -self.max_daily_loss:
            self._circuit_broken = True
            return False, f"Daily loss limit hit (${self._daily_pnl:.2f} <= -${self.max_daily_loss:.2f})"

        # Consecutive loss limit
        if self._consecutive_losses >= self.max_consecutive_losses:
            self._circuit_broken = True
            return False, f"Consecutive loss limit ({self._consecutive_losses} >= {self.max_consecutive_losses})"

        # Max positions
        if self._open_positions >= self.max_positions:
            return False, f"Max positions reached ({self._open_positions}/{self.max_positions})"

        # Max total exposure
        if self._open_exposure + signal.size > self.max_exposure:
            return False, f"Max exposure exceeded (${self._open_exposure:.2f} + ${signal.size:.2f} > ${self.max_exposure:.2f})"

        # Per-category exposure
        category = market.category or "other"
        cat_exp = self._category_exposure.get(category, 0.0)
        if cat_exp + signal.size > self.max_exposure_per_category:
            return False, f"Category '{category}' exposure limit (${cat_exp:.2f} + ${signal.size:.2f} > ${self.max_exposure_per_category:.2f})"

        # Position size cap
        if signal.size > self.max_position_size:
            return False, f"Position size too large (${signal.size:.2f} > ${self.max_position_size:.2f})"

        # Time to resolution
        hours = market.hours_to_resolution
        if hours is not None and hours < self.min_hours_to_resolution:
            return False, f"Too close to resolution ({hours:.1f}h < {self.min_hours_to_resolution:.1f}h)"

        # Portfolio-based checks (correlated positions, directional diversity)
        if self._portfolio:
            category = market.category or "other"

            # Per-category position count limit
            cat_positions = self._portfolio.positions_in_category(category)
            if len(cat_positions) >= self.max_positions_per_category:
                return False, (
                    f"Category '{category}' position limit "
                    f"({len(cat_positions)}/{self.max_positions_per_category})"
                )

            # Correlated position limit (same topic/keywords)
            correlated = self._portfolio.find_correlated(market)
            if len(correlated) >= self.max_correlated_positions:
                return False, (
                    f"Correlated position limit "
                    f"({len(correlated)}/{self.max_correlated_positions})"
                )

            # Directional diversity — max same outcome per category
            outcome_count = self._portfolio.count_outcome_in_category(
                category, signal.outcome,
            )
            if outcome_count >= self.max_same_outcome_per_category:
                return False, (
                    f"Same-outcome limit in '{category}': "
                    f"{outcome_count} {signal.outcome.value} bets "
                    f"(max {self.max_same_outcome_per_category})"
                )

        return True, "OK"

    def set_portfolio(self, portfolio: "Portfolio"):
        """Wire portfolio for correlated/directional checks."""
        self._portfolio = portfolio

    def record_trade_open(self, size: float, category: str = "other"):
        """Record a new position being opened."""
        self._open_positions += 1
        self._open_exposure += size
        self._category_exposure[category] = self._category_exposure.get(category, 0.0) + size
        self._daily_trades += 1

    def record_trade_close(
        self, size: float, pnl: float, category: str = "other", strategy: str = "edge",
    ):
        """Record a position being closed."""
        self._open_positions = max(0, self._open_positions - 1)
        self._open_exposure = max(0, self._open_exposure - size)

        cat_exp = self._category_exposure.get(category, 0.0)
        self._category_exposure[category] = max(0, cat_exp - size)

        self._daily_pnl += pnl

        # Only count edge/MM losses for circuit breaker (arb is risk-free at resolution)
        if strategy != "arbitrage":
            if pnl < 0:
                self._consecutive_losses += 1
            else:
                self._consecutive_losses = 0

        logger.info(
            f"Risk: trade closed P/L=${pnl:.2f} | "
            f"daily=${self._daily_pnl:.2f} | "
            f"consec_losses={self._consecutive_losses} | "
            f"positions={self._open_positions}"
        )

    def activate_kill_switch(self, reason: str = "manual"):
        """Immediately stop all trading."""
        self._kill_switch = True
        logger.warning(f"KILL SWITCH ACTIVATED: {reason}")

    def deactivate_kill_switch(self):
        """Re-enable trading."""
        self._kill_switch = False
        logger.info("Kill switch deactivated")

    def reset_daily(self):
        """Reset daily counters. Call at 00:00 UTC."""
        self._daily_pnl = 0.0
        self._daily_trades = 0
        self._consecutive_losses = 0
        self._circuit_broken = False
        self._last_reset = datetime.now(timezone.utc)
        logger.info("Risk manager: daily reset")

    @property
    def is_trading_allowed(self) -> bool:
        """Quick check if any trading is currently allowed."""
        if self._kill_switch:
            return False
        if self._circuit_broken:
            return False
        if self._daily_pnl <= -self.max_daily_loss:
            return False
        if self._consecutive_losses >= self.max_consecutive_losses:
            return False
        return True

    def summary(self) -> dict:
        return {
            "trading_allowed": self.is_trading_allowed,
            "kill_switch": self._kill_switch,
            "circuit_broken": self._circuit_broken,
            "daily_pnl": round(self._daily_pnl, 2),
            "daily_trades": self._daily_trades,
            "consecutive_losses": self._consecutive_losses,
            "open_positions": self._open_positions,
            "open_exposure": round(self._open_exposure, 2),
            "category_exposure": {k: round(v, 2) for k, v in self._category_exposure.items()},
            "limits": {
                "max_daily_loss": self.max_daily_loss,
                "max_positions": self.max_positions,
                "max_exposure": self.max_exposure,
                "max_consecutive_losses": self.max_consecutive_losses,
            },
        }
