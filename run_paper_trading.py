"""Paper trading wrapper with health check and auto-restart.

24/7 operation — no market hours gate (prediction markets never close).
"""

import os
import sys
import time
import subprocess
import logging
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MAX_RESTARTS_PER_DAY = 10
RESTART_DELAY = 30  # seconds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/paper_trading.log", mode="a"),
    ],
)
logger = logging.getLogger(__name__)


def ensure_logs_dir():
    os.makedirs("logs", exist_ok=True)


def run_health_check() -> bool:
    """Run health check and return True if passed."""
    try:
        result = subprocess.run(
            [sys.executable, "health_check.py"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        logger.info(result.stdout)
        if result.returncode != 0:
            logger.error(f"Health check failed:\n{result.stderr}")
        return result.returncode == 0
    except Exception as e:
        logger.error(f"Health check error: {e}")
        return False


def run_bot(args: list[str]) -> int:
    """Run the trading bot and return exit code."""
    cmd = [sys.executable, "-m", "runners.run_live", "--paper"] + args
    logger.info(f"Starting bot: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, timeout=None)
        return result.returncode
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
        return 0
    except Exception as e:
        logger.error(f"Bot crashed: {e}")
        return 1


def main():
    ensure_logs_dir()

    args = sys.argv[1:]
    restarts_today = 0
    last_restart_date = datetime.utcnow().date()

    logger.info("=" * 50)
    logger.info("Polymarket Paper Trading Wrapper")
    logger.info("=" * 50)

    # Initial health check
    if not run_health_check():
        logger.warning("Health check failed — starting anyway")

    while True:
        today = datetime.utcnow().date()
        if today != last_restart_date:
            restarts_today = 0
            last_restart_date = today

        if restarts_today >= MAX_RESTARTS_PER_DAY:
            logger.error(f"Max restarts ({MAX_RESTARTS_PER_DAY}) exceeded today — stopping")
            return 1

        exit_code = run_bot(args)

        if exit_code == 0:
            logger.info("Bot exited cleanly")
            return 0

        restarts_today += 1
        logger.warning(
            f"Bot crashed (exit {exit_code}) — restart {restarts_today}/{MAX_RESTARTS_PER_DAY} "
            f"in {RESTART_DELAY}s"
        )
        time.sleep(RESTART_DELAY)


if __name__ == "__main__":
    sys.exit(main())
