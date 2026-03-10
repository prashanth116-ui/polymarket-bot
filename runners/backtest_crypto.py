"""Backtest for CryptoTrader V2 — replay historical 15-min windows.

Pulls BTC 1-min candles from Coinbase and window resolutions from Gamma API,
then simulates the momentum strategy at various thresholds.

Usage:
    python -m runners.backtest_crypto --days 3
    python -m runners.backtest_crypto --days 7 --threshold 0.002
"""

import argparse
import json
import time
from datetime import datetime, timezone, timedelta

import requests


GAMMA_API_URL = "https://gamma-api.polymarket.com"
COINBASE_API_URL = "https://api.exchange.coinbase.com"


def fetch_btc_candles(start_ts: float, end_ts: float) -> list[dict]:
    """Fetch BTC/USD 1-minute candles from Coinbase.

    Returns list of {time, open, high, low, close, volume} sorted by time ascending.
    Coinbase limits to 300 candles per request, so we paginate.
    """
    all_candles = []
    current_start = start_ts

    while current_start < end_ts:
        current_end = min(current_start + 300 * 60, end_ts)  # 300 candles * 60s

        try:
            resp = requests.get(
                f"{COINBASE_API_URL}/products/BTC-USD/candles",
                params={
                    "granularity": 60,
                    "start": datetime.fromtimestamp(current_start, tz=timezone.utc).isoformat(),
                    "end": datetime.fromtimestamp(current_end, tz=timezone.utc).isoformat(),
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            # Coinbase returns [timestamp, low, high, open, close, volume]
            for row in data:
                all_candles.append({
                    "time": row[0],
                    "open": float(row[3]),
                    "high": float(row[2]),
                    "low": float(row[1]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                })
        except Exception as e:
            print(f"  Coinbase API error: {e}")

        current_start = current_end
        time.sleep(0.3)  # Rate limit

    # Sort by time ascending and deduplicate
    all_candles.sort(key=lambda c: c["time"])
    seen = set()
    unique = []
    for c in all_candles:
        if c["time"] not in seen:
            seen.add(c["time"])
            unique.append(c)

    return unique


def fetch_window_resolution(slug: str) -> str | None:
    """Fetch resolution for a window from Gamma API.

    Returns "UP", "DOWN", or None if not resolved.
    """
    try:
        resp = requests.get(
            f"{GAMMA_API_URL}/events",
            params={"slug": slug},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        events = resp.json()
        if not events:
            return None

        mkt = events[0].get("markets", [{}])[0]
        outcome_prices = mkt.get("outcomePrices", "")

        if isinstance(outcome_prices, str):
            try:
                prices = json.loads(outcome_prices)
            except (ValueError, TypeError):
                return None
        else:
            prices = outcome_prices or []

        if len(prices) >= 2:
            up_price = float(prices[0])
            down_price = float(prices[1])
            if up_price > 0.5:
                return "UP"
            elif down_price > 0.5:
                return "DOWN"

        return None
    except Exception:
        return None


def compute_momentum(candles: list[dict], target_ts: float, window_secs: int = 30) -> float | None:
    """Compute BTC momentum at a specific timestamp using 1-min candles.

    Approximates 30-second momentum using the closest candle closes.
    Uses candle at target_ts vs candle at target_ts - window_secs.

    With 1-min candles, we use the closest available candle.
    """
    if not candles:
        return None

    # Find candle closest to target_ts
    current_candle = None
    for c in candles:
        if c["time"] <= target_ts:
            current_candle = c
        else:
            break

    if current_candle is None:
        return None

    # Find candle closest to target_ts - window_secs
    lookback_ts = target_ts - window_secs
    prev_candle = None
    for c in candles:
        if c["time"] <= lookback_ts:
            prev_candle = c
        else:
            break

    if prev_candle is None or prev_candle["close"] == 0:
        return None

    return (current_candle["close"] - prev_candle["close"]) / prev_candle["close"]


def get_token_prices_at_entry(candles: list[dict], window_start: float,
                              entry_offset: int = 840) -> tuple[float | None, float | None]:
    """Estimate Up/Down token prices at entry time.

    We don't have historical CLOB data, so we estimate based on BTC price movement
    within the window. If BTC is up relative to window open, Up token is higher.

    entry_offset: seconds into window when we enter (default 840 = T-60s)
    """
    # Find BTC price at window start
    open_candle = None
    for c in candles:
        if c["time"] <= window_start:
            open_candle = c
        else:
            break

    # Find BTC price at entry time
    entry_ts = window_start + entry_offset
    entry_candle = None
    for c in candles:
        if c["time"] <= entry_ts:
            entry_candle = c
        else:
            break

    if open_candle is None or entry_candle is None:
        return None, None

    # Compute BTC % change within the window
    btc_change = (entry_candle["close"] - open_candle["close"]) / open_candle["close"]

    # Estimate token prices based on BTC direction within window
    # These are rough estimates based on observed market behavior:
    # - Tokens start around 0.50/0.50
    # - By T-60s, typical range is 0.10-0.90 depending on how clear the direction is
    # - Map BTC change to token price: 0.1% BTC move ≈ 0.15 token price shift
    shift = min(abs(btc_change) * 1500, 0.45)  # Cap at 0.45 (prices 0.05-0.95)

    if btc_change > 0:
        up_price = 0.50 + shift
        down_price = 0.50 - shift
    else:
        up_price = 0.50 - shift
        down_price = 0.50 + shift

    # Clamp to reasonable range
    up_price = max(0.02, min(0.98, up_price))
    down_price = max(0.02, min(0.98, down_price))

    return up_price, down_price


def run_backtest(days: int = 3, thresholds: list[float] = None,
                 entry_window_secs: int = 60, base_position_size: float = 20.0,
                 min_entry_price: float = 0.05, max_entry_price: float = 0.55):
    """Run backtest over historical windows."""

    if thresholds is None:
        thresholds = [0.0005, 0.001, 0.0015, 0.002, 0.003, 0.004, 0.005]

    interval_secs = 900  # 15 minutes

    # Time range
    end_ts = time.time() - 900  # Skip current (unresolved) window
    start_ts = end_ts - (days * 86400)

    print(f"=== CryptoTrader V2 Backtest ===")
    print(f"Period: {datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC "
          f"to {datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"Days: {days}")
    print(f"Entry window: last {entry_window_secs}s of each 15-min window")
    print(f"Price range: ${min_entry_price:.2f} - ${max_entry_price:.2f}")
    print(f"Position size: ${base_position_size:.2f} (base, 1x-2x scaled)")
    print()

    # 1. Fetch BTC candles
    print("Fetching BTC/USD 1-min candles from Coinbase...")
    candles = fetch_btc_candles(start_ts - 300, end_ts + 300)  # Buffer
    print(f"  Got {len(candles)} candles")

    if len(candles) < 100:
        print("ERROR: Not enough candle data")
        return

    # 2. Generate window slugs and fetch resolutions
    print("Fetching window resolutions from Gamma API...")
    windows = []
    window_ts = int(start_ts // interval_secs) * interval_secs

    while window_ts < end_ts:
        slug = f"btc-updown-15m-{window_ts}"
        resolution = fetch_window_resolution(slug)

        if resolution:
            # Compute momentum at entry time (T-60s before window close)
            entry_ts = window_ts + interval_secs - entry_window_secs
            momentum_30s = compute_momentum(candles, entry_ts, window_secs=30)
            momentum_60s = compute_momentum(candles, entry_ts, window_secs=60)
            momentum_120s = compute_momentum(candles, entry_ts, window_secs=120)

            # Estimate token prices
            up_price, down_price = get_token_prices_at_entry(candles, window_ts)

            windows.append({
                "slug": slug,
                "window_ts": window_ts,
                "resolution": resolution,
                "momentum_30s": momentum_30s,
                "momentum_60s": momentum_60s,
                "momentum_120s": momentum_120s,
                "up_price": up_price,
                "down_price": down_price,
            })

        window_ts += interval_secs
        time.sleep(0.15)  # Rate limit Gamma API

    print(f"  Got {len(windows)} resolved windows")

    if not windows:
        print("ERROR: No resolved windows found")
        return

    # Count UP vs DOWN
    up_count = sum(1 for w in windows if w["resolution"] == "UP")
    down_count = sum(1 for w in windows if w["resolution"] == "DOWN")
    print(f"  Resolutions: {up_count} UP, {down_count} DOWN ({up_count/(up_count+down_count):.0%} UP)")
    print()

    # 3. Momentum direction accuracy (raw — no threshold)
    correct_30s = sum(1 for w in windows if w["momentum_30s"] is not None and
                      ((w["momentum_30s"] > 0 and w["resolution"] == "UP") or
                       (w["momentum_30s"] < 0 and w["resolution"] == "DOWN")))
    total_with_momentum = sum(1 for w in windows if w["momentum_30s"] is not None)

    if total_with_momentum > 0:
        print(f"Raw momentum accuracy (30s, no threshold): "
              f"{correct_30s}/{total_with_momentum} = {correct_30s/total_with_momentum:.1%}")

    correct_60s = sum(1 for w in windows if w["momentum_60s"] is not None and
                      ((w["momentum_60s"] > 0 and w["resolution"] == "UP") or
                       (w["momentum_60s"] < 0 and w["resolution"] == "DOWN")))
    total_60s = sum(1 for w in windows if w["momentum_60s"] is not None)
    if total_60s > 0:
        print(f"Raw momentum accuracy (60s, no threshold): "
              f"{correct_60s}/{total_60s} = {correct_60s/total_60s:.1%}")

    correct_120s = sum(1 for w in windows if w["momentum_120s"] is not None and
                       ((w["momentum_120s"] > 0 and w["resolution"] == "UP") or
                        (w["momentum_120s"] < 0 and w["resolution"] == "DOWN")))
    total_120s = sum(1 for w in windows if w["momentum_120s"] is not None)
    if total_120s > 0:
        print(f"Raw momentum accuracy (120s, no threshold): "
              f"{correct_120s}/{total_120s} = {correct_120s/total_120s:.1%}")
    print()

    # 4. Simulate strategy at various momentum thresholds
    print(f"{'Threshold':>10} | {'Trades':>6} | {'Wins':>4} | {'Losses':>6} | {'Win%':>5} | "
          f"{'Avg Size':>8} | {'Total P/L':>10} | {'Avg P/L':>8} | {'Max DD':>7} | {'Consec L':>8}")
    print("-" * 105)

    results = {}

    for threshold in thresholds:
        balance = 200.0
        initial_balance = balance
        trades = []
        consec_losses = 0
        max_consec_losses = 0
        max_drawdown = 0
        peak_balance = balance
        daily_pnl = {}

        for w in windows:
            m = w["momentum_30s"]
            if m is None:
                continue

            # Check momentum threshold
            if abs(m) < threshold:
                continue

            # Determine direction
            direction = "UP" if m > 0 else "DOWN"

            # Get entry price
            if direction == "UP":
                entry_price = w["up_price"]
            else:
                entry_price = w["down_price"]

            if entry_price is None:
                continue

            # Price range filter
            if entry_price < min_entry_price or entry_price > max_entry_price:
                continue

            # Fee
            fee = 0.25 * (entry_price * (1 - entry_price)) ** 2

            # Position sizing (1x-2x based on momentum strength)
            momentum_strength = abs(m) / threshold
            size_mult = min(momentum_strength, 2.0)
            position_size = base_position_size * size_mult

            # Number of shares
            shares = position_size / (entry_price + fee)
            cost = shares * (entry_price + fee)

            # Check resolution
            won = (direction == w["resolution"])

            if won:
                pnl = shares * (1.0 - entry_price) - shares * fee  # Payout $1/share minus entry cost and fee
                pnl = shares - cost  # Simplified: shares * $1 - cost
                consec_losses = 0
            else:
                pnl = -cost
                consec_losses += 1
                max_consec_losses = max(max_consec_losses, consec_losses)

            balance += pnl
            peak_balance = max(peak_balance, balance)
            drawdown = peak_balance - balance
            max_drawdown = max(max_drawdown, drawdown)

            # Track daily P/L
            day = datetime.fromtimestamp(w["window_ts"], tz=timezone.utc).strftime("%Y-%m-%d")
            daily_pnl[day] = daily_pnl.get(day, 0) + pnl

            trades.append({
                "slug": w["slug"],
                "direction": direction,
                "resolution": w["resolution"],
                "entry_price": entry_price,
                "momentum": m,
                "won": won,
                "pnl": pnl,
                "cost": cost,
                "balance": balance,
            })

        wins = sum(1 for t in trades if t["won"])
        losses = len(trades) - wins
        total_pnl = balance - initial_balance
        avg_pnl = total_pnl / len(trades) if trades else 0
        avg_size = sum(t["cost"] for t in trades) / len(trades) if trades else 0
        win_rate = wins / len(trades) if trades else 0

        results[threshold] = {
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "avg_pnl": avg_pnl,
            "max_drawdown": max_drawdown,
            "max_consec_losses": max_consec_losses,
            "daily_pnl": daily_pnl,
        }

        print(f"{threshold:>10.4f} | {len(trades):>6} | {wins:>4} | {losses:>6} | "
              f"{win_rate:>4.0%} | ${avg_size:>7.2f} | ${total_pnl:>9.2f} | "
              f"${avg_pnl:>7.2f} | ${max_drawdown:>6.2f} | {max_consec_losses:>8}")

    print()

    # 5. Find best threshold
    best_threshold = max(results.keys(), key=lambda t: results[t]["total_pnl"])
    best = results[best_threshold]

    print(f"=== Best Threshold: {best_threshold:.4f} ({best_threshold*10000:.0f} bps) ===")
    print(f"Trades: {len(best['trades'])} ({best['wins']}W-{best['losses']}L)")
    print(f"Win Rate: {best['win_rate']:.1%}")
    print(f"Total P/L: ${best['total_pnl']:+.2f}")
    print(f"Max Drawdown: ${best['max_drawdown']:.2f}")
    print(f"Max Consecutive Losses: {best['max_consec_losses']}")
    print()

    # 6. Daily P/L breakdown for best threshold
    if best["daily_pnl"]:
        print(f"Daily P/L (threshold={best_threshold:.4f}):")
        for day in sorted(best["daily_pnl"].keys()):
            pnl = best["daily_pnl"][day]
            print(f"  {day}: ${pnl:+.2f}")
        print()

    # 7. Show individual trades for current threshold (0.002)
    current = results.get(0.002, results.get(best_threshold))
    if current and current["trades"]:
        print(f"=== Trade Details (threshold=0.002) ===")
        for t in current["trades"]:
            ts = int(t["slug"].split("-")[-1])
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m/%d %H:%M")
            result = "WIN " if t["won"] else "LOSS"
            print(f"  {dt} | {t['direction']:>4} | entry=${t['entry_price']:.4f} | "
                  f"mom={t['momentum']:+.4%} | {result} | P/L=${t['pnl']:+.2f} | "
                  f"bal=${t['balance']:.2f}")

    print()
    print("=== Recommendations ===")

    # Generate recommendations
    if best["win_rate"] < 0.55:
        print("- WIN RATE IS BELOW 55% — momentum at T-60s is a weak predictor of 15-min resolution")
        print("  Consider: longer momentum window (60s or 120s), or enter earlier (T-90s)")

    if best["win_rate"] >= 0.55 and best["total_pnl"] > 0:
        print(f"- Optimal threshold: {best_threshold*10000:.0f} bps ({best['win_rate']:.0%} win rate)")

    # Compare momentum windows
    for label, key in [("30s", "momentum_30s"), ("60s", "momentum_60s"), ("120s", "momentum_120s")]:
        above_thresh = [w for w in windows if w[key] is not None and abs(w[key]) >= best_threshold]
        if above_thresh:
            correct = sum(1 for w in above_thresh if
                         (w[key] > 0 and w["resolution"] == "UP") or
                         (w[key] < 0 and w["resolution"] == "DOWN"))
            wr = correct / len(above_thresh)
            print(f"- {label} momentum at {best_threshold*10000:.0f}bps: {correct}/{len(above_thresh)} = {wr:.0%} win rate")

    if best_threshold != 0.002:
        print(f"- Current 20bps threshold is {'too high' if 0.002 > best_threshold else 'too low'} — "
              f"consider switching to {best_threshold*10000:.0f}bps")

    return results


def main():
    parser = argparse.ArgumentParser(description="Backtest CryptoTrader V2")
    parser.add_argument("--days", type=int, default=3, help="Number of days to backtest")
    parser.add_argument("--threshold", type=float, default=None, help="Single threshold to test")
    args = parser.parse_args()

    thresholds = [args.threshold] if args.threshold else None
    run_backtest(days=args.days, thresholds=thresholds)


if __name__ == "__main__":
    main()
