# Project Instructions

## On Session Start
Run the health check script at the beginning of each session:
```
python health_check.py
```

## Project Overview
Polymarket prediction market trading bot supporting edge-based, market-making, arbitrage, and crypto scalper strategies. Paper mode first, then live.

## Architecture
- **Python**: Strategy, risk, data, trade lifecycle logic
- **TypeScript bridge**: Express server (`http://127.0.0.1:8420`) wrapping `@polymarket/clob-client` for order execution and wallet auth

## Deployment
- **Droplet**: `107.170.74.154`
- **Edge strategy**: `systemctl status polymarket-bot` (`/opt/polymarket-bot`)
- **Crypto scalper**: `systemctl status crypto-scalper` (`/opt/polymarket-bot`)
- **Repo**: `prashanth116-ui/polymarket-bot`

## Key Commands

### Health Check
```bash
python health_check.py
```

### TypeScript Bridge
```bash
cd bridge && npm install && npm run dev
```

### Tests
```bash
python -m pytest tests/ -v
```

### Market Monitor
```bash
# Top markets by volume
python -m runners.market_monitor

# Search for specific markets
python -m runners.market_monitor --search "trump"

# Trending markets
python -m runners.market_monitor --trending --top 10

# Wide-spread markets (MM opportunities)
python -m runners.market_monitor --spreads

# Markets resolving soon
python -m runners.market_monitor --near

# Show order book for a token
python -m runners.market_monitor --book TOKEN_ID
```

### Paper Trading (Edge Strategy)
```bash
# 24/7 wrapper with auto-restart
python run_paper_trading.py

# Direct live loop (paper mode)
python -m runners.run_live --paper --bankroll 1000

# Direct live loop (live mode — real money!)
python -m runners.run_live --live --bankroll 500

# Override min edge
python -m runners.run_live --paper --min-edge 0.08
```

### Crypto Scalper V3 (Contrarian)
```bash
# Paper mode (default)
python -m runners.run_crypto --paper

# Custom parameters
python -m runners.run_crypto --paper --asset btc --interval 15 --min-streak 2

# Override position size / bankroll
python -m runners.run_crypto --paper --position-size 30 --bankroll 300

# Backtests (strategy discovery)
python -m runners.backtest_crypto --days 7
python -m runners.backtest_crypto_trend --days 7
```

## Risk Management

### Risk Manager Checks (all must pass)
1. Kill switch not active
2. Circuit breaker not tripped
3. Daily loss < $50 (configurable)
4. Consecutive losses < 3
5. Open positions < 10
6. Total exposure < max
7. Per-category exposure < $200
8. Position size < $100
9. Time to resolution > 24h

### Position Sizing
- Quarter Kelly (25% Kelly fraction)
- Max position: $100
- Max bankroll per trade: 10%
- Min trade size: $1
- Scaled down near exposure limits

### Portfolio Tracking
- Exposure by category (politics, crypto, sports, etc.)
- Exposure by strategy (edge, MM, arb)
- Correlated market detection (same category or keyword overlap)
- Remaining exposure caps

## Crypto Scalper V3 — Contrarian Mean-Reversion

### How It Works
Polymarket has ultra-short-term BTC up/down markets that resolve every 15 minutes based on whether BTC goes up or down. The crypto scalper trades these markets using a contrarian (mean-reversion) strategy.

**Core insight**: BTC 15-min windows mean-revert, not trend-follow. After consecutive same-direction resolutions, the probability of reversal increases significantly.

### Backtest Results (7 days, 673 windows)
| Streak Length | Reversal Rate | Win Rate | Total P/L | Max DD | Stat Sig |
|--------------|---------------|----------|-----------|--------|----------|
| After 2 same | 54.9% | 55% | +$8,599 | $413 | p=0.070 |
| After 3 same | 57.4% | 57% | +$4,570 | $249 | p=0.035 |
| After 4 same | 68.3% | 68% | +$1,597 | $117 | p=0.002 |

