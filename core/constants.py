"""API URLs and constants for Polymarket."""

# Polymarket API endpoints
GAMMA_API_URL = "https://gamma-api.polymarket.com"
CLOB_API_URL = "https://clob.polymarket.com"
DATA_API_URL = "https://data-api.polymarket.com"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Polygon chain
CHAIN_ID = 137

# USDC on Polygon
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Polymarket CTF Exchange
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

# Fee tiers (maker/taker in basis points)
MAKER_FEE_BPS = 0  # Makers earn rebates
TAKER_FEE_BPS = 200  # 2% taker fee

# Bridge server
DEFAULT_BRIDGE_URL = "http://127.0.0.1:8420"

# Scan intervals (seconds)
FULL_SCAN_INTERVAL = 1800  # 30 min
PRICE_POLL_INTERVAL = 60  # 1 min
NEWS_POLL_INTERVAL = 900  # 15 min
HEARTBEAT_INTERVAL = 3600  # 1 hour

# Risk defaults
DEFAULT_MAX_POSITION_SIZE = 200.0  # USDC
DEFAULT_MAX_POSITIONS = 10
DEFAULT_MAX_DAILY_LOSS = 500.0  # USDC
DEFAULT_MAX_EXPOSURE_PCT = 0.50  # 50% of bankroll
DEFAULT_KELLY_FRACTION = 0.25  # Quarter Kelly
DEFAULT_MIN_EDGE = 0.07  # 7% — ~3% net margin after 4% round-trip fees
DEFAULT_MIN_CONFIDENCE = 0.65
DEFAULT_MIN_LIQUIDITY = 1000.0  # USDC — thin markets have noisier prices

# Market filters
MIN_VOLUME_24H = 100.0  # USDC
MIN_HOURS_TO_RESOLUTION = 24  # Don't trade markets resolving within 24h
MAX_PRICE_FOR_BUY = 0.95  # Don't buy above 95 cents
MIN_PRICE_FOR_BUY = 0.05  # Don't buy below 5 cents

# Market Making defaults
MM_MIN_SPREAD = 0.04  # Minimum spread to quote
MM_BASE_SPREAD = 0.06  # Base spread before adjustments
MM_MAX_SPREAD = 0.15  # Maximum spread
MM_MAX_INVENTORY = 200  # Max shares per side
MM_QUOTE_SIZE = 20  # Shares per quote
MM_BOUNDARY_BUFFER = 0.08  # Widen spread near 0/1
MM_MAX_MM_MARKETS = 10  # Max simultaneous MM markets
MM_MAX_MM_EXPOSURE = 500.0  # USDC total MM exposure

# Arbitrage defaults
ARB_MIN_PROFIT_BPS = 50  # Min profit in basis points (0.5%)
ARB_MAX_POSITION = 100  # Max USDC per arb
ARB_MAX_ARB_EXPOSURE = 200.0  # USDC total arb exposure

# Binance WebSocket (Binance.US — required for US-based servers)
BINANCE_WS_URL = "wss://stream.binance.us:9443/ws"

# Crypto scalper defaults (V3 — contrarian)
CRYPTO_DEFAULT_POSITION_SIZE = 20.0  # USDC per trade (base — scaled by streak length)
CRYPTO_DEFAULT_INTERVAL_MINS = 15
CRYPTO_DEFAULT_MIN_STREAK = 2  # Min consecutive same-direction windows before contrarian entry
CRYPTO_DEFAULT_ENTRY_WINDOW_SECS = 300  # Enter 5 min before close (T-300s) for better prices
CRYPTO_DEFAULT_MIN_ENTRY_PRICE = 0.05  # Don't buy tokens market thinks are worthless
CRYPTO_DEFAULT_MAX_ENTRY_PRICE = 0.55  # Don't buy above 55¢ (fees eat edge)
CRYPTO_DEFAULT_BANKROLL = 200.0  # Separate bankroll from edge strategy
CRYPTO_DEFAULT_MAX_CONSEC_LOSSES = 3  # Stop trading after 3 consecutive losses
CRYPTO_DEFAULT_MAX_DAILY_LOSS = 50.0  # Max daily loss in USDC before stopping
