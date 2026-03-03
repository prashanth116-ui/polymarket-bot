"""Crypto scalper runner V2 — trades Polymarket 15-min BTC up/down markets.

Separate process from the edge strategy bot. Runs a tight loop synced to
15-minute window boundaries, trading in the last ~60 seconds when BTC spot
direction is clear but Polymarket odds haven't caught up.

V2 changes (from V1):
- Fixed resolution detection: uses Gamma API outcomePrices (CLOB 404s after window close)
- Added consecutive loss breaker (3 losses → pause 1 hour)
- Added daily drawdown limit ($50)
- Added CSV window logging for offline analysis
- Handles CLOB 404s gracefully (max 3 failures → skip window)
- Better position tracking and P/L reporting

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
import sys
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
    CRYPTO_DEFAULT_MIN_MOMENTUM,
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
    """Generate Polymarket slug for the current time window.

    Polymarket crypto up/down markets use slugs like:
        btc-updown-15m-1709312400
    where the timestamp is the window start (floored to interval).
    """
    ts = int(time.time() // interval_secs) * interval_secs
    interval_label = f"{interval_secs // 60}m"
    return f"{asset}-updown-{interval_label}-{ts}"


def window_seconds_remaining(interval_secs: int = 900) -> int:
    """Seconds remaining until the current window closes."""
    now = time.time()
    window_start = int(now // interval_secs) * interval_secs
    window_end = window_start + interval_secs
    return max(0, int(window_end - now))


def next_entry_zone_wait(interval_secs: int = 900, entry_window_secs: int = 60) -> float:
    """Seconds to sleep until the next entry zone opens.

    The entry zone is the last entry_window_secs seconds of the window.
    """
    remaining = window_seconds_remaining(interval_secs)
    if remaining <= entry_window_secs:
        return 0  # Already in entry zone
    return remaining - entry_window_secs


class CryptoTrader:
    """Main crypto scalper trading loop (V2)."""

    def __init__(
        self,
        asset: str = "btc",
        interval_mins: int = CRYPTO_DEFAULT_INTERVAL_MINS,
        position_size: float = CRYPTO_DEFAULT_POSITION_SIZE,
        bankroll: float = CRYPTO_DEFAULT_BANKROLL,
        min_momentum: float = CRYPTO_DEFAULT_MIN_MOMENTUM,
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
            min_momentum=min_momentum,
            min_entry_price=min_entry_price,
            max_entry_price=max_entry_price,
            base_position_size=position_size,
            entry_window_secs=entry_window_secs,
        )

        # State
        self._current_window_market = None  # Cached market for current window
        self._current_window_ts = 0  # Window start timestamp for cache invalidation
        self._hourly_trades = 0
        self._hourly_reset_ts = 0

        # P/L tracking
        self._total_trades = 0
        self._total_wins = 0
        self._total_losses = 0
        self._consec_losses = 0
        self._daily_pnl = 0.0
        self._daily_reset_date = ""
        self._consec_pause_until = 0  # Timestamp when consec loss pause ends

        # Pending trade (waiting for resolution)
        self._pending_trade = None  # {market, direction, outcome, cost_basis, size, entry_price}

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
            f"CryptoTrader V2 starting | asset={self.asset.upper()} | "
            f"interval={self.interval_secs // 60}m | "
            f"entry_window={self.entry_window_secs}s | "
            f"position_size=${self.strategy.base_position_size:.2f} | "
            f"bankroll=${self.executor.balance:.2f} | "
            f"min_momentum={self.strategy.min_momentum:.4f} | "
            f"price_range=${self.strategy.min_entry_price:.2f}-${self.strategy.max_entry_price:.2f} | "
            f"max_consec_losses={self.max_consec_losses} | "
            f"max_daily_loss=${self.max_daily_loss:.2f}"
        )

        self.spot_feed.start()

        # Wait for spot feed to connect
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

        self.notifier.send(
            f"<b>Crypto Scalper V2 Started</b>\n"
            f"Asset: {self.asset.upper()}\n"
            f"Interval: {self.interval_secs // 60}m\n"
            f"Balance: ${self.executor.balance:.2f}\n"
            f"Position size: ${self.strategy.base_position_size:.2f}\n"
            f"Min momentum: {self.strategy.min_momentum:.2%}\n"
            f"Price range: ${self.strategy.min_entry_price:.2f}-${self.strategy.max_entry_price:.2f}\n"
            f"Consec loss limit: {self.max_consec_losses}\n"
            f"Daily loss limit: ${self.max_daily_loss:.2f}"
        )

        # Initialize CSV
        self._init_csv()

        try:
            self._main_loop()
        except Exception as e:
            logger.error(f"CryptoTrader fatal error: {e}", exc_info=True)
            self.notifier.send_error(f"Crypto scalper crashed: {e}")
        finally:
            self._shutdown()

    def _main_loop(self):
        """Core loop: sleep until entry zone -> evaluate -> trade -> wait for resolution."""
        while self._running:
            try:
                # 1. Sleep until entry zone
                wait_secs = next_entry_zone_wait(self.interval_secs, self.entry_window_secs)
                if wait_secs > 0:
                    remaining = window_seconds_remaining(self.interval_secs)
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
                    self._sleep_past_window()
                    continue

                # 3. Check hourly trade limit
                self._check_hourly_reset()
                if self._hourly_trades >= self.max_trades_per_hour:
                    logger.info(
                        f"Hourly trade limit reached ({self._hourly_trades}/{self.max_trades_per_hour})"
                    )
                    self._log_window(skip_reason="hourly_limit")
                    self._sleep_past_window()
                    continue

                # 4. Discover current window market
                market = self._discover_market()
                if not market:
                    logger.warning("Could not find market for current window, skipping")
                    self._log_window(skip_reason="no_market")
                    self._sleep_past_window()
                    continue

                # 5. Try to trade within the entry zone
                traded = self._entry_zone_loop(market)

                # 6. Wait for window to close
                remaining = window_seconds_remaining(self.interval_secs)
                if remaining > 0:
                    logger.info(f"Waiting {remaining}s for window to close...")
                    if not self._interruptible_sleep(remaining + 5):
                        break

                # 7. Check resolution (whether we traded or not, log the window)
                if traded:
                    self._check_resolution(market)
                else:
                    self._log_window(
                        market=market,
                        skip_reason="no_signal",
                    )

            except Exception as e:
                logger.error(f"Loop iteration error: {e}", exc_info=True)
                time.sleep(10)

    def _entry_zone_loop(self, market: dict) -> bool:
        """Poll spot + market data in the entry zone, try to get a signal.

        Returns True if a trade was executed.
        """
        condition_id = market["condition_id"]
        up_token_id = market["up_token_id"]
        down_token_id = market["down_token_id"]
        clob_failures = 0

        while self._running:
            remaining = window_seconds_remaining(self.interval_secs)
            if remaining <= 0:
                break

            # Get spot momentum
            momentum = self.spot_feed.get_momentum(window_secs=30)
            spot_data = self.spot_feed.get_price()

            if spot_data:
                logger.debug(f"BTC=${spot_data[0]:,.2f} momentum={momentum}")

            # Get Polymarket prices (handle CLOB 404s gracefully)
            up_price = self.clob.get_midpoint(up_token_id)
            down_price = self.clob.get_midpoint(down_token_id)

            if up_price is None or down_price is None:
                clob_failures += 1
                if clob_failures >= 3:
                    logger.warning(
                        f"CLOB returned {clob_failures} consecutive failures — "
                        f"tokens may not be listed yet, skipping window"
                    )
                    return False
                logger.debug(f"CLOB price fetch failed ({clob_failures}/3), retrying...")
                time.sleep(5)
                continue

            clob_failures = 0  # Reset on success

            # Evaluate strategy
            sig = self.strategy.evaluate(
                spot_momentum=momentum,
                window_seconds_remaining=remaining,
                up_price=up_price,
                down_price=down_price,
                up_token_id=up_token_id,
                down_token_id=down_token_id,
                market_id=condition_id,
            )

            if sig:
                # Execute trade
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

                # Store pending trade for resolution
                self._pending_trade = {
                    "market": market,
                    "direction": direction,
                    "outcome": sig.outcome,
                    "cost_basis": result.cost,
                    "size": result.size,
                    "entry_price": result.price,
                    "token_id": token_id,
                    "momentum": momentum,
                    "up_price": up_price,
                    "down_price": down_price,
                }

                # Record trade
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

                # Telegram alert
                spot_price = spot_data[0] if spot_data else 0
                self.notifier.send(
                    f"<b>CRYPTO ENTRY</b>\n"
                    f"Direction: {direction}\n"
                    f"BTC Spot: ${spot_price:,.2f}\n"
                    f"Momentum: {momentum:.4%}\n"
                    f"Token: {sig.outcome.value} @ ${result.price:.4f}\n"
                    f"Size: ${result.cost:.2f}\n"
                    f"Edge: {sig.edge:.1%}\n"
                    f"Window: {remaining}s remaining\n"
                    f"Balance: ${self.executor.balance:.2f}\n"
                    f"Record: {self._total_wins}W-{self._total_losses}L"
                )

                logger.info(
                    f"ENTRY {direction} | BTC=${spot_price:,.2f} | "
                    f"momentum={momentum:.4%} | "
                    f"{sig.outcome.value} @ ${result.price:.4f} x {result.size:.2f} | "
                    f"cost=${result.cost:.2f}"
                )
                return True

            # Poll every 5 seconds within entry zone
            time.sleep(5)

        return False

    def _discover_market(self) -> Optional[dict]:
        """Find the Polymarket market for the current 15-min window.

        Uses the Gamma events endpoint with the exact slug.
        Returns dict with condition_id, up_token_id, down_token_id or None.
        """
        # Cache check — same window?
        window_ts = int(time.time() // self.interval_secs) * self.interval_secs
        if self._current_window_market and self._current_window_ts == window_ts:
            return self._current_window_market

        slug = current_window_slug(self.asset, self.interval_secs)
        logger.info(f"Discovering market for slug: {slug}")

        try:
            resp = requests.get(
                f"{GAMMA_API_URL}/events",
                params={"slug": slug},
                timeout=15,
            )
            resp.raise_for_status()
            events = resp.json()
        except Exception as e:
            logger.error(f"Gamma API error fetching event {slug}: {e}")
            return None

        if not events:
            logger.warning(f"No event found for slug={slug}")
            return None

        event = events[0]
        event_markets = event.get("markets", [])
        if not event_markets:
            logger.warning(f"Event {slug} has no markets")
            return None

        raw_market = event_markets[0]

        # Parse outcomes and token IDs
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

        # Map Up/Down outcomes to token IDs
        up_token_id = ""
        down_token_id = ""
        for i, outcome in enumerate(outcomes):
            if i < len(clob_ids):
                if outcome.lower() == "up":
                    up_token_id = clob_ids[i]
                elif outcome.lower() == "down":
                    down_token_id = clob_ids[i]

        if not up_token_id or not down_token_id:
            logger.warning(f"Could not map Up/Down tokens: outcomes={outcomes}, ids={clob_ids}")
            return None

        condition_id = raw_market.get("conditionId", "")
        question = raw_market.get("question", event.get("title", ""))

        result = {
            "condition_id": condition_id,
            "question": question,
            "up_token_id": up_token_id,
            "down_token_id": down_token_id,
            "slug": slug,
        }

        # Cache for this window
        self._current_window_market = result
        self._current_window_ts = window_ts

        logger.info(
            f"Market found: {question} | "
            f"Up={up_token_id[:16]}... Down={down_token_id[:16]}..."
        )
        return result

    def _check_resolution(self, market: dict):
        """Check how the window resolved using Gamma API outcomePrices.

        outcomePrices=["1", "0"] → Up won
        outcomePrices=["0", "1"] → Down won
        """
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

        # Poll Gamma API for resolution (markets resolve 5-30s after window close)
        resolution = None

        for attempt in range(24):  # Up to 120 seconds of polling
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

                        # outcomePrices: ["1","0"] = Up won, ["0","1"] = Down won
                        if len(prices) >= 2:
                            up_resolved = float(prices[0])
                            down_resolved = float(prices[1])

                            if up_resolved > 0.5:
                                resolution = "UP"
                                logger.info(
                                    f"Resolution: UP (outcomePrices={prices})"
                                )
                            elif down_resolved > 0.5:
                                resolution = "DOWN"
                                logger.info(
                                    f"Resolution: DOWN (outcomePrices={prices})"
                                )
            except Exception as e:
                logger.debug(f"Resolution poll error: {e}")

            if resolution:
                break

            if attempt < 23:
                logger.debug(f"Resolution poll {attempt+1}/24 — waiting...")

        if resolution is None:
            logger.warning(
                f"Window did not resolve within 120s — recording as loss"
            )
            resolution = "UNKNOWN"

        # Determine win/loss
        won = (direction == resolution)

        if won:
            # Token resolves to $1/share
            pnl = entry_size - cost_basis  # size shares * $1 each - cost
            self._total_wins += 1
            self._consec_losses = 0
        else:
            # Token resolves to $0/share
            pnl = -cost_basis
            self._total_losses += 1
            self._consec_losses += 1

        self._daily_pnl += pnl

        # Resolve in executor
        if resolution != "UNKNOWN":
            res_outcome = "YES" if resolution == "UP" else "NO"
            self.executor.resolve_position(
                market_id=condition_id,
                outcome=outcome,
                resolution=res_outcome,
            )
        else:
            # Force sell at 0 (unknown resolution = assume loss)
            self.executor.sell(
                market_id=condition_id,
                token_id=trade["token_id"],
                outcome=outcome,
                price=0.0,
                size=entry_size,
                exit_reason=ExitReason.WINDOW_EXPIRED,
            )

        # Record exit trade
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

        # Log to CSV
        self._log_window(
            market=market,
            traded=True,
            direction=direction,
            entry_price=trade["entry_price"],
            cost_basis=cost_basis,
            resolution=resolution,
            won=won,
            pnl=pnl,
            momentum=trade.get("momentum"),
            up_price=trade.get("up_price"),
            down_price=trade.get("down_price"),
        )

        # Telegram alert
        result_emoji = "WIN" if won else "LOSS"
        self.notifier.send(
            f"<b>{result_emoji}</b>\n"
            f"Market: {market['question']}\n"
            f"Direction: {direction}\n"
            f"Resolution: {resolution}\n"
            f"Entry: ${trade['entry_price']:.4f}\n"
            f"P/L: ${pnl:+.2f}\n"
            f"Balance: ${self.executor.balance:.2f}\n"
            f"Daily P/L: ${self._daily_pnl:+.2f}\n"
            f"Record: {self._total_wins}W-{self._total_losses}L\n"
            f"Consec losses: {self._consec_losses}"
        )

        logger.info(
            f"{'WIN' if won else 'LOSS'} | {direction} vs {resolution} | "
            f"entry=${trade['entry_price']:.4f} | P/L=${pnl:+.2f} | "
            f"balance=${self.executor.balance:.2f} | "
            f"daily_pnl=${self._daily_pnl:+.2f} | "
            f"record={self._total_wins}W-{self._total_losses}L | "
            f"consec_losses={self._consec_losses}"
        )

        # Clear pending trade
        self._pending_trade = None

    def _check_risk_controls(self) -> Optional[str]:
        """Check if risk controls prevent trading. Returns skip reason or None."""
        # Consecutive loss pause
        if self._consec_pause_until > time.time():
            remaining = int(self._consec_pause_until - time.time())
            return f"consec_loss_pause ({remaining}s remaining)"

        if self._consec_losses >= self.max_consec_losses:
            self._consec_pause_until = time.time() + 3600  # Pause 1 hour
            logger.warning(
                f"Consecutive loss limit reached ({self._consec_losses}), "
                f"pausing for 1 hour"
            )
            self.notifier.send(
                f"<b>CONSEC LOSS PAUSE</b>\n"
                f"Consecutive losses: {self._consec_losses}\n"
                f"Pausing for 1 hour\n"
                f"Daily P/L: ${self._daily_pnl:+.2f}"
            )
            return f"consec_losses={self._consec_losses}"

        # Daily drawdown limit
        if self._daily_pnl <= -self.max_daily_loss:
            return f"daily_loss_limit (${self._daily_pnl:+.2f})"

        return None

    def _check_daily_reset(self):
        """Reset daily P/L at midnight UTC."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._daily_reset_date:
            if self._daily_reset_date:
                logger.info(
                    f"Daily reset | yesterday P/L: ${self._daily_pnl:+.2f} | "
                    f"record: {self._total_wins}W-{self._total_losses}L"
                )
            self._daily_pnl = 0.0
            self._consec_losses = 0
            self._consec_pause_until = 0
            self._daily_reset_date = today

    def _check_hourly_reset(self):
        """Reset hourly trade counter at the start of each hour."""
        current_hour = int(time.time() // 3600)
        if current_hour != self._hourly_reset_ts:
            self._hourly_trades = 0
            self._hourly_reset_ts = current_hour

    def _sleep_past_window(self):
        """Sleep until the current window closes + buffer."""
        remaining = window_seconds_remaining(self.interval_secs)
        self._interruptible_sleep(remaining + 5)

    def _interruptible_sleep(self, seconds: float) -> bool:
        """Sleep that can be interrupted by shutdown signal.

        Returns True if sleep completed, False if interrupted.
        """
        end = time.time() + seconds
        while time.time() < end and self._running:
            time.sleep(min(1.0, end - time.time()))
        return self._running

    def _handle_shutdown(self, signum, frame):
        """Handle SIGINT/SIGTERM gracefully."""
        logger.info(f"Shutdown signal received ({signum})")
        self._running = False

    def _shutdown(self):
        """Clean shutdown — close open positions and stop feeds."""
        logger.info("Shutting down CryptoTrader...")

        # Close any open positions
        positions = self.executor.get_positions()
        for pos in positions:
            logger.info(f"Closing position on shutdown: {pos.market_id}")
            current_price = self.clob.get_midpoint(pos.token_id) or 0.5
            self.executor.sell(
                market_id=pos.market_id,
                token_id=pos.token_id,
                outcome=pos.outcome,
                price=current_price,
                size=pos.size,
                exit_reason=ExitReason.MANUAL,
            )

        # Send summary
        summary = self.executor.summary()
        summary["total_crypto_trades"] = self._total_trades
        summary["crypto_wins"] = self._total_wins
        summary["crypto_losses"] = self._total_losses
        self.notifier.send(
            f"<b>Crypto Scalper V2 Stopped</b>\n"
            f"Trades: {self._total_trades} "
            f"({self._total_wins}W-{self._total_losses}L)\n"
            f"Balance: ${summary['balance']:.2f}\n"
            f"Total P/L: ${summary['total_pnl']:.2f}\n"
            f"Daily P/L: ${self._daily_pnl:+.2f}"
        )

        self.spot_feed.stop()
        self.storage.close()
        logger.info("CryptoTrader shutdown complete")

    def _init_csv(self):
        """Initialize CSV log file with headers if it doesn't exist."""
        log_dir = os.path.dirname(self._csv_path)
        os.makedirs(log_dir, exist_ok=True)

        if not os.path.exists(self._csv_path):
            with open(self._csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "window_slug", "question",
                    "btc_price", "momentum", "up_price", "down_price",
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
        momentum: Optional[float] = None,
        up_price: Optional[float] = None,
        down_price: Optional[float] = None,
    ):
        """Log window data to CSV for offline analysis."""
        try:
            spot_data = self.spot_feed.get_price()
            btc_price = spot_data[0] if spot_data else 0.0
            if momentum is None:
                momentum = self.spot_feed.get_momentum(window_secs=30)

            slug = market.get("slug", "") if market else current_window_slug(self.asset, self.interval_secs)
            question = market.get("question", "") if market else ""

            with open(self._csv_path, "a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    datetime.now(timezone.utc).isoformat(),
                    slug,
                    question,
                    f"{btc_price:.2f}",
                    f"{momentum:.8f}" if momentum is not None else "",
                    f"{up_price:.4f}" if up_price is not None else "",
                    f"{down_price:.4f}" if down_price is not None else "",
                    traded,
                    direction,
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
    parser = argparse.ArgumentParser(description="Crypto scalper V2 for Polymarket up/down markets")
    parser.add_argument("--paper", action="store_true", default=True, help="Paper trading mode (default)")
    parser.add_argument("--asset", default="btc", help="Asset to trade (default: btc)")
    parser.add_argument("--interval", type=int, default=CRYPTO_DEFAULT_INTERVAL_MINS, help="Window interval in minutes")
    parser.add_argument("--position-size", type=float, default=None, help="USDC per trade (base)")
    parser.add_argument("--bankroll", type=float, default=None, help="Starting bankroll")
    parser.add_argument("--min-momentum", type=float, default=None, help="Minimum BTC momentum (fraction)")
    parser.add_argument("--entry-window", type=int, default=None, help="Entry window seconds before close")
    parser.add_argument("--max-consec-losses", type=int, default=None, help="Stop after N consecutive losses")
    parser.add_argument("--max-daily-loss", type=float, default=None, help="Max daily loss in USDC")
    args = parser.parse_args()

    # Load settings with CLI overrides
    settings = load_settings()
    crypto_cfg = settings.get("crypto_scalper", {})

    position_size = args.position_size or crypto_cfg.get("position_size", CRYPTO_DEFAULT_POSITION_SIZE)
    bankroll = args.bankroll or crypto_cfg.get("bankroll", CRYPTO_DEFAULT_BANKROLL)
    min_momentum = args.min_momentum or crypto_cfg.get("min_momentum", CRYPTO_DEFAULT_MIN_MOMENTUM)
    entry_window = args.entry_window or crypto_cfg.get("entry_window_secs", CRYPTO_DEFAULT_ENTRY_WINDOW_SECS)
    max_consec = args.max_consec_losses or crypto_cfg.get("max_consec_losses", CRYPTO_DEFAULT_MAX_CONSEC_LOSSES)
    max_daily = args.max_daily_loss or crypto_cfg.get("max_daily_loss", CRYPTO_DEFAULT_MAX_DAILY_LOSS)

    # Setup logging — use StreamHandler only (systemd captures stdout to log file)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    )

    trader = CryptoTrader(
        asset=args.asset,
        interval_mins=args.interval,
        position_size=position_size,
        bankroll=bankroll,
        min_momentum=min_momentum,
        entry_window_secs=entry_window,
        max_consec_losses=max_consec,
        max_daily_loss=max_daily,
    )
    trader.run()


if __name__ == "__main__":
    main()
