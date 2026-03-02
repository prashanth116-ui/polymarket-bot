"""Health check script — run at the start of each session."""

import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def check_imports():
    """Verify all core imports work."""
    print("Checking imports...")
    try:
        from core.types import Market, Signal, Position, TradeResult, Outcome, Side
        from core.constants import CLOB_API_URL, GAMMA_API_URL, DEFAULT_BRIDGE_URL
        from core.kelly import kelly_fraction, size_position
        from config.loader import load_settings, get_env
        from execution.paper_executor import PaperExecutor
        from execution.bridge_executor import BridgeExecutor
        from runners.notifier import TelegramNotifier
        print("  All imports OK")
        return True
    except ImportError as e:
        print(f"  FAIL: {e}")
        return False


def check_config():
    """Verify settings.yaml loads."""
    print("Checking config...")
    try:
        from config.loader import load_settings
        settings = load_settings()
        mode = settings.get("mode", "unknown")
        print(f"  Mode: {mode}")
        print(f"  Bridge URL: {settings.get('bridge', {}).get('url', 'not set')}")
        print(f"  Max daily loss: ${settings.get('risk', {}).get('max_daily_loss', 0)}")
        return True
    except Exception as e:
        print(f"  FAIL: {e}")
        return False


def check_bridge():
    """Check if TypeScript bridge is running."""
    print("Checking bridge...")
    try:
        import requests
        from core.constants import DEFAULT_BRIDGE_URL
        resp = requests.get(f"{DEFAULT_BRIDGE_URL}/health", timeout=3)
        data = resp.json()
        print(f"  Bridge: {'connected' if data.get('ok') else 'error'}")
        print(f"  Wallet: {data.get('mode', 'unknown')}")
        return True
    except requests.exceptions.ConnectionError:
        print("  Bridge not running (start with: cd bridge && npm run dev)")
        return False
    except Exception as e:
        print(f"  FAIL: {e}")
        return False


def check_env():
    """Check environment variables."""
    print("Checking environment...")
    from config.loader import get_env, load_env
    load_env()

    keys = {
        "POLY_PRIVATE_KEY": "Polymarket wallet",
        "TELEGRAM_BOT_TOKEN": "Telegram bot",
        "TELEGRAM_CHAT_ID": "Telegram chat",
        "ANTHROPIC_API_KEY": "Claude API",
    }

    all_set = True
    for key, desc in keys.items():
        value = get_env(key)
        if value:
            print(f"  {desc}: configured")
        else:
            print(f"  {desc}: NOT SET")
            all_set = False
    return all_set


def check_telegram():
    """Test Telegram connectivity."""
    print("Checking Telegram...")
    from runners.notifier import TelegramNotifier
    notifier = TelegramNotifier()
    if not notifier.enabled:
        print("  Telegram not configured — skipping")
        return True
    if notifier.test():
        print("  Telegram OK")
        return True
    print("  Telegram FAILED")
    return False


def check_clob_api():
    """Test CLOB API connectivity."""
    print("Checking Polymarket API...")
    try:
        import requests
        from core.constants import GAMMA_API_URL
        resp = requests.get(f"{GAMMA_API_URL}/markets?limit=1", timeout=10)
        if resp.status_code == 200:
            markets = resp.json()
            count = len(markets) if isinstance(markets, list) else 0
            print(f"  Gamma API: OK (fetched {count} market)")
            return True
        print(f"  Gamma API: HTTP {resp.status_code}")
        return False
    except Exception as e:
        print(f"  FAIL: {e}")
        return False


def check_paper_executor():
    """Verify paper executor works."""
    print("Checking paper executor...")
    try:
        from execution.paper_executor import PaperExecutor
        from core.types import Outcome, StrategyType

        exe = PaperExecutor(initial_balance=1000.0)

        # Buy
        result = exe.buy(
            market_id="test-market",
            token_id="test-token",
            outcome=Outcome.YES,
            price=0.50,
            size=10.0,
        )
        assert result.paper is True
        assert result.size == 10.0

        # Check position
        positions = exe.get_positions()
        assert len(positions) == 1
        assert positions[0].outcome == Outcome.YES

        # Sell
        result = exe.sell(
            market_id="test-market",
            token_id="test-token",
            outcome=Outcome.YES,
            price=0.55,
            size=10.0,
        )
        assert len(exe.get_positions()) == 0

        summary = exe.summary()
        print(f"  Paper executor OK — balance: ${summary['balance']:.2f}, P/L: ${summary['total_pnl']:.2f}")
        return True
    except Exception as e:
        print(f"  FAIL: {e}")
        return False


def main():
    print("=" * 50)
    print("Polymarket Bot Health Check")
    print("=" * 50)

    results = {
        "Imports": check_imports(),
        "Config": check_config(),
        "Environment": check_env(),
        "Paper Executor": check_paper_executor(),
        "CLOB API": check_clob_api(),
        "Bridge": check_bridge(),
        "Telegram": check_telegram(),
    }

    print("\n" + "=" * 50)
    print("Results:")
    all_pass = True
    for name, passed in results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  {name}: {status}")
        if not passed and name not in ("Bridge", "Telegram", "Environment"):
            all_pass = False

    print("=" * 50)
    if all_pass:
        print("Health check PASSED")
    else:
        print("Health check FAILED — see above for details")

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
