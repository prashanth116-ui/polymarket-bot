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
DEFAULT_MAX_POSITION_SIZE = 100.0  # USDC
DEFAULT_MAX_POSITIONS = 10
DEFAULT_MAX_DAILY_LOSS = 50.0  # USDC
DEFAULT_MAX_EXPOSURE_PCT = 0.25  # 25% of bankroll
DEFAULT_KELLY_FRACTION = 0.25  # Quarter Kelly
DEFAULT_MIN_EDGE = 0.05  # 5%
DEFAULT_MIN_CONFIDENCE = 0.6
DEFAULT_MIN_LIQUIDITY = 500.0  # USDC

# Market filters
MIN_VOLUME_24H = 100.0  # USDC
MIN_HOURS_TO_RESOLUTION = 24  # Don't trade markets resolving within 24h
MAX_PRICE_FOR_BUY = 0.95  # Don't buy above 95 cents
MIN_PRICE_FOR_BUY = 0.05  # Don't buy below 5 cents
