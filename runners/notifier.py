"""Telegram notification system."""

import logging
import os
from datetime import datetime, timezone

import requests

from config.loader import get_env

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Send alerts via Telegram bot."""

    def __init__(self, bot_token: str = None, chat_id: str = None):
        self.bot_token = bot_token or get_env("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or get_env("TELEGRAM_CHAT_ID")
        self.enabled = bool(self.bot_token and self.chat_id)
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}" if self.bot_token else ""

        if not self.enabled:
            logger.warning("Telegram not configured — alerts disabled")

    def send(self, message: str, silent: bool = False) -> bool:
        """Send a message to the configured chat."""
        if not self.enabled:
            logger.debug(f"Telegram disabled, would send: {message[:100]}...")
            return False

        try:
            resp = requests.post(
                f"{self.api_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                    "disable_notification": silent,
                },
                timeout=10,
            )
            if resp.status_code == 200:
                return True
            logger.error(f"Telegram send failed: {resp.status_code} {resp.text}")
            return False
        except Exception as e:
            logger.error(f"Telegram send error: {e}")
            return False

    def send_entry(
        self,
        market_question: str,
        outcome: str,
        price: float,
        size: float,
        edge: float,
        strategy: str,
    ):
        msg = (
            f"🟢 <b>ENTRY</b>\n"
            f"<b>Market:</b> {market_question}\n"
            f"<b>Outcome:</b> {outcome} @ ${price:.4f}\n"
            f"<b>Size:</b> ${size:.2f}\n"
            f"<b>Edge:</b> {edge:.1%}\n"
            f"<b>Strategy:</b> {strategy}\n"
            f"<b>Time:</b> {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
        )
        self.send(msg)

    def send_exit(
        self,
        market_question: str,
        outcome: str,
        entry_price: float,
        exit_price: float,
        pnl: float,
        reason: str,
    ):
        emoji = "🟢" if pnl >= 0 else "🔴"
        msg = (
            f"{emoji} <b>EXIT</b>\n"
            f"<b>Market:</b> {market_question}\n"
            f"<b>Outcome:</b> {outcome}\n"
            f"<b>Entry:</b> ${entry_price:.4f} → <b>Exit:</b> ${exit_price:.4f}\n"
            f"<b>P/L:</b> ${pnl:.2f}\n"
            f"<b>Reason:</b> {reason}\n"
            f"<b>Time:</b> {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
        )
        self.send(msg)

    def send_daily_summary(self, summary: dict):
        emoji = "🟢" if summary.get("daily_pnl", 0) >= 0 else "🔴"
        msg = (
            f"📊 <b>DAILY SUMMARY</b>\n"
            f"{emoji} P/L: ${summary.get('daily_pnl', 0):.2f}\n"
            f"Trades: {summary.get('daily_trades', 0)}\n"
            f"Open Positions: {summary.get('open_positions', 0)}\n"
            f"Balance: ${summary.get('balance', 0):.2f}\n"
            f"Total P/L: ${summary.get('total_pnl', 0):.2f}"
        )
        self.send(msg)

    def send_heartbeat(self, summary: dict):
        msg = (
            f"💓 <b>Heartbeat</b> | "
            f"Balance: ${summary.get('balance', 0):.2f} | "
            f"Positions: {summary.get('open_positions', 0)} | "
            f"Daily P/L: ${summary.get('daily_pnl', 0):.2f}"
        )
        self.send(msg, silent=True)

    def send_risk_alert(self, message: str):
        msg = f"⚠️ <b>RISK ALERT</b>\n{message}"
        self.send(msg)

    def send_error(self, message: str):
        msg = f"❌ <b>ERROR</b>\n{message}"
        self.send(msg)

    def test(self) -> bool:
        """Send a test message."""
        return self.send("🤖 Polymarket bot — Telegram connection test OK")
