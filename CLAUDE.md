# Project Instructions

## On Session Start
Run the health check script at the beginning of each session:
```
python health_check.py
```

## Project Overview
Polymarket prediction market trading bot supporting edge-based, market-making, and arbitrage strategies. Paper mode first, then live.

## Architecture
- **Python**: Strategy, risk, data, trade lifecycle logic
- **TypeScript bridge**: Express server (`http://127.0.0.1:8420`) wrapping `@polymarket/clob-client` for order execution and wallet auth

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

### Paper Trading
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

## Key Files

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
