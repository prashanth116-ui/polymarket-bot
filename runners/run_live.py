"""Main 24/7 trading loop — paper or live mode.

Usage:
    python -m runners.run_live --paper
    python -m runners.run_live --paper --bankroll 500
    python -m runners.run_live --live  # real money — careful!
"""

import argparse
import logging
import os
import signal
import sys
import time
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.loader import load_settings, get_env
from core.constants import (
    ARB_MAX_ARB_EXPOSURE,
    ARB_MAX_POSITION,
    ARB_MIN_PROFIT_BPS,
    DEFAULT_KELLY_FRACTION,
    DEFAULT_MAX_POSITION_SIZE,
    DEFAULT_MIN_EDGE,
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_MIN_LIQUIDITY,
    FULL_SCAN_INTERVAL,
    HEARTBEAT_INTERVAL,
    MM_BASE_SPREAD,
    MM_BOUNDARY_BUFFER,
    MM_MAX_INVENTORY,
    MM_MAX_MM_EXPOSURE,
    MM_MAX_MM_MARKETS,
    MM_MAX_SPREAD,
    MM_MIN_SPREAD,
    MM_QUOTE_SIZE,
    PRICE_POLL_INTERVAL,
)
from core.types import ExitReason, Market, OpenOrder, Outcome, Position, Side, Signal, SignalAction, StrategyType
from data.clob_client import ClobReader
from data.market_cache import MarketCache
from data.market_scanner import MarketScanner
from data.storage import Storage
from data.ws_price_feed import WebSocketPriceFeed
from execution.paper_executor import PaperExecutor
from execution.reconciler import PositionReconciler
from models.calibration import CalibrationTracker
from models.ensemble import EnsembleModel
from models.statistical import BaseRateModel, MarketImpliedModel, TimeDecayModel
from risk.portfolio import Portfolio
from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager
from runners.notifier import TelegramNotifier
from strategies.arbitrage import ArbitrageStrategy
from strategies.coordinator import StrategyCoordinator
from strategies.edge_strategy import EdgeStrategy
from data.sources.news_feed import NewsFeed
from strategies.market_maker import MarketMakerStrategy

logger = logging.getLogger(__name__)


