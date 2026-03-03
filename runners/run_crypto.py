"""Crypto scalper runner — trades Polymarket 15-min BTC up/down markets.

Separate process from the edge strategy bot. Runs a tight loop synced to
15-minute window boundaries, trading in the last ~60 seconds when BTC spot
direction is clear but Polymarket odds haven't caught up.

Usage:
    python -m runners.run_crypto --paper
    python -m runners.run_crypto --paper --asset btc --interval 15
"""

import argparse
import logging
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
    CRYPTO_DEFAULT_MAX_ENTRY_PRICE,
    CRYPTO_DEFAULT_MIN_MOMENTUM,
    CRYPTO_DEFAULT_MIN_PRICE_GAP,
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
    """Main crypto scalper trading loop."""

    def __init__(
        self,
        asset: str = "btc",
        interval_mins: int = CRYPTO_DEFAULT_INTERVAL_MINS,
        position_size: float = CRYPTO_DEFAULT_POSITION_SIZE,
        bankroll: float = CRYPTO_DEFAULT_BANKROLL,
        min_momentum: float = CRYPTO_DEFAULT_MIN_MOMENTUM,
        min_price_gap: float = CRYPTO_DEFAULT_MIN_PRICE_GAP,
        max_entry_price: float = CRYPTO_DEFAULT_MAX_ENTRY_PRICE,
        entry_window_secs: int = CRYPTO_DEFAULT_ENTRY_WINDOW_SECS,
        max_trades_per_hour: int = 4,
    ):
        self.asset = asset
        self.interval_secs = interval_mins * 60
        self.entry_window_secs = entry_window_secs
        self.max_trades_per_hour = max_trades_per_hour
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
            min_price_gap=min_price_gap,
            max_entry_price=max_entry_price,
            position_size=position_size,
            entry_window_secs=entry_window_secs,
        )

        # State
        self._current_window_market = None  # Cached market for current window
        self._current_window_ts = 0  # Window start timestamp for cache invalidation
        self._hourly_trades = 0
        self._hourly_reset_ts = 0
        self._total_trades = 0
        self._total_wins = 0
        self._total_losses = 0

    def run(self):
        """Main trading loop, synced to 15-minute window boundaries."""
        self._running = True
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        logger.info(
            f"CryptoTrader starting | asset={self.asset.upper()} | "
            f"interval={self.interval_secs // 60}m | "
            f"entry_window={self.entry_window_secs}s | "
            f"position_size=${self.strategy.position_size:.2f} | "
            f"bankroll=${self.executor.balance:.2f}"
        )

        self.spot_feed.start()

        # Wait for spot feed to connect
        for _ in range(30):
            if self.spot_feed.connected:
                break
            time.sleep(1)

        if not self.spot_feed.connected:
            logger.error("Failed to connect to Binance spot feed after 30s")
            self.notifier.send_error("Crypto scalper: Binance WS connection failed")
            return

        spot_data = self.spot_feed.get_price()
        if spot_data:
            logger.info(f"Binance connected | {self.asset.upper()}/USDT = ${spot_data[0]:,.2f}")

        self.notifier.send(
            f"<b>Crypto Scalper Started</b>\n"
            f"Asset: {self.asset.upper()}\n"
            f"Interval: {self.interval_secs // 60}m\n"
            f"Balance: ${self.executor.balance:.2f}\n"
            f"Position size: ${self.strategy.position_size:.2f}"
        )

        try:
            self._main_loop()
        except Exception as e:
            logger.error(f"CryptoTrader fatal error: {e}", exc_info=True)
            self.notifier.send_error(f"Crypto scalper crashed: {e}")
        finally:
            self._shutdown()

    def _main_loop(self):
        """Core loop: sleep until entry zone → evaluate → trade → wait for resolution."""
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

                # 2. Check hourly trade limit
                self._check_hourly_reset()
                if self._hourly_trades >= self.max_trades_per_hour:
                    logger.info(
                        f"Hourly trade limit reached ({self._hourly_trades}/{self.max_trades_per_hour}), "
                        f"skipping window"
                    )
                    self._sleep_past_window()
                    continue

                # 3. Discover current window market
                market = self._discover_market()
                if not market:
                    logger.warning("Could not find market for current window, skipping")
                    self._sleep_past_window()
                    continue

                # 4. Try to trade within the entry zone
                traded = self._entry_zone_loop(market)

                # 5. Wait for window to close
                remaining = window_seconds_remaining(self.interval_secs)
                if remaining > 0:
                    logger.info(f"Waiting {remaining}s for window to close...")
                    if not self._interruptible_sleep(remaining + 5):
                        break

                # 6. Check resolution
                if traded:
                    self._check_resolution(market)

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

        while self._running:
            remaining = window_seconds_remaining(self.interval_secs)
            if remaining <= 0:
                break

            # Get spot momentum
            momentum = self.spot_feed.get_momentum(window_secs=30)
            spot_data = self.spot_feed.get_price()

            if spot_data:
                logger.debug(f"BTC=${spot_data[0]:,.2f} momentum={momentum}")

            # Get Polymarket prices
            up_price = self.clob.get_midpoint(up_token_id)
            down_price = self.clob.get_midpoint(down_token_id)

            if up_price is None or down_price is None:
                logger.debug("Could not fetch Polymarket prices, retrying...")
                time.sleep(3)
                continue

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
                    f"Balance: ${self.executor.balance:.2f}"
                )

                logger.info(
                    f"ENTRY {direction} | BTC=${spot_price:,.2f} | "
                    f"momentum={momentum:.4%} | "
                    f"{sig.outcome.value} @ ${result.price:.4f} x {result.size:.2f}"
                )
                return True

            # Poll every 5 seconds within entry zone
            time.sleep(5)

        return False

    def _discover_market(self) -> Optional[dict]:
        """Find the Polymarket market for the current 15-min window.

        Uses the Gamma events endpoint with the exact slug (e.g. btc-updown-15m-1772506800).
        Returns dict with condition_id, up_token_id, down_token_id or None.
        """
        import json as _json

        # Cache check — same window?
        window_ts = int(time.time() // self.interval_secs) * self.interval_secs
        if self._current_window_market and self._current_window_ts == window_ts:
            return self._current_window_market

        slug = current_window_slug(self.asset, self.interval_secs)
        logger.info(f"Discovering market for slug: {slug}")

        # Fetch from Gamma events endpoint (not markets search)
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
        """Check how the window resolved and record P/L.

        Uses token prices as primary signal (go to ~1.0/~0.0 on resolution),
        falls back to Gamma API for confirmation.
        """
        condition_id = market["condition_id"]
        up_token = market["up_token_id"]
        down_token = market["down_token_id"]

        # Poll for resolution (market resolves 5-30s after window close)
        resolved = False
        resolution = None

        for attempt in range(24):  # Up to 120 seconds of polling
            time.sleep(5)

            # Primary: check token prices — they snap to ~1.0/~0.0 on resolution
            up_price = self.clob.get_midpoint(up_token)
            down_price = self.clob.get_midpoint(down_token)

            if up_price is not None and up_price > 0.90:
                resolution = "Up"
                logger.info(f"Up token at ${up_price:.4f} — resolved UP")
            elif down_price is not None and down_price > 0.90:
                resolution = "Down"
                logger.info(f"Down token at ${down_price:.4f} — resolved DOWN")

            # Fallback: check Gamma API for explicit resolution
            if resolution is None:
                try:
                    resp = requests.get(
                        f"{GAMMA_API_URL}/markets",
                        params={"conditionId": condition_id},
                        timeout=10,
                    )
                    if resp.status_code == 200:
                        markets_data = resp.json()
                        if markets_data and isinstance(markets_data, list):
                            res = markets_data[0].get("resolution")
                            if res:
                                resolution = res
                                logger.info(f"Gamma API resolution: {resolution}")
                except Exception as e:
                    logger.debug(f"Gamma resolution check error: {e}")

            if resolution is None:
                if attempt < 23:
                    logger.debug(f"Resolution poll {attempt+1}/24 — waiting...")
                continue

            # Map resolution to the outcome we hold
            # Our position outcome is YES (bought Up) or NO (bought Down)
            resolved = True
            positions = self.executor.get_positions()
            for pos in positions:
                if pos.market_id == condition_id:
                    # Determine if we won
                    if pos.outcome == Outcome.YES:
                        won = resolution == "Up"
                    else:
                        won = resolution == "Down"

                    pnl = pos.size - pos.cost_basis if won else -pos.cost_basis
                    if won:
                        self._total_wins += 1
                    else:
                        self._total_losses += 1

                    # Resolve in executor (payout $1/share if won, $0 if lost)
                    res_outcome = "YES" if resolution == "Up" else "NO"
                    self.executor.resolve_position(
                        market_id=condition_id,
                        outcome=pos.outcome,
                        resolution=res_outcome,
                    )

                    self.notifier.send(
                        f"{'<b>WIN</b>' if won else '<b>LOSS</b>'}\n"
                        f"Market: {market['question']}\n"
                        f"Resolution: {resolution}\n"
                        f"P/L: ${pnl:+.2f}\n"
                        f"Balance: ${self.executor.balance:.2f}\n"
                        f"Record: {self._total_wins}W-{self._total_losses}L"
                    )

                    self.storage.record_trade(
                        market_id=condition_id,
                        outcome=pos.outcome.value,
                        side="SELL",
                        price=1.0 if won else 0.0,
                        size=pos.size,
                        cost=pos.size if won else 0.0,
                        fee=0,
                        strategy="crypto_scalper",
                        exit_reason="window_expired",
                    )
            break

        if not resolved:
            logger.warning(
                f"Window did not resolve within 120s — "
                f"selling position at market price"
            )
            positions = self.executor.get_positions()
            for pos in positions:
                if pos.market_id == condition_id:
                    current_price = self.clob.get_midpoint(pos.token_id) or 0.5
                    self.executor.sell(
                        market_id=condition_id,
                        token_id=pos.token_id,
                        outcome=pos.outcome,
                        price=current_price,
                        size=pos.size,
                        exit_reason=ExitReason.WINDOW_EXPIRED,
                    )

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
            f"<b>Crypto Scalper Stopped</b>\n"
            f"Trades: {self._total_trades} "
            f"({self._total_wins}W-{self._total_losses}L)\n"
            f"Balance: ${summary['balance']:.2f}\n"
            f"Total P/L: ${summary['total_pnl']:.2f}"
        )

        self.spot_feed.stop()
        self.storage.close()
        logger.info("CryptoTrader shutdown complete")


