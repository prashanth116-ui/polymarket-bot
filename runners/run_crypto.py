"""Crypto scalper runner V3 — contrarian strategy for Polymarket 15-min BTC up/down markets.

After N consecutive same-direction window resolutions, bet on reversal.
Enter at T-300s (5 min before close) when token prices are near $0.50.

V3 changes (from V2):
- Replaced momentum signal with contrarian streak detection
- Enter at T-300s instead of T-60s (better prices near $0.50)
- Track resolution history across windows for streak detection
- Spot feed kept for monitoring/logging only (not for signal generation)
- Persist resolution history to disk for restart recovery

Usage:
    python -m runners.run_crypto --paper
    python -m runners.run_crypto --paper --asset btc --interval 15
"""

import argparse
import csv
import json as _json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from config.loader import load_settings
from core.constants import (
    CRYPTO_DEFAULT_BANKROLL,
    CRYPTO_DEFAULT_ENTRY_WINDOW_SECS,
    CRYPTO_DEFAULT_INTERVAL_MINS,
    CRYPTO_DEFAULT_MAX_CONSEC_LOSSES,
    CRYPTO_DEFAULT_MAX_DAILY_LOSS,
    CRYPTO_DEFAULT_MAX_ENTRY_PRICE,
    CRYPTO_DEFAULT_MIN_ENTRY_PRICE,
    CRYPTO_DEFAULT_MIN_STREAK,
    CRYPTO_DEFAULT_POSITION_SIZE,
    GAMMA_API_URL,
)
from core.types import ExitReason, Outcome, StrategyType
from data.clob_client import ClobReader
from data.spot_feed import BinanceSpotFeed
from data.storage import Storage
from execution.paper_executor import PaperExecutor
from runners.notifier import TelegramNotifier
from strategies.crypto_scalper import CryptoScalper

logger = logging.getLogger(__name__)