class LiveTrader:
    """Main 24/7 trading loop."""

    def __init__(
        self,
        mode: str = "paper",
        bankroll: float = 1000.0,
        settings: dict = None,
    ):
        self.mode = mode
        self.settings = settings or load_settings()
        self._running = False
        self._shutdown_event = threading.Event()

        # --- Components ---
        risk_cfg = self.settings.get("risk", {})
        scan_cfg = self.settings.get("scan", {})
        strat_cfg = self.settings.get("strategy", {})
        mm_cfg = self.settings.get("market_maker", {})
        arb_cfg = self.settings.get("arbitrage", {})
        coord_cfg = self.settings.get("coordinator", {})

        # Data
        self.scanner = MarketScanner()
        self.clob = ClobReader()
        self.cache = MarketCache()
        self.storage = Storage(self.settings.get("database", {}).get("path", "data/polymarket.db"))

        # Models
        self.implied_model = MarketImpliedModel()
        self.base_rate_model = BaseRateModel()
        self.time_decay_model = TimeDecayModel()
        self.ensemble = EnsembleModel([
            self.implied_model,
            self.base_rate_model,
            self.time_decay_model,
        ])

        # Try to add LLM model if API key available
        self._init_llm_model()

        # Strategies
        self.edge_strategy = EdgeStrategy(
            model=self.ensemble,
            min_edge=strat_cfg.get("min_edge", DEFAULT_MIN_EDGE),
            min_confidence=strat_cfg.get("min_confidence", DEFAULT_MIN_CONFIDENCE),
            min_liquidity=strat_cfg.get("min_liquidity", DEFAULT_MIN_LIQUIDITY),
            kelly_mult=risk_cfg.get("kelly_fraction", DEFAULT_KELLY_FRACTION),
            max_position=risk_cfg.get("max_position_size", DEFAULT_MAX_POSITION_SIZE),
            bankroll=bankroll,
        )

        self.arb_strategy = ArbitrageStrategy(
            min_profit_bps=arb_cfg.get("min_profit_bps", ARB_MIN_PROFIT_BPS),
            max_position=arb_cfg.get("max_position", ARB_MAX_POSITION),
        )

        self.mm_strategy = MarketMakerStrategy(
            base_spread=mm_cfg.get("base_spread", MM_BASE_SPREAD),
            min_spread=mm_cfg.get("min_spread", MM_MIN_SPREAD),
            max_spread=mm_cfg.get("max_spread", MM_MAX_SPREAD),
            max_inventory=mm_cfg.get("max_inventory", MM_MAX_INVENTORY),
            quote_size=mm_cfg.get("quote_size", MM_QUOTE_SIZE),
            boundary_buffer=mm_cfg.get("boundary_buffer", MM_BOUNDARY_BUFFER),
            min_liquidity=mm_cfg.get("min_liquidity", strat_cfg.get("min_liquidity", DEFAULT_MIN_LIQUIDITY)),
        )

        self.coordinator = StrategyCoordinator(
            edge_strategy=self.edge_strategy,
            arb_strategy=self.arb_strategy,
            mm_strategy=self.mm_strategy,
            max_mm_markets=coord_cfg.get("max_mm_markets", MM_MAX_MM_MARKETS),
            max_mm_exposure=coord_cfg.get("max_mm_exposure", MM_MAX_MM_EXPOSURE),
            max_arb_exposure=coord_cfg.get("max_arb_exposure", ARB_MAX_ARB_EXPOSURE),
        )

        # Risk
        self.risk_manager = RiskManager(
            max_daily_loss=risk_cfg.get("max_daily_loss", 50.0),
            max_positions=risk_cfg.get("max_positions", 10),
            max_exposure=bankroll * risk_cfg.get("max_exposure_pct", 0.25),
            max_consecutive_losses=risk_cfg.get("max_consecutive_losses", 3),
            max_position_size=risk_cfg.get("max_position_size", DEFAULT_MAX_POSITION_SIZE),
        )

        self.portfolio = Portfolio()
        self.sizer = PositionSizer(
            bankroll=bankroll,
            kelly_mult=risk_cfg.get("kelly_fraction", DEFAULT_KELLY_FRACTION),
            max_position=risk_cfg.get("max_position_size", DEFAULT_MAX_POSITION_SIZE),
        )

        # Execution
        if mode == "paper":
            self.executor = PaperExecutor(initial_balance=bankroll)
        else:
            from execution.bridge_executor import BridgeExecutor
            bridge_url = self.settings.get("bridge", {}).get("url", "http://127.0.0.1:8420")
            self.executor = BridgeExecutor(bridge_url=bridge_url)

        # Calibration
        self.calibration = CalibrationTracker(storage=self.storage)

        # Notifications
        self.notifier = TelegramNotifier()

        # Timing
        self.full_scan_interval = scan_cfg.get("full_market_scan", FULL_SCAN_INTERVAL)
        self.price_poll_interval = scan_cfg.get("price_poll", PRICE_POLL_INTERVAL)
        self.heartbeat_interval = scan_cfg.get("heartbeat", HEARTBEAT_INTERVAL)
        self._last_full_scan: float = 0
        self._last_heartbeat: float = 0
        self._last_daily_reset: Optional[str] = None

        # WebSocket price feed
        ws_cfg = self.settings.get("websocket", {})
        self._ws_enabled = ws_cfg.get("enabled", True)
        self._ws_fallback_to_rest = ws_cfg.get("fallback_to_rest", True)
        self.ws_feed: Optional[WebSocketPriceFeed] = None
        if self._ws_enabled:
            self.ws_feed = WebSocketPriceFeed()

        # Position reconciliation (live mode only)
        recon_cfg = self.settings.get("reconciliation", {})
        self.reconciler = PositionReconciler(
            reconcile_interval=recon_cfg.get("interval", 300),
        )

        # Bridge health tracking (live mode only)
        self._bridge_degraded = False

        # News feed for LLM context
        self.news_feed = NewsFeed()
        self._news_cache: dict[str, tuple[float, list]] = {}  # market_id -> (timestamp, articles)
        self._news_cache_ttl = scan_cfg.get("news_poll", 900)  # 15 min

        # Watched markets (candidates for trading)
        self._watched_markets: list[str] = []

        # Track subscribed token IDs for WS
        self._ws_subscribed_tokens: set[str] = set()

    def _init_llm_model(self):
        """Add LLM model to ensemble if API key is available."""
        try:
            api_key = get_env("ANTHROPIC_API_KEY")
            if api_key:
                from models.llm_forecaster import LLMForecaster
                llm = LLMForecaster(provider="anthropic")
                self.ensemble.add_model(llm, weight=2.0)
                logger.info("LLM forecaster (Anthropic) added to ensemble")
                return

            api_key = get_env("OPENAI_API_KEY")
            if api_key:
                from models.llm_forecaster import LLMForecaster
                llm = LLMForecaster(
                    provider="openai",
                    screening_model="gpt-4o-mini",
                    final_model="gpt-4o",
                )
                self.ensemble.add_model(llm, weight=2.0)
                logger.info("LLM forecaster (OpenAI) added to ensemble")
                return

        except Exception as e:
            logger.warning(f"LLM model not available: {e}")

        logger.info("No LLM API key found — running with statistical models only")

    def run(self):
        """Main loop entry point."""
        self._running = True

        # Register signal handlers
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        logger.info("=" * 60)
        logger.info(f"Polymarket Bot starting — mode={self.mode}")
        logger.info(f"Bankroll: ${self.executor.get_balance():.2f}")
        logger.info(f"Models: {[m.name for m in self.ensemble.models]}")
        logger.info(f"Strategies: Edge (min_edge={self.edge_strategy.min_edge:.1%}), "
                     f"Arb (min_profit={self.arb_strategy.min_profit_bps}bps), "
                     f"MM (spread={self.mm_strategy.base_spread:.2f})")
        logger.info(f"Scan interval: {self.full_scan_interval}s full, {self.price_poll_interval}s price")
        logger.info("=" * 60)

        self.notifier.send(
            f"🤖 <b>Bot started</b> | Mode: {self.mode} | "
            f"Balance: ${self.executor.get_balance():.2f}"
        )

        # Start WebSocket price feed
        if self.ws_feed:
            self.ws_feed.start()
            logger.info("WebSocket price feed started")

        try:
            while self._running:
                loop_start = time.time()

                # Daily reset at 00:00 UTC
                self._check_daily_reset()

                # Bridge health check (live mode only)
                if self.mode == "live":
                    self._check_bridge_health()

                # Position reconciliation (live mode only)
                if self.mode == "live" and self.reconciler.should_reconcile():
                    self._run_reconciliation()

                # Full market scan (every 30 min)
                if time.time() - self._last_full_scan >= self.full_scan_interval:
                    self._full_scan()

                # Price update + strategy evaluation
                self._price_update_and_evaluate()

                # Manage existing positions (check exits)
                self._manage_positions()

                # Heartbeat
                if time.time() - self._last_heartbeat >= self.heartbeat_interval:
                    self._send_heartbeat()

                # Sleep until next cycle
                elapsed = time.time() - loop_start
                sleep_time = max(5, self.price_poll_interval - elapsed)
                self._interruptible_sleep(sleep_time)

        except Exception as e:
            logger.error(f"Fatal error in main loop: {e}", exc_info=True)
            self.notifier.send_error(f"Fatal error: {e}")
        finally:
            self._shutdown()

    def _full_scan(self):
        """Discover and filter markets from the Gamma API."""
        logger.info("Running full market scan...")
        try:
            markets = self.scanner.scan(
                limit=100,
                min_volume_24h=float(self.settings.get("filters", {}).get("min_volume_24h", 100)),
                sort_by="volume24hr",
            )

            self.cache.add_markets(markets)
            self._watched_markets = [m.condition_id for m in markets]

            # Persist discovered markets
            for m in markets:
                self.storage.upsert_market(
                    condition_id=m.condition_id,
                    question=m.question,
                    category=m.category,
                    end_date=m.end_date.isoformat() if m.end_date else None,
                    yes_token_id=m.yes_token_id,
                    no_token_id=m.no_token_id,
                    volume=m.volume,
                    liquidity=m.liquidity,
                    last_price_yes=m.last_price_yes,
                    last_price_no=m.last_price_no,
                )

            # Subscribe new tokens to WebSocket
            if self.ws_feed:
                new_tokens = set()
                for m in markets:
                    if m.yes_token_id:
                        new_tokens.add(m.yes_token_id)
                    if m.no_token_id:
                        new_tokens.add(m.no_token_id)
                to_subscribe = new_tokens - self._ws_subscribed_tokens
                if to_subscribe:
                    self.ws_feed.subscribe(list(to_subscribe))
                    self._ws_subscribed_tokens.update(to_subscribe)
                    logger.info(f"WS subscribed to {len(to_subscribe)} new tokens")

            self._last_full_scan = time.time()
            logger.info(f"Scan complete: {len(markets)} markets watched")

        except Exception as e:
            logger.error(f"Full scan error: {e}")

    def _build_context(self, market: Market) -> dict:
        """Build context dict for strategy evaluation."""
        context = {}

        # Fetch order books
        try:
            if market.yes_token_id:
                book_yes = self.clob.get_order_book(market.yes_token_id)
                if book_yes:
                    context["book_yes"] = book_yes
            if market.no_token_id:
                book_no = self.clob.get_order_book(market.no_token_id)
                if book_no:
                    context["book_no"] = book_no
        except Exception:
            pass

        # Inventory from portfolio
        inventory_yes = 0
        inventory_no = 0
        for pos in self.portfolio.positions:
            if pos.market_id == market.condition_id:
                if pos.outcome == Outcome.YES:
                    inventory_yes += pos.size
                else:
                    inventory_no += pos.size
        context["inventory_yes"] = inventory_yes
        context["inventory_no"] = inventory_no

        # Active orders
        context["active_orders"] = self.executor.get_open_orders(market.condition_id)

        # Fetch news (with cache per market)
        try:
            cached = self._news_cache.get(market.condition_id)
            if cached and time.time() - cached[0] < self._news_cache_ttl:
                articles = cached[1]
            else:
                articles = self.news_feed.search_market_news(market.question, max_items=5)
                self._news_cache[market.condition_id] = (time.time(), articles)
            if articles:
                context["news"] = articles
        except Exception:
            pass

        # Market metadata for LLM
        context["additional_context"] = (
            f"Volume: ${market.volume:,.0f} | "
            f"Liquidity: ${market.liquidity:,.0f} | "
            f"Spread: {market.spread_yes:.1%}"
        )

        return context

    def _price_update_and_evaluate(self):
        """Update prices and evaluate strategies on watched markets."""
        if not self.risk_manager.is_trading_allowed:
            return

        # Drain WebSocket price updates first
        ws_prices: dict[str, float] = {}
        if self.ws_feed:
            ws_prices = self.ws_feed.drain_updates()
            if ws_prices:
                for token_id, price in ws_prices.items():
                    self.cache.update_price(token_id, price)

        for cid in self._watched_markets:
            market = self.cache.get_market(cid)
            if not market:
                continue

            # Feed price to MM for historical volatility tracking
            self.mm_strategy.record_price(cid, market.last_price_yes)

            # Fall back to REST if WS didn't provide an update for this market
            ws_had_update = (
                market.yes_token_id in ws_prices or
                market.no_token_id in ws_prices
            )
            if not ws_had_update or not self._ws_enabled:
                try:
                    if market.yes_token_id:
                        mid = self.clob.get_midpoint(market.yes_token_id)
                        if mid is not None:
                            self.cache.update_price(market.yes_token_id, mid)
                except Exception:
                    pass

            # Check limit order fills in paper mode
            if isinstance(self.executor, PaperExecutor) and market.yes_token_id:
                try:
                    fills = self.executor.check_limit_fills(
                        market.yes_token_id, market.last_price_yes
                    )
                    for fill in fills:
                        logger.info(f"MM FILL: {fill.side.value} {fill.outcome.value} @ ${fill.price:.4f}")
                except Exception:
                    pass

            # Skip evaluation if we already have an edge position
            if self.portfolio.has_position(cid):
                continue

            # Evaluate all strategies via coordinator
            try:
                context = self._build_context(market)
                signals = self.coordinator.evaluate_market(market, context)
                for signal in signals:
                    if signal.strategy == StrategyType.MARKET_MAKING:
                        self._execute_mm_signal(signal, market)
                    else:
                        self._execute_signal(signal, market)
            except Exception as e:
                logger.error(f"Strategy evaluation error for {cid[:20]}: {e}")

    def _execute_signal(self, signal: Signal, market: Market):
        """Execute a trading signal after risk checks."""
        # Risk check
        allowed, reason = self.risk_manager.check_trade(signal, market)
        if not allowed:
            logger.info(f"Trade blocked by risk: {reason}")
            return

        # Get token ID
        token_id = market.yes_token_id if signal.outcome == Outcome.YES else market.no_token_id

        # Size with position sizer (may reduce from signal.size)
        remaining = self.portfolio.remaining_exposure(self.risk_manager.max_exposure)
        sized = self.sizer.size(
            true_prob=signal.metadata.get("model_prob", 0.5),
            market_price=signal.price,
            remaining_exposure=remaining,
        )
        if sized < 1.0:
            return

        # Execute
        try:
            shares = sized / signal.price
            result = self.executor.buy(
                market_id=market.condition_id,
                token_id=token_id,
                outcome=signal.outcome,
                price=signal.price,
                size=shares,
            )

            # Track in portfolio
            positions = self.executor.get_positions()
            for pos in positions:
                if pos.market_id == market.condition_id and pos.outcome == signal.outcome:
                    self.portfolio.add_position(pos, market)
                    break

            # Track in risk manager
            self.risk_manager.record_trade_open(sized, market.category or "other")

            # Track in coordinator
            if signal.strategy == StrategyType.EDGE:
                self.coordinator.record_edge_entry(market.condition_id)
            elif signal.strategy == StrategyType.ARBITRAGE:
                self.coordinator.record_arb_entry(market.condition_id, sized)

            # Record in storage
            self.storage.record_trade(
                market_id=market.condition_id,
                outcome=signal.outcome.value,
                side="BUY",
                price=result.price,
                size=result.size,
                cost=result.cost,
                fee=result.fee,
                order_id=result.order_id,
                strategy=signal.strategy.value,
                paper=result.paper,
            )

            # Record prediction for calibration
            model_prob = signal.metadata.get("model_prob", 0.5)
            self.calibration.record_prediction(
                model_name=signal.metadata.get("model_name", "ensemble"),
                market_id=market.condition_id,
                predicted_prob=model_prob,
            )

            # Notify
            self.notifier.send_entry(
                market_question=market.question,
                outcome=signal.outcome.value,
                price=result.price,
                size=sized,
                edge=signal.edge,
                strategy=signal.strategy.value,
            )

            logger.info(
                f"ENTRY: {signal.outcome.value} {market.question[:40]}... "
                f"@ ${result.price:.4f} x {result.size:.1f} "
                f"(${sized:.2f}, edge={signal.edge:.1%})"
            )

        except Exception as e:
            logger.error(f"Execution error: {e}")
            self.notifier.send_error(f"Execution failed: {e}")

    def _execute_mm_signal(self, signal: Signal, market: Market):
        """Execute a market making signal by placing a limit order."""
        token_id = market.yes_token_id if signal.outcome == Outcome.YES else market.no_token_id
        side = Side.BUY if signal.action == SignalAction.BUY else Side.SELL

        try:
            order_id = self.executor.place_limit_order(
                market_id=market.condition_id,
                token_id=token_id,
                outcome=signal.outcome,
                side=side,
                price=signal.price,
                size=signal.size / signal.price if signal.action == SignalAction.BUY else signal.size,
                strategy=StrategyType.MARKET_MAKING,
            )

            if order_id:
                order = OpenOrder(
                    order_id=order_id,
                    market_id=market.condition_id,
                    token_id=token_id,
                    outcome=signal.outcome,
                    side=side,
                    price=signal.price,
                    size=signal.size,
                    strategy=StrategyType.MARKET_MAKING,
                )
                self.coordinator.record_order_placed(order)

                logger.info(
                    f"MM ORDER: {side.value} {signal.outcome.value} "
                    f"{market.question[:30]}... @ ${signal.price:.4f}"
                )

        except Exception as e:
            logger.error(f"MM execution error: {e}")

    def _manage_positions(self):
        """Check exit conditions on all open positions."""
        for pos in list(self.portfolio.positions):
            market = self.cache.get_market(pos.market_id)
            if not market:
                continue

            # Update current price
            try:
                if pos.token_id:
                    mid = self.clob.get_midpoint(pos.token_id)
                    if mid is not None:
                        pos.update_pnl(mid)
                        self.cache.update_price(pos.token_id, mid)
            except Exception:
                pass

            # Check for market resolution
            if market.resolution:
                self._handle_resolution(pos, market)
                continue

            # Check exit conditions
            exit_signal = self.edge_strategy.check_exit(market, pos)
            if exit_signal:
                self._execute_exit(exit_signal, pos, market)

    def _execute_exit(self, signal: Signal, pos: Position, market: Market):
        """Execute a position exit."""
        try:
            result = self.executor.sell(
                market_id=pos.market_id,
                token_id=pos.token_id,
                outcome=pos.outcome,
                price=signal.price,
                size=pos.size,
            )

            pnl = (result.price - pos.entry_price) * pos.size

            # Update tracking
            self.portfolio.remove_position(pos.market_id, pos.outcome)
            self.risk_manager.record_trade_close(
                size=pos.cost_basis,
                pnl=pnl,
                category=market.category or "other",
            )

            # Track in coordinator
            if pos.strategy == StrategyType.EDGE:
                self.coordinator.record_edge_exit(pos.market_id)
            elif pos.strategy == StrategyType.ARBITRAGE:
                self.coordinator.record_arb_exit(pos.market_id, pos.cost_basis)
            elif pos.strategy == StrategyType.MARKET_MAKING:
                self.coordinator.record_mm_exit(pos.market_id, pos.cost_basis)

            # Record in storage
            exit_reason = signal.metadata.get("exit_reason", "unknown")
            self.storage.record_trade(
                market_id=pos.market_id,
                outcome=pos.outcome.value,
                side="SELL",
                price=result.price,
                size=result.size,
                cost=result.cost,
                fee=result.fee,
                order_id=result.order_id,
                strategy=StrategyType.EDGE.value,
                exit_reason=exit_reason,
                paper=result.paper,
            )

            # Notify
            self.notifier.send_exit(
                market_question=market.question,
                outcome=pos.outcome.value,
                entry_price=pos.entry_price,
                exit_price=result.price,
                pnl=pnl,
                reason=exit_reason,
            )

            logger.info(
                f"EXIT: {pos.outcome.value} {market.question[:40]}... "
                f"entry=${pos.entry_price:.4f} exit=${result.price:.4f} "
                f"P/L=${pnl:.2f} ({exit_reason})"
            )

        except Exception as e:
            logger.error(f"Exit execution error: {e}")

    def _handle_resolution(self, pos: Position, market: Market):
        """Handle market resolution — payout or loss."""
        if isinstance(self.executor, PaperExecutor):
            self.executor.resolve_position(pos.market_id, pos.outcome, market.resolution)

        # Score models
        actual = 1 if market.resolution == "YES" else 0
        scores = self.calibration.score_resolution(pos.market_id, actual)
        for model_name, brier in scores.items():
            self.ensemble.update_weights_from_brier(model_name, brier)

        pnl = pos.size if pos.outcome.value == market.resolution else -pos.cost_basis

        self.portfolio.remove_position(pos.market_id, pos.outcome)
        self.risk_manager.record_trade_close(pos.cost_basis, pnl, market.category or "other")

        self.notifier.send_exit(
            market_question=market.question,
            outcome=pos.outcome.value,
            entry_price=pos.entry_price,
            exit_price=1.0 if pos.outcome.value == market.resolution else 0.0,
            pnl=pnl,
            reason="resolution",
        )

        logger.info(f"RESOLVED: {market.question[:40]}... -> {market.resolution} (P/L=${pnl:.2f})")

    def _check_bridge_health(self):
        """Check bridge health and enter/exit degraded mode (live mode only)."""
        from execution.bridge_executor import BridgeExecutor
        if not isinstance(self.executor, BridgeExecutor):
            return

        was_degraded = self._bridge_degraded

        if self.executor.health_ok:
            if was_degraded:
                # Bridge recovered
                self._bridge_degraded = False
                self.coordinator.degraded_mode = False
                logger.info("Bridge recovered — resuming full mode")
                self.notifier.send("Bridge recovered — full mode restored")
        else:
            if not was_degraded:
                # Bridge went down — enter degraded mode
                self._bridge_degraded = True
                self.coordinator.degraded_mode = True

                # Cancel all MM quotes (can't manage them without bridge)
                cancelled = self.executor.cancel_all_orders()
                logger.warning(
                    f"Bridge offline — degraded mode (cancelled {cancelled} orders)"
                )
                self.notifier.send_error(
                    "Bridge offline — degraded mode. MM/arb blocked, edge exits only."
                )

    def _run_reconciliation(self):
        """Run position reconciliation against bridge (live mode only)."""
        from execution.bridge_executor import BridgeExecutor
        if not isinstance(self.executor, BridgeExecutor):
            return

        try:
            bridge_positions = self.executor.get_positions()
            local_positions = self.portfolio.positions

            result = self.reconciler.reconcile(local_positions, bridge_positions)

            if result.has_mismatches:
                # Bridge is source of truth for size mismatches
                for mm in result.size_mismatches:
                    pos = self.portfolio.get_position(mm.market_id, mm.outcome)
                    if pos:
                        pos.size = mm.bridge_size
                        logger.info(
                            f"Reconciled {mm.market_id}:{mm.outcome.value} "
                            f"size {mm.local_size:.2f} -> {mm.bridge_size:.2f}"
                        )

                # Bridge-only positions: add to portfolio
                for mm in result.bridge_only:
                    for bp in bridge_positions:
                        if bp.market_id == mm.market_id and bp.outcome == mm.outcome:
                            self.portfolio.add_position(bp)
                            logger.info(
                                f"Added bridge-only position {mm.market_id}:{mm.outcome.value}"
                            )

                # Local-only: mark stale (log warning, don't auto-remove)
                for mm in result.local_only:
                    logger.warning(
                        f"Stale local position {mm.market_id}:{mm.outcome.value} "
                        f"(size={mm.local_size:.2f}, not on bridge)"
                    )

                self.notifier.send(
                    f"Position reconciliation: {result.summary()}"
                )

        except Exception as e:
            logger.error(f"Reconciliation error: {e}")

    def _check_daily_reset(self):
        """Reset daily counters at 00:00 UTC."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._last_daily_reset != today:
            if self._last_daily_reset is not None:
                # Send daily summary before reset
                self._send_daily_summary()
            self.risk_manager.reset_daily()
            if isinstance(self.executor, PaperExecutor):
                self.executor.reset_daily()
            self.storage.cleanup_old_data()
            self._last_daily_reset = today
            logger.info(f"Daily reset complete: {today}")

    def _send_heartbeat(self):
        """Send periodic heartbeat alert."""
        summary = self._build_summary()
        self.notifier.send_heartbeat(summary)
        self._last_heartbeat = time.time()

    def _send_daily_summary(self):
        """Send end-of-day summary."""
        summary = self._build_summary()
        self.notifier.send_daily_summary(summary)

        # Record daily P/L
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.storage.record_daily_pnl(
            date=today,
            strategy="edge",
            pnl=summary.get("daily_pnl", 0),
            trades=summary.get("daily_trades", 0),
        )

    def _build_summary(self) -> dict:
        """Build combined summary from all components."""
        exe_summary = self.executor.summary() if isinstance(self.executor, PaperExecutor) else {}
        risk_summary = self.risk_manager.summary()
        port_summary = self.portfolio.summary()
        coord_summary = self.coordinator.summary()

        summary = {
            "balance": exe_summary.get("balance", 0),
            "daily_pnl": exe_summary.get("daily_pnl", risk_summary.get("daily_pnl", 0)),
            "daily_trades": exe_summary.get("daily_trades", risk_summary.get("daily_trades", 0)),
            "total_pnl": exe_summary.get("total_pnl", 0),
            "open_positions": port_summary.get("positions", 0),
            "open_exposure": port_summary.get("total_exposure", 0),
            "unrealized_pnl": port_summary.get("total_unrealized_pnl", 0),
            "consecutive_losses": risk_summary.get("consecutive_losses", 0),
            "trading_allowed": risk_summary.get("trading_allowed", True),
            "watched_markets": len(self._watched_markets),
            "cache_markets": self.cache.market_count,
            "ensemble_models": len(self.ensemble.models),
            "edge_markets": coord_summary.get("edge_markets", 0),
            "arb_markets": coord_summary.get("arb_markets", 0),
            "mm_markets": coord_summary.get("mm_markets", 0),
            "mm_exposure": coord_summary.get("mm_exposure", 0),
            "arb_exposure": coord_summary.get("arb_exposure", 0),
            "open_orders": coord_summary.get("open_orders", 0),
            "ws_connected": self.ws_feed.connected if self.ws_feed else False,
            "ws_subscriptions": self.ws_feed.subscription_count if self.ws_feed else 0,
            "bridge_degraded": self._bridge_degraded,
        }
        return summary

    def _interruptible_sleep(self, seconds: float):
        """Sleep that can be interrupted by shutdown signal."""
        self._shutdown_event.wait(timeout=seconds)

    def _handle_shutdown(self, signum, frame):
        """Handle SIGINT/SIGTERM gracefully."""
        logger.info(f"Shutdown signal received ({signum})")
        self._running = False
        self._shutdown_event.set()

    def _shutdown(self):
        """Clean shutdown — close positions summary, save state."""
        logger.info("Shutting down...")

        # Stop WebSocket feed
        if self.ws_feed:
            self.ws_feed.stop()

        # Final summary
        self._send_daily_summary()

        # Close storage
        self.storage.close()

        self.notifier.send(
            f"🛑 <b>Bot stopped</b> | "
            f"Positions: {self.portfolio.position_count} | "
            f"P/L: ${self.risk_manager._daily_pnl:.2f}"
        )

        logger.info("Shutdown complete")


def main():
    parser = argparse.ArgumentParser(description="Polymarket Trading Bot")
    parser.add_argument("--paper", action="store_true", help="Paper trading mode (default)")
    parser.add_argument("--live", action="store_true", help="Live trading mode")
    parser.add_argument("--bankroll", type=float, default=None, help="Starting bankroll in USDC (default: from settings)")
    parser.add_argument("--min-edge", type=float, default=None, help="Override min edge (e.g., 0.05)")
    parser.add_argument("--log-level", default="INFO", help="Logging level")
    args = parser.parse_args()

    # Setup logging
    os.makedirs("logs", exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("logs/polymarket.log", mode="a"),
        ],
    )

    mode = "live" if args.live else "paper"
    settings = load_settings()

    if args.min_edge is not None:
        settings.setdefault("strategy", {})["min_edge"] = args.min_edge

    bankroll = args.bankroll or settings.get("risk", {}).get("bankroll", 10000.0)

    trader = LiveTrader(
        mode=mode,
        bankroll=bankroll,
        settings=settings,
    )

    trader.run()


if __name__ == "__main__":
    main()