### Strategy Logic
1. **Track resolution history**: Record whether each 15-min window resolved UP or DOWN (persisted to `logs/crypto_history.json` across restarts, keeps last 20 windows)
2. **Detect streaks**: Count consecutive same-direction resolutions (e.g., 3x UP in a row)
3. **Bet contrarian**: After `min_streak` (default 2) consecutive same-direction resolutions, bet on reversal (e.g., after 3x UP streak, buy the DOWN token)
4. **Enter at T-300s**: Enter 5 minutes before window close when token prices are near $0.50 for balanced risk:reward
5. **Price filter**: Only buy tokens priced $0.05-$0.55 (avoids worthless tokens and negative-edge entries where fees eat profit)
6. **Fee-adjusted edge**: Dynamic fee = `0.25 * (p * (1-p))^2`. Edge must be positive after fees.
7. **Scale with conviction**: Position size scales with streak length (streak=2: 1x, streak=3: 1.5x, streak=4+: 2x base size, capped at 2x)

### Why Contrarian Beats Momentum
The V1/V2 momentum strategy had a fundamental timing paradox:
- **Strong momentum** (clear BTC direction) = high accuracy BUT token prices at $0.90+ = terrible risk:reward (risk $40 to make $2, EV = -$1.29/trade)
- **Weak momentum** (ambiguous direction) = token prices near $0.50 = good risk:reward BUT coin-flip accuracy

Contrarian avoids this entirely — it enters at T-300s when prices are near $0.50, and the edge comes from statistical mean-reversion, not real-time price prediction.

### V3 Changes from V2
- Replaced momentum-based signals with contrarian streak detection
- Enter at T-300s instead of T-60s (better prices near $0.50)
- Spot feed kept for monitoring/logging only (not for signal generation)
- Track and persist resolution history across restarts
- Position sizing scales with streak length (longer streak = higher confidence)

### Risk Controls
| Control | Default | Description |
|---------|---------|-------------|
| Max consecutive losses | 3 | Pause 1 hour after 3 consecutive losses |
| Max daily loss | $50 | Stop trading for the day |
| Max trades/hour | 4 | Safety cap (one per window) |
| Price range | $0.05-$0.55 | Avoid worthless and overpriced tokens |
| Bankroll | $200 | Separate from edge strategy |
| Position size | $20 base | Scaled 1x-2x by streak length |

### Entry/Exit Flow
```
1. Sleep until T-300s (5 min before window close)
2. Check risk controls (consec losses, daily loss, hourly limit)
3. Check streak from resolution history
4. If streak >= min_streak:
   a. Discover market via Gamma API (slug: btc-updown-15m-{timestamp})
   b. Fetch Up/Down token prices from CLOB midpoint
   c. Evaluate: bet AGAINST streak direction
   d. Check price range, fee-adjusted edge
   e. Execute paper trade via PaperExecutor
5. Wait for window close + 5s
6. Poll Gamma API outcomePrices for resolution (up to 120s)
7. Record P/L, update streak history, send Telegram alert
8. Sleep until next window's entry zone
```

### Market Discovery
- **Slug format**: `btc-updown-15m-{unix_timestamp}` (timestamp floored to 15-min boundary)
- **API**: `GET gamma-api.polymarket.com/events?slug={slug}`
- **Token mapping**: Outcomes "Up"/"Down" mapped to CLOB token IDs
- **Resolution**: `outcomePrices` field — `["1","0"]` = UP won, `["0","1"]` = DOWN won

### Config (`config/settings.yaml`)
```yaml
crypto_scalper:
  enabled: false
  asset: btc
  interval_minutes: 15
  bankroll: 200
  position_size: 20
  min_streak: 2
  min_entry_price: 0.05
  max_entry_price: 0.55
  entry_window_secs: 300
  max_trades_per_hour: 4
  max_consec_losses: 3
  max_daily_loss: 50
```

### Deployment
Runs as a separate systemd service on the droplet:
```bash
# On droplet (107.170.74.154)
sudo systemctl status crypto-scalper
sudo systemctl restart crypto-scalper
journalctl -u crypto-scalper -f
tail -f /opt/polymarket-bot/logs/crypto_scalper.log
```

