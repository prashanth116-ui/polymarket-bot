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

### Paper Trading
```bash
python run_paper_trading.py
```

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
| `runners/notifier.py` | Telegram alerts |
| `bridge/src/index.ts` | Express server (port 8420) |
| `health_check.py` | Import, config, API connectivity checks |
| `run_paper_trading.py` | 24/7 wrapper with auto-restart |

## Environment Variables
Set in `config/.env` (gitignored):
- `POLY_PRIVATE_KEY` — Polymarket wallet private key
- `TELEGRAM_BOT_TOKEN` — Telegram bot token
- `TELEGRAM_CHAT_ID` — Telegram chat ID
- `ANTHROPIC_API_KEY` — Claude API key
- `OPENAI_API_KEY` — OpenAI API key
