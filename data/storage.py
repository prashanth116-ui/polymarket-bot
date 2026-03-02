"""SQLite persistence for markets, trades, P/L, estimates, model scores."""

import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "data/polymarket.db"
RETENTION_DAYS = 90


class Storage:
    """SQLite storage with 90-day retention."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        cur = self.conn.cursor()
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS markets (
                condition_id TEXT PRIMARY KEY,
                question TEXT NOT NULL,
                category TEXT,
                end_date TEXT,
                yes_token_id TEXT,
                no_token_id TEXT,
                volume REAL DEFAULT 0,
                liquidity REAL DEFAULT 0,
                last_price_yes REAL DEFAULT 0.5,
                last_price_no REAL DEFAULT 0.5,
                resolution TEXT,
                active INTEGER DEFAULT 1,
                first_seen TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                size REAL NOT NULL,
                cost REAL NOT NULL,
                fee REAL DEFAULT 0,
                order_id TEXT,
                strategy TEXT,
                exit_reason TEXT,
                paper INTEGER DEFAULT 1,
                timestamp TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS daily_pnl (
                date TEXT NOT NULL,
                strategy TEXT NOT NULL,
                pnl REAL NOT NULL,
                trades INTEGER NOT NULL,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                fees REAL DEFAULT 0,
                PRIMARY KEY (date, strategy)
            );

            CREATE TABLE IF NOT EXISTS estimates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                probability REAL NOT NULL,
                confidence REAL NOT NULL,
                model_name TEXT NOT NULL,
                reasoning TEXT,
                timestamp TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS model_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model_name TEXT NOT NULL,
                market_id TEXT NOT NULL,
                predicted_prob REAL NOT NULL,
                actual_outcome INTEGER NOT NULL,
                brier_score REAL NOT NULL,
                timestamp TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS positions (
                market_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                token_id TEXT,
                entry_price REAL NOT NULL,
                size REAL NOT NULL,
                cost_basis REAL NOT NULL,
                strategy TEXT,
                opened_at TEXT NOT NULL,
                PRIMARY KEY (market_id, outcome)
            );

            CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market_id);
            CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(timestamp);
            CREATE INDEX IF NOT EXISTS idx_estimates_market ON estimates(market_id);
            CREATE INDEX IF NOT EXISTS idx_model_scores_model ON model_scores(model_name);
        """)
        self.conn.commit()

    # --- Markets ---

    def upsert_market(self, condition_id: str, question: str, category: str = "",
                      end_date: str = None, yes_token_id: str = "", no_token_id: str = "",
                      volume: float = 0, liquidity: float = 0,
                      last_price_yes: float = 0.5, last_price_no: float = 0.5,
                      active: bool = True):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute("""
            INSERT INTO markets (condition_id, question, category, end_date,
                yes_token_id, no_token_id, volume, liquidity,
                last_price_yes, last_price_no, active, first_seen, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(condition_id) DO UPDATE SET
                question=excluded.question, category=excluded.category,
                volume=excluded.volume, liquidity=excluded.liquidity,
                last_price_yes=excluded.last_price_yes, last_price_no=excluded.last_price_no,
                active=excluded.active, updated_at=excluded.updated_at
        """, (condition_id, question, category, end_date,
              yes_token_id, no_token_id, volume, liquidity,
              last_price_yes, last_price_no, int(active), now, now))
        self.conn.commit()

    def get_market(self, condition_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM markets WHERE condition_id = ?", (condition_id,)
        ).fetchone()
        return dict(row) if row else None

    def set_resolution(self, condition_id: str, resolution: str):
        self.conn.execute(
            "UPDATE markets SET resolution = ?, active = 0 WHERE condition_id = ?",
            (resolution, condition_id)
        )
        self.conn.commit()

    # --- Trades ---

    def record_trade(self, market_id: str, outcome: str, side: str, price: float,
                     size: float, cost: float, fee: float = 0, order_id: str = None,
                     strategy: str = None, exit_reason: str = None, paper: bool = True):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute("""
            INSERT INTO trades (market_id, outcome, side, price, size, cost, fee,
                order_id, strategy, exit_reason, paper, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (market_id, outcome, side, price, size, cost, fee,
              order_id, strategy, exit_reason, int(paper), now))
        self.conn.commit()

    def get_trades(self, market_id: str = None, days: int = 30) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        if market_id:
            rows = self.conn.execute(
                "SELECT * FROM trades WHERE market_id = ? AND timestamp > ? ORDER BY timestamp DESC",
                (market_id, cutoff)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM trades WHERE timestamp > ? ORDER BY timestamp DESC",
                (cutoff,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_trade_count(self, days: int = 1) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM trades WHERE timestamp > ?", (cutoff,)
        ).fetchone()
        return row["cnt"] if row else 0

    # --- Daily P/L ---

    def record_daily_pnl(self, date: str, strategy: str, pnl: float,
                         trades: int, wins: int = 0, losses: int = 0, fees: float = 0):
        self.conn.execute("""
            INSERT INTO daily_pnl (date, strategy, pnl, trades, wins, losses, fees)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, strategy) DO UPDATE SET
                pnl=excluded.pnl, trades=excluded.trades,
                wins=excluded.wins, losses=excluded.losses, fees=excluded.fees
        """, (date, strategy, pnl, trades, wins, losses, fees))
        self.conn.commit()

    def get_daily_pnl(self, days: int = 30) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = self.conn.execute(
            "SELECT * FROM daily_pnl WHERE date >= ? ORDER BY date DESC", (cutoff,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_total_pnl(self, days: int = 30) -> float:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        row = self.conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) as total FROM daily_pnl WHERE date >= ?", (cutoff,)
        ).fetchone()
        return row["total"] if row else 0.0

    # --- Estimates ---

    def record_estimate(self, market_id: str, outcome: str, probability: float,
                        confidence: float, model_name: str, reasoning: str = ""):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute("""
            INSERT INTO estimates (market_id, outcome, probability, confidence,
                model_name, reasoning, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (market_id, outcome, probability, confidence, model_name, reasoning, now))
        self.conn.commit()

    def get_latest_estimate(self, market_id: str, model_name: str = None) -> Optional[dict]:
        if model_name:
            row = self.conn.execute("""
                SELECT * FROM estimates
                WHERE market_id = ? AND model_name = ?
                ORDER BY timestamp DESC LIMIT 1
            """, (market_id, model_name)).fetchone()
        else:
            row = self.conn.execute("""
                SELECT * FROM estimates
                WHERE market_id = ?
                ORDER BY timestamp DESC LIMIT 1
            """, (market_id,)).fetchone()
        return dict(row) if row else None

    # --- Model Scores ---

    def record_model_score(self, model_name: str, market_id: str,
                           predicted_prob: float, actual_outcome: int, brier_score: float):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute("""
            INSERT INTO model_scores (model_name, market_id, predicted_prob,
                actual_outcome, brier_score, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (model_name, market_id, predicted_prob, actual_outcome, brier_score, now))
        self.conn.commit()

    def get_model_brier(self, model_name: str, days: int = 30) -> Optional[float]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        row = self.conn.execute("""
            SELECT AVG(brier_score) as avg_brier FROM model_scores
            WHERE model_name = ? AND timestamp > ?
        """, (model_name, cutoff)).fetchone()
        return row["avg_brier"] if row and row["avg_brier"] is not None else None

    def get_model_scores(self, model_name: str, days: int = 30) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = self.conn.execute("""
            SELECT * FROM model_scores
            WHERE model_name = ? AND timestamp > ?
            ORDER BY timestamp DESC
        """, (model_name, cutoff)).fetchall()
        return [dict(r) for r in rows]

    # --- Positions ---

    def save_position(self, market_id: str, outcome: str, token_id: str,
                      entry_price: float, size: float, cost_basis: float,
                      strategy: str = None):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute("""
            INSERT INTO positions (market_id, outcome, token_id, entry_price,
                size, cost_basis, strategy, opened_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(market_id, outcome) DO UPDATE SET
                entry_price=excluded.entry_price, size=excluded.size,
                cost_basis=excluded.cost_basis
        """, (market_id, outcome, token_id, entry_price, size, cost_basis, strategy, now))
        self.conn.commit()

    def remove_position(self, market_id: str, outcome: str):
        self.conn.execute(
            "DELETE FROM positions WHERE market_id = ? AND outcome = ?",
            (market_id, outcome)
        )
        self.conn.commit()

    def get_positions(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM positions").fetchall()
        return [dict(r) for r in rows]

    # --- Maintenance ---

    def cleanup_old_data(self, retention_days: int = RETENTION_DAYS):
        """Remove data older than retention period."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        cutoff_date = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime("%Y-%m-%d")

        self.conn.execute("DELETE FROM trades WHERE timestamp < ?", (cutoff,))
        self.conn.execute("DELETE FROM estimates WHERE timestamp < ?", (cutoff,))
        self.conn.execute("DELETE FROM model_scores WHERE timestamp < ?", (cutoff,))
        self.conn.execute("DELETE FROM daily_pnl WHERE date < ?", (cutoff_date,))
        self.conn.commit()
        logger.info(f"Cleaned up data older than {retention_days} days")

    def close(self):
        self.conn.close()