def current_window_slug(asset: str = "btc", interval_secs: int = 900) -> str:
    """Generate Polymarket slug for the current time window."""
    ts = int(time.time() // interval_secs) * interval_secs
    interval_label = f"{interval_secs // 60}m"
    return f"{asset}-updown-{interval_label}-{ts}"


def window_seconds_remaining(interval_secs: int = 900) -> int:
    """Seconds remaining until the current window closes."""
    now = time.time()
    window_start = int(now // interval_secs) * interval_secs
    window_end = window_start + interval_secs
    return max(0, int(window_end - now))


class CryptoTrader:
    """Main crypto scalper trading loop (V3 — contrarian)."""

    def __init__(
        self,
        asset: str = "btc",
        interval_mins: int = CRYPTO_DEFAULT_INTERVAL_MINS,
        position_size: float = CRYPTO_DEFAULT_POSITION_SIZE,
        bankroll: float = CRYPTO_DEFAULT_BANKROLL,
        min_streak: int = CRYPTO_DEFAULT_MIN_STREAK,
        min_entry_price: float = CRYPTO_DEFAULT_MIN_ENTRY_PRICE,
        max_entry_price: float = CRYPTO_DEFAULT_MAX_ENTRY_PRICE,
        entry_window_secs: int = CRYPTO_DEFAULT_ENTRY_WINDOW_SECS,
        max_trades_per_hour: int = 4,
        max_consec_losses: int = CRYPTO_DEFAULT_MAX_CONSEC_LOSSES,
        max_daily_loss: float = CRYPTO_DEFAULT_MAX_DAILY_LOSS,
    ):
        self.asset = asset
        self.interval_secs = interval_mins * 60
        self.entry_window_secs = entry_window_secs
        self.max_trades_per_hour = max_trades_per_hour
        self.max_consec_losses = max_consec_losses
        self.max_daily_loss = max_daily_loss
        self._running = False

        # Components
        self.spot_feed = BinanceSpotFeed(f"{asset}usdt")
        self.clob = ClobReader()
        self.storage = Storage()
        self.executor = PaperExecutor(
            initial_balance=bankroll,
            slippage_bps=50,
            storage=self.storage,
        )
        self.notifier = TelegramNotifier()
        self.strategy = CryptoScalper(
            min_streak=min_streak,
            min_entry_price=min_entry_price,
            max_entry_price=max_entry_price,
            base_position_size=position_size,
            entry_window_secs=entry_window_secs,
        )

        # State
        self._current_window_market = None
        self._current_window_ts = 0
        self._hourly_trades = 0
        self._hourly_reset_ts = 0

        # Resolution history — list of recent resolutions ["UP", "DOWN", "UP", ...]
        self._resolution_history = []
        self._history_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "logs", "crypto_history.json"
        )

        # P/L tracking
        self._total_trades = 0
        self._total_wins = 0
        self._total_losses = 0
        self._consec_losses = 0
        self._daily_pnl = 0.0
        self._daily_reset_date = ""
        self._consec_pause_until = 0

        # Pending trade
        self._pending_trade = None

        # CSV logging
        self._csv_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "logs", "crypto_windows.csv"
        )

    def run(self):
        """Main trading loop, synced to 15-minute window boundaries."""
        self._running = True
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        logger.info(
            f"CryptoTrader V3 (Contrarian) starting | asset={self.asset.upper()} | "
            f"interval={self.interval_secs // 60}m | "
            f"entry_window={self.entry_window_secs}s | "
            f"min_streak={self.strategy.min_streak} | "
            f"position_size=${self.strategy.base_position_size:.2f} | "
            f"bankroll=${self.executor.balance:.2f} | "
            f"price_range=${self.strategy.min_entry_price:.2f}-${self.strategy.max_entry_price:.2f}"
        )

        # Load resolution history from disk
        self._load_history()

        self.spot_feed.start()

        # Wait for spot feed
        for _ in range(30):
            if self.spot_feed.connected:
                break
            time.sleep(1)

        if not self.spot_feed.connected:
            logger.error("Failed to connect to spot feed after 30s")
            self.notifier.send_error("Crypto scalper: spot feed connection failed")
            return

        spot_data = self.spot_feed.get_price()
        if spot_data:
            logger.info(f"Spot feed connected | {self.asset.upper()}/USD = ${spot_data[0]:,.2f}")

        # Show current streak
        streak_dir, streak_len = self._get_current_streak()
        streak_info = f"{streak_len}x {streak_dir}" if streak_dir else "none"

        self.notifier.send(
            f"<b>Crypto Scalper V3 (Contrarian) Started</b>\n"
            f"Asset: {self.asset.upper()}\n"
            f"Interval: {self.interval_secs // 60}m\n"
            f"Balance: ${self.executor.balance:.2f}\n"
            f"Min streak: {self.strategy.min_streak}\n"
            f"Position size: ${self.strategy.base_position_size:.2f}\n"
            f"Price range: ${self.strategy.min_entry_price:.2f}-${self.strategy.max_entry_price:.2f}\n"
            f"Entry window: T-{self.entry_window_secs}s\n"
            f"Current streak: {streak_info}\n"
            f"History: {len(self._resolution_history)} windows"
        )

        self._init_csv()

        try:
            self._main_loop()
        except Exception as e:
            logger.error(f"CryptoTrader fatal error: {e}", exc_info=True)
            self.notifier.send_error(f"Crypto scalper crashed: {e}")
        finally:
            self._shutdown()

    def _main_loop(self):
        """Core loop: wait for entry zone -> check streak -> trade -> resolve."""
        while self._running:
            try:
                now = time.time()
                window_ts = int(now // self.interval_secs) * self.interval_secs
                deadline = window_ts + self.interval_secs

                # 1. Sleep until entry zone (T-300s = 5 min before close)
                entry_zone_start = deadline - self.entry_window_secs
                wait_secs = max(0, entry_zone_start - time.time())
                if wait_secs > 0:
                    remaining = int(deadline - time.time())
                    logger.info(
                        f"Waiting {wait_secs:.0f}s for entry zone "
                        f"({remaining}s remaining in window)"
                    )
                    if not self._interruptible_sleep(wait_secs):
                        break

                # 2. Check risk controls
                self._check_daily_reset()
                skip_reason = self._check_risk_controls()
                if skip_reason:
                    logger.info(f"Skipping window: {skip_reason}")
                    self._log_window(skip_reason=skip_reason)
                    # Still need to resolve window for history
                    self._sleep_until(deadline + 5)
                    self._resolve_and_record_history()
                    continue

                # 3. Check hourly trade limit
                self._check_hourly_reset()
                if self._hourly_trades >= self.max_trades_per_hour:
                    logger.info(f"Hourly limit ({self._hourly_trades}/{self.max_trades_per_hour})")
                    self._log_window(skip_reason="hourly_limit")
                    self._sleep_until(deadline + 5)
                    self._resolve_and_record_history()
                    continue

                # 4. Check streak
                streak_dir, streak_len = self._get_current_streak()
                logger.info(
                    f"Current streak: {streak_len}x {streak_dir or 'none'} "
                    f"(need >= {self.strategy.min_streak})"
                )

                # 5. Discover market
                market = self._discover_market()
                if not market:
                    logger.warning("Could not find market, skipping")
                    self._log_window(skip_reason="no_market")
                    self._sleep_until(deadline + 5)
                    self._resolve_and_record_history()
                    continue

                # 6. Try to trade
                traded = self._entry_zone_loop(market, streak_dir, streak_len)

                # 7. Wait for window close
                wait_for_close = deadline - time.time()
                if wait_for_close > 0:
                    logger.info(f"Waiting {wait_for_close:.0f}s for window close...")
                    if not self._interruptible_sleep(wait_for_close + 5):
                        break

                # 8. Resolve and record history
                if traded:
                    self._check_resolution(market)
                else:
                    self._log_window(market=market, skip_reason="no_signal")

                # Always record resolution to history (whether we traded or not)
                self._resolve_and_record_history()

            except Exception as e:
                logger.error(f"Loop iteration error: {e}", exc_info=True)
                time.sleep(10)

    def _entry_zone_loop(self, market: dict, streak_dir: Optional[str], streak_len: int) -> bool:
        """Poll market data in entry zone, try to get a signal.

        Returns True if a trade was executed.
        """
        condition_id = market["condition_id"]
        up_token_id = market["up_token_id"]
        down_token_id = market["down_token_id"]
        clob_failures = 0

        window_ts = int(time.time() // self.interval_secs) * self.interval_secs
        deadline = window_ts + self.interval_secs

        while self._running:
            now = time.time()
            if now >= deadline:
                break
            remaining = int(deadline - now)

            # Get Polymarket prices
            up_price = self.clob.get_midpoint(up_token_id)
            down_price = self.clob.get_midpoint(down_token_id)

            if up_price is None or down_price is None:
                clob_failures += 1
                if clob_failures >= 3:
                    logger.warning(f"CLOB {clob_failures} failures — skipping window")
                    return False
                logger.debug(f"CLOB fail ({clob_failures}/3), retrying...")
                time.sleep(5)
                continue

            clob_failures = 0

            # Evaluate contrarian strategy
            sig = self.strategy.evaluate(
                streak_direction=streak_dir,
                streak_length=streak_len,
                window_seconds_remaining=remaining,
                up_price=up_price,
                down_price=down_price,
                up_token_id=up_token_id,
                down_token_id=down_token_id,
                market_id=condition_id,
            )

            if sig:
                token_id = sig.metadata.get("token_id", up_token_id)
                direction = sig.metadata.get("direction", "UP")

                result = self.executor.buy(
                    market_id=sig.market_id,
                    token_id=token_id,
                    outcome=sig.outcome,
                    price=sig.price,
                    size=sig.size,
                    strategy=StrategyType.CRYPTO_SCALPER,
                )

                self._hourly_trades += 1
                self._total_trades += 1

                self._pending_trade = {
                    "market": market,
                    "direction": direction,
                    "outcome": sig.outcome,
                    "cost_basis": result.cost,
                    "size": result.size,
                    "entry_price": result.price,
                    "token_id": token_id,
                    "streak_direction": streak_dir,
                    "streak_length": streak_len,
                    "up_price": up_price,
                    "down_price": down_price,
                }

                self.storage.record_trade(
                    market_id=sig.market_id,
                    outcome=sig.outcome.value,
                    side="BUY",
                    price=result.price,
                    size=result.size,
                    cost=result.cost,
                    fee=result.fee,
                    order_id=result.order_id,
                    strategy="crypto_scalper",
                )

                spot_data = self.spot_feed.get_price()
                spot_price = spot_data[0] if spot_data else 0

                self.notifier.send(
                    f"<b>CRYPTO ENTRY (Contrarian)</b>\n"
                    f"Streak: {streak_len}x {streak_dir} → bet {direction}\n"
                    f"BTC: ${spot_price:,.2f}\n"
                    f"Token: {sig.outcome.value} @ ${result.price:.4f}\n"
                    f"Size: ${result.cost:.2f}\n"
                    f"Edge: {sig.edge:.1%}\n"
                    f"Window: {remaining}s remaining\n"
                    f"Balance: ${self.executor.balance:.2f}\n"
                    f"Record: {self._total_wins}W-{self._total_losses}L"
                )

                logger.info(
                    f"ENTRY Contrarian {direction} | streak={streak_len}x{streak_dir} | "
                    f"{sig.outcome.value} @ ${result.price:.4f} x {result.size:.2f} | "
                    f"cost=${result.cost:.2f}"
                )
                return True

            # Poll every 10s in entry zone
            time.sleep(10)

        return False

    def _discover_market(self) -> Optional[dict]:
        """Find the Polymarket market for the current 15-min window."""
        window_ts = int(time.time() // self.interval_secs) * self.interval_secs
        if self._current_window_market and self._current_window_ts == window_ts:
            return self._current_window_market

        slug = current_window_slug(self.asset, self.interval_secs)
        logger.info(f"Discovering market: {slug}")

        try:
            resp = requests.get(
                f"{GAMMA_API_URL}/events",
                params={"slug": slug},
                timeout=15,
            )
            resp.raise_for_status()
            events = resp.json()
        except Exception as e:
            logger.error(f"Gamma API error: {e}")
            return None

        if not events:
            logger.warning(f"No event for slug={slug}")
            return None

        event = events[0]
        event_markets = event.get("markets", [])
        if not event_markets:
            return None

        raw_market = event_markets[0]

        outcomes_raw = raw_market.get("outcomes", "[]")
        if isinstance(outcomes_raw, str):
            outcomes = _json.loads(outcomes_raw)
        else:
            outcomes = outcomes_raw or []

        clob_ids_raw = raw_market.get("clobTokenIds", "[]")
        if isinstance(clob_ids_raw, str):
            clob_ids = _json.loads(clob_ids_raw)
        else:
            clob_ids = clob_ids_raw or []

        up_token_id = ""
        down_token_id = ""
        for i, outcome in enumerate(outcomes):
            if i < len(clob_ids):
                if outcome.lower() == "up":
                    up_token_id = clob_ids[i]
                elif outcome.lower() == "down":
                    down_token_id = clob_ids[i]

        if not up_token_id or not down_token_id:
            logger.warning(f"Could not map tokens: {outcomes}, {clob_ids}")
            return None

        result = {
            "condition_id": raw_market.get("conditionId", ""),
            "question": raw_market.get("question", event.get("title", "")),
            "up_token_id": up_token_id,
            "down_token_id": down_token_id,
            "slug": slug,
        }

        self._current_window_market = result
        self._current_window_ts = window_ts

        logger.info(f"Market: {result['question']}")
        return result

    def _check_resolution(self, market: dict):
        """Check how the window resolved using Gamma API outcomePrices."""
        slug = market.get("slug", "")
        condition_id = market["condition_id"]

        if not self._pending_trade:
            logger.warning("No pending trade to resolve")
            return

        trade = self._pending_trade
        direction = trade["direction"]
        outcome = trade["outcome"]
        cost_basis = trade["cost_basis"]
        entry_size = trade["size"]

        resolution = None
        for attempt in range(24):
            time.sleep(5)
            try:
                resp = requests.get(
                    f"{GAMMA_API_URL}/events",
                    params={"slug": slug},
                    timeout=10,
                )
                if resp.status_code == 200:
                    events = resp.json()
                    if events:
                        mkt = events[0].get("markets", [{}])[0]
                        outcome_prices = mkt.get("outcomePrices", "")

                        if isinstance(outcome_prices, str):
                            try:
                                prices = _json.loads(outcome_prices)
                            except (ValueError, TypeError):
                                prices = []
                        else:
                            prices = outcome_prices or []

                        if len(prices) >= 2:
                            up_resolved = float(prices[0])
                            down_resolved = float(prices[1])
                            if up_resolved > 0.5:
                                resolution = "UP"
                            elif down_resolved > 0.5:
                                resolution = "DOWN"
            except Exception as e:
                logger.debug(f"Resolution poll error: {e}")

            if resolution:
                break

        if resolution is None:
            logger.warning("Window did not resolve in 120s — recording as loss")
            resolution = "UNKNOWN"

        won = (direction == resolution)

        if won:
            pnl = entry_size - cost_basis
            self._total_wins += 1
            self._consec_losses = 0
        else:
            pnl = -cost_basis
            self._total_losses += 1
            self._consec_losses += 1

        self._daily_pnl += pnl

        if resolution != "UNKNOWN":
            res_outcome = "YES" if resolution == "UP" else "NO"
            self.executor.resolve_position(
                market_id=condition_id,
                outcome=outcome,
                resolution=res_outcome,
            )
        else:
            self.executor.sell(
                market_id=condition_id,
                token_id=trade["token_id"],
                outcome=outcome,
                price=0.0,
                size=entry_size,
                exit_reason=ExitReason.WINDOW_EXPIRED,
            )

        self.storage.record_trade(
            market_id=condition_id,
            outcome=outcome.value,
            side="SELL",
            price=1.0 if won else 0.0,
            size=entry_size,
            cost=entry_size if won else 0.0,
            fee=0,
            strategy="crypto_scalper",
            exit_reason="window_expired",
        )

        self._log_window(
            market=market,
            traded=True,
            direction=direction,
            entry_price=trade["entry_price"],
            cost_basis=cost_basis,
            resolution=resolution,
            won=won,
            pnl=pnl,
            streak_direction=trade.get("streak_direction"),
            streak_length=trade.get("streak_length", 0),
            up_price=trade.get("up_price"),
            down_price=trade.get("down_price"),
        )

        result_emoji = "WIN" if won else "LOSS"
        self.notifier.send(
            f"<b>{result_emoji}</b>\n"
            f"Market: {market['question']}\n"
            f"Bet: {direction} (vs {trade.get('streak_length', 0)}x {trade.get('streak_direction', '')} streak)\n"
            f"Resolution: {resolution}\n"
            f"Entry: ${trade['entry_price']:.4f}\n"
            f"P/L: ${pnl:+.2f}\n"
            f"Balance: ${self.executor.balance:.2f}\n"
            f"Daily P/L: ${self._daily_pnl:+.2f}\n"
            f"Record: {self._total_wins}W-{self._total_losses}L\n"
            f"Consec losses: {self._consec_losses}"
        )

        logger.info(
            f"{'WIN' if won else 'LOSS'} | bet {direction} vs streak "
            f"{trade.get('streak_length', 0)}x{trade.get('streak_direction', '')} | "
            f"resolution={resolution} | "
            f"P/L=${pnl:+.2f} | balance=${self.executor.balance:.2f}"
        )

        self._pending_trade = None

    # --- Streak tracking ---

    def _get_current_streak(self) -> tuple[Optional[str], int]:
        """Get current streak direction and length from resolution history.

        Returns (direction, length) or (None, 0) if no history.
        """
        if not self._resolution_history:
            return None, 0

        current_dir = self._resolution_history[-1]
        length = 0
        for res in reversed(self._resolution_history):
            if res == current_dir:
                length += 1
            else:
                break

        return current_dir, length

    def _resolve_and_record_history(self):
        """Fetch resolution for the most recent closed window and add to history."""
        # The window that just closed
        now = time.time()
        prev_window_ts = int(now // self.interval_secs) * self.interval_secs - self.interval_secs
        slug = f"{self.asset}-updown-{self.interval_secs // 60}m-{prev_window_ts}"

        resolution = self._fetch_resolution(slug)
        if resolution:
            self._resolution_history.append(resolution)
            # Keep last 20 windows max
            if len(self._resolution_history) > 20:
                self._resolution_history = self._resolution_history[-20:]
            self._save_history()

            streak_dir, streak_len = self._get_current_streak()
            logger.info(
                f"History updated: {resolution} | "
                f"streak={streak_len}x {streak_dir} | "
                f"last 10: {' '.join(r[0] for r in self._resolution_history[-10:])}"
            )
        else:
            logger.warning(f"Could not resolve {slug}")

    def _fetch_resolution(self, slug: str) -> Optional[str]:
        """Fetch resolution for a specific window slug."""
        for attempt in range(6):
            try:
                resp = requests.get(
                    f"{GAMMA_API_URL}/events",
                    params={"slug": slug},
                    timeout=10,
                )
                if resp.status_code == 200:
                    events = resp.json()
                    if events:
                        mkt = events[0].get("markets", [{}])[0]
                        outcome_prices = mkt.get("outcomePrices", "")
                        if isinstance(outcome_prices, str):
                            try:
                                prices = _json.loads(outcome_prices)
                            except (ValueError, TypeError):
                                prices = []
                        else:
                            prices = outcome_prices or []

                        if len(prices) >= 2:
                            if float(prices[0]) > 0.5:
                                return "UP"
                            elif float(prices[1]) > 0.5:
                                return "DOWN"
            except Exception as e:
                logger.debug(f"Resolution fetch error: {e}")

            if attempt < 5:
                time.sleep(5)

        return None

    def _load_history(self):
        """Load resolution history from disk."""
        try:
            if os.path.exists(self._history_path):
                with open(self._history_path) as f:
                    data = _json.load(f)
                    self._resolution_history = data.get("history", [])
                    logger.info(
                        f"Loaded {len(self._resolution_history)} resolution history entries"
                    )
        except Exception as e:
            logger.warning(f"Could not load history: {e}")
            self._resolution_history = []

    def _save_history(self):
        """Save resolution history to disk."""
        try:
            log_dir = os.path.dirname(self._history_path)
            os.makedirs(log_dir, exist_ok=True)
            with open(self._history_path, "w") as f:
                _json.dump({"history": self._resolution_history}, f)
        except Exception as e:
            logger.debug(f"Could not save history: {e}")

    # --- Risk controls ---

    def _check_risk_controls(self) -> Optional[str]:
        """Check if risk controls prevent trading."""
        if self._consec_pause_until > time.time():
            remaining = int(self._consec_pause_until - time.time())
            return f"consec_loss_pause ({remaining}s remaining)"

        if self._consec_losses >= self.max_consec_losses:
            self._consec_pause_until = time.time() + 3600
            logger.warning(f"Consec loss limit ({self._consec_losses}), pausing 1 hour")
            self.notifier.send(
                f"<b>CONSEC LOSS PAUSE</b>\n"
                f"Consecutive losses: {self._consec_losses}\n"
                f"Pausing 1 hour\n"
                f"Daily P/L: ${self._daily_pnl:+.2f}"
            )
            return f"consec_losses={self._consec_losses}"

        if self._daily_pnl <= -self.max_daily_loss:
            return f"daily_loss_limit (${self._daily_pnl:+.2f})"

        return None

    def _check_daily_reset(self):
        """Reset daily P/L at midnight UTC."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._daily_reset_date:
            if self._daily_reset_date:
                logger.info(f"Daily reset | P/L: ${self._daily_pnl:+.2f}")
            self._daily_pnl = 0.0
            self._consec_losses = 0
            self._consec_pause_until = 0
            self._daily_reset_date = today

    def _check_hourly_reset(self):
        """Reset hourly trade counter."""
        current_hour = int(time.time() // 3600)
        if current_hour != self._hourly_reset_ts:
            self._hourly_trades = 0
            self._hourly_reset_ts = current_hour

    # --- Utilities ---

    def _sleep_until(self, target_time: float) -> bool:
        """Sleep until absolute timestamp."""
        wait = target_time - time.time()
        if wait > 0:
            return self._interruptible_sleep(wait)
        return self._running

    def _interruptible_sleep(self, seconds: float) -> bool:
        """Sleep that can be interrupted by shutdown."""
        end = time.time() + seconds
        while time.time() < end and self._running:
            time.sleep(min(1.0, end - time.time()))
        return self._running

    def _handle_shutdown(self, signum, frame):
        logger.info(f"Shutdown signal ({signum})")
        self._running = False

    def _shutdown(self):
        """Clean shutdown."""
        logger.info("Shutting down CryptoTrader...")

        positions = self.executor.get_positions()
        for pos in positions:
            current_price = self.clob.get_midpoint(pos.token_id) or 0.5
            self.executor.sell(
                market_id=pos.market_id,
                token_id=pos.token_id,
                outcome=pos.outcome,
                price=current_price,
                size=pos.size,
                exit_reason=ExitReason.MANUAL,
            )

        streak_dir, streak_len = self._get_current_streak()
        self.notifier.send(
            f"<b>Crypto Scalper V3 Stopped</b>\n"
            f"Trades: {self._total_trades} "
            f"({self._total_wins}W-{self._total_losses}L)\n"
            f"Balance: ${self.executor.balance:.2f}\n"
            f"Daily P/L: ${self._daily_pnl:+.2f}\n"
            f"Final streak: {streak_len}x {streak_dir or 'none'}"
        )

        self.spot_feed.stop()
        self.storage.close()
        logger.info("CryptoTrader shutdown complete")

    # --- CSV logging ---

    def _init_csv(self):
        """Initialize CSV log file."""
        log_dir = os.path.dirname(self._csv_path)
        os.makedirs(log_dir, exist_ok=True)

        if not os.path.exists(self._csv_path):
            with open(self._csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "window_slug", "question",
                    "btc_price", "up_price", "down_price",
                    "streak_direction", "streak_length",
                    "traded", "direction", "entry_price", "cost_basis",
                    "resolution", "won", "pnl",
                    "skip_reason", "balance", "daily_pnl",
                    "consec_losses", "total_wins", "total_losses",
                ])

    def _log_window(
        self,
        market: Optional[dict] = None,
        traded: bool = False,
        direction: str = "",
        entry_price: float = 0.0,
        cost_basis: float = 0.0,
        resolution: str = "",
        won: Optional[bool] = None,
        pnl: float = 0.0,
        skip_reason: str = "",
        streak_direction: Optional[str] = None,
        streak_length: int = 0,
        up_price: Optional[float] = None,
        down_price: Optional[float] = None,
    ):
        """Log window data to CSV."""
        try:
            spot_data = self.spot_feed.get_price()
            btc_price = spot_data[0] if spot_data else 0.0

            slug = market.get("slug", "") if market else current_window_slug(self.asset, self.interval_secs)
            question = market.get("question", "") if market else ""

            with open(self._csv_path, "a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    datetime.now(timezone.utc).isoformat(),
                    slug, question,
                    f"{btc_price:.2f}",
                    f"{up_price:.4f}" if up_price is not None else "",
                    f"{down_price:.4f}" if down_price is not None else "",
                    streak_direction or "",
                    streak_length,
                    traded, direction,
                    f"{entry_price:.4f}" if entry_price else "",
                    f"{cost_basis:.2f}" if cost_basis else "",
                    resolution,
                    won if won is not None else "",
                    f"{pnl:.2f}" if traded else "",
                    skip_reason,
                    f"{self.executor.balance:.2f}",
                    f"{self._daily_pnl:.2f}",
                    self._consec_losses,
                    self._total_wins,
                    self._total_losses,
                ])
        except Exception as e:
            logger.debug(f"CSV logging error: {e}")


def main():
    parser = argparse.ArgumentParser(description="Crypto scalper V3 (contrarian)")
    parser.add_argument("--paper", action="store_true", default=True)
    parser.add_argument("--asset", default="btc")
    parser.add_argument("--interval", type=int, default=CRYPTO_DEFAULT_INTERVAL_MINS)
    parser.add_argument("--position-size", type=float, default=None)
    parser.add_argument("--bankroll", type=float, default=None)
    parser.add_argument("--min-streak", type=int, default=None)
    parser.add_argument("--entry-window", type=int, default=None)
    parser.add_argument("--max-consec-losses", type=int, default=None)
    parser.add_argument("--max-daily-loss", type=float, default=None)
    args = parser.parse_args()

    settings = load_settings()
    crypto_cfg = settings.get("crypto_scalper", {})

    position_size = args.position_size or crypto_cfg.get("position_size", CRYPTO_DEFAULT_POSITION_SIZE)
    bankroll = args.bankroll or crypto_cfg.get("bankroll", CRYPTO_DEFAULT_BANKROLL)
    min_streak = args.min_streak or crypto_cfg.get("min_streak", CRYPTO_DEFAULT_MIN_STREAK)
    entry_window = args.entry_window or crypto_cfg.get("entry_window_secs", CRYPTO_DEFAULT_ENTRY_WINDOW_SECS)
    max_consec = args.max_consec_losses or crypto_cfg.get("max_consec_losses", CRYPTO_DEFAULT_MAX_CONSEC_LOSSES)
    max_daily = args.max_daily_loss or crypto_cfg.get("max_daily_loss", CRYPTO_DEFAULT_MAX_DAILY_LOSS)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    )

    trader = CryptoTrader(
        asset=args.asset,
        interval_mins=args.interval,
        position_size=position_size,
        bankroll=bankroll,
        min_streak=min_streak,
        entry_window_secs=entry_window,
        max_consec_losses=max_consec,
        max_daily_loss=max_daily,
    )
    trader.run()


if __name__ == "__main__":
    main()