### Key Files
| File | Purpose |
|------|---------|
| `strategies/crypto_scalper.py` | Signal logic — streak evaluation, edge calculation, position sizing |
| `runners/run_crypto.py` | Main loop — window timing, market discovery, streak tracking, resolution |
| `data/spot_feed.py` | Coinbase WebSocket for real-time BTC/USD (monitoring only) |
| `core/constants.py` | Crypto scalper defaults (position size, streak, entry window, etc.) |
| `tests/test_crypto_scalper.py` | 18 tests (contrarian signals, sizing, fees, slugs) |
| `runners/backtest_crypto.py` | V2 momentum backtest (historical reference) |
| `runners/backtest_crypto_trend.py` | V3 multi-strategy backtest (discovered contrarian edge) |
| `logs/crypto_history.json` | Persisted resolution history (last 20 windows) |
| `logs/crypto_windows.csv` | Per-window trade log |

## Key Files (Edge Strategy)

| File | Purpose |
|------|---------|
| `core/types.py` | Market, Signal, Position, TradeResult types |
| `core/constants.py` | API URLs, chain ID, fee tiers, risk defaults |
| `core/kelly.py` | Kelly criterion for binary outcome sizing |
| `config/loader.py` | YAML/JSON config loader |
| `config/settings.yaml` | Global settings (mode, scan, risk limits) |
| `execution/executor_interface.py` | Abstract executor base class |
| `execution/paper_executor.py` | Simulated fills with slippage model |
| `execution/bridge_executor.py` | HTTP calls to TypeScript bridge |
| `data/market_scanner.py` | Gamma API market discovery + filtering |
| `data/clob_client.py` | CLOB REST reader (prices, books, spreads) |
| `data/websocket_client.py` | Async WebSocket for real-time prices |
| `data/market_cache.py` | Thread-safe in-memory market state |
| `data/storage.py` | SQLite persistence (markets, trades, P/L, scores) |
| `data/sources/news_feed.py` | NewsAPI + RSS news aggregation |
| `data/sources/economic_data.py` | FRED API economic indicators |
| `data/sources/polls.py` | RCP/538 polling data |
| `models/base.py` | Abstract ProbabilityModel base class |
| `models/llm_forecaster.py` | LLM forecaster (Claude/GPT-4, 2-tier with caching) |
| `models/statistical.py` | MarketImplied, BaseRate, TimeDecay models |
| `models/ensemble.py` | Weighted multi-model aggregator (Brier-based weights) |
| `models/calibration.py` | Brier score tracking + calibration curves |
| `strategies/base.py` | Abstract Strategy base class |
| `strategies/edge_strategy.py` | Edge strategy (buy when model > market + min_edge) |
| `risk/risk_manager.py` | Circuit breaker, daily loss, consecutive loss, kill switch |
| `risk/position_sizer.py` | Kelly-based sizing with caps |
| `risk/portfolio.py` | Portfolio exposure tracking + correlated market detection |
| `runners/run_live.py` | Main 24/7 trading loop (paper or live) |
| `runners/notifier.py` | Telegram alerts |
| `runners/market_monitor.py` | CLI market watcher tool |
| `bridge/src/index.ts` | Express server (port 8420) |
| `health_check.py` | Import, config, API connectivity checks |
| `run_paper_trading.py` | 24/7 wrapper with auto-restart |

## Strategy Pipeline
```
Market Scanner → Filter by volume/liquidity → Ensemble Model → Edge Detection → Signal
                                                  ↓
                                    MarketImplied + BaseRate + TimeDecay + LLM
                                                  ↓
                                    Weighted average (Brier-based weights)
                                                  ↓
                                    Edge = model_prob - market_price
                                    If edge >= 5%: generate Signal with Kelly sizing
```

### LLM Forecaster Tiers
| Tier | Model | Use Case | Cache |
|------|-------|----------|-------|
| Screening | Haiku/GPT-4o-mini | Quick filter — skip no-edge markets | 1 hour |
| Final | Sonnet/GPT-4 | Detailed analysis for edge candidates | 1 hour |

### Edge Strategy Exit Conditions
1. **Edge gone**: model estimate moved or market moved to our price (edge < 2%)
2. **Stop loss**: position down > 30%
3. **Take profit**: position up > 50%
4. **Near resolution**: < 6h to resolution with uncertain price (25-75%)

## Environment Variables
Set in `config/.env` (gitignored):
- `POLY_PRIVATE_KEY` — Polymarket wallet private key
- `TELEGRAM_BOT_TOKEN` — Telegram bot token
- `TELEGRAM_CHAT_ID` — Telegram chat ID
- `ANTHROPIC_API_KEY` — Claude API key
- `OPENAI_API_KEY` — OpenAI API key