def main():
    parser = argparse.ArgumentParser(description="Crypto scalper for Polymarket up/down markets")
    parser.add_argument("--paper", action="store_true", default=True, help="Paper trading mode (default)")
    parser.add_argument("--asset", default="btc", help="Asset to trade (default: btc)")
    parser.add_argument("--interval", type=int, default=CRYPTO_DEFAULT_INTERVAL_MINS, help="Window interval in minutes (default: 15)")
    parser.add_argument("--position-size", type=float, default=None, help="USDC per trade")
    parser.add_argument("--bankroll", type=float, default=None, help="Starting bankroll")
    parser.add_argument("--min-momentum", type=float, default=None, help="Minimum BTC momentum (fraction)")
    parser.add_argument("--entry-window", type=int, default=None, help="Entry window seconds before close")
    args = parser.parse_args()

    # Load settings with CLI overrides
    settings = load_settings()
    crypto_cfg = settings.get("crypto_scalper", {})

    position_size = args.position_size or crypto_cfg.get("position_size", CRYPTO_DEFAULT_POSITION_SIZE)
    bankroll = args.bankroll or crypto_cfg.get("bankroll", CRYPTO_DEFAULT_BANKROLL)
    min_momentum = args.min_momentum or crypto_cfg.get("min_momentum", CRYPTO_DEFAULT_MIN_MOMENTUM)
    entry_window = args.entry_window or crypto_cfg.get("entry_window_secs", CRYPTO_DEFAULT_ENTRY_WINDOW_SECS)

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
    )
    trader.run()


if __name__ == "__main__":
    main()
