"""Backtest: Multi-Window Trend Following for CryptoTrader V2.

Strategy: After N consecutive same-direction window resolutions, bet on
continuation in the next window. Enter early (T-300s or T-180s) when token
prices are still near $0.50 for balanced risk:reward.

Also tests fade (contrarian) and combined approaches.

Usage:
    python -m runners.backtest_crypto_trend
"""

import time
from datetime import datetime, timezone
from collections import Counter

from runners.backtest_crypto import (
    fetch_btc_candles,
    fetch_window_resolution,
    compute_momentum,
    get_token_prices_at_entry,
)


def crypto_fee_rate(price):
    return 0.25 * (price * (1 - price)) ** 2


def simulate_trades(trades_list, bankroll=200.0):
    """Given a list of (won, entry_price, position_size) tuples, compute P/L."""
    balance = bankroll
    results = []
    consec_losses = 0
    max_consec = 0
    peak = balance
    max_dd = 0
    daily_pnl = {}

    for t in trades_list:
        won = t["won"]
        entry_price = t["entry_price"]
        pos_size = t.get("position_size", 20.0)

        fee = crypto_fee_rate(entry_price)
        shares = pos_size / (entry_price + fee)
        cost = shares * (entry_price + fee)

        if won:
            pnl = shares - cost
            consec_losses = 0
        else:
            pnl = -cost
            consec_losses += 1
            max_consec = max(max_consec, consec_losses)

        balance += pnl
        peak = max(peak, balance)
        dd = peak - balance
        max_dd = max(max_dd, dd)

        day = t.get("day", "unknown")
        daily_pnl[day] = daily_pnl.get(day, 0) + pnl

        results.append({**t, "pnl": pnl, "balance": balance, "cost": cost})

    wins = sum(1 for r in results if r["won"])
    losses = len(results) - wins
    total_pnl = balance - bankroll

    return {
        "trades": results,
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(results) if results else 0,
        "total_pnl": total_pnl,
        "max_dd": max_dd,
        "max_consec": max_consec,
        "daily_pnl": daily_pnl,
        "avg_entry": sum(r["entry_price"] for r in results) / len(results) if results else 0,
    }


def main():
    interval_secs = 900
    end_ts = time.time() - 900
    start_ts = end_ts - (7 * 86400)

    print("=" * 85)
    print("CryptoTrader V2 — Multi-Window Trend Backtest")
    print("=" * 85)
    print(f"Period: {datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} to "
          f"{datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print()

    # Fetch data
    print("Fetching BTC candles...")
    candles = fetch_btc_candles(start_ts - 600, end_ts + 300)
    print(f"  {len(candles)} candles")

    print("Fetching window resolutions...")
    windows = []
    window_ts = int(start_ts // interval_secs) * interval_secs
    while window_ts < end_ts:
        slug = f"btc-updown-15m-{window_ts}"
        resolution = fetch_window_resolution(slug)
        if resolution:
            w = {"resolution": resolution, "window_ts": window_ts,
                 "day": datetime.fromtimestamp(window_ts, tz=timezone.utc).strftime("%Y-%m-%d"),
                 "time": datetime.fromtimestamp(window_ts, tz=timezone.utc).strftime("%m/%d %H:%M")}

            # Compute momentum at various entry points
            for label, offset in [("T60", 840), ("T120", 780), ("T180", 720), ("T300", 600)]:
                entry_ts = window_ts + offset
                w[f"m30_{label}"] = compute_momentum(candles, entry_ts, 30)
                w[f"m60_{label}"] = compute_momentum(candles, entry_ts, 60)
                w[f"m120_{label}"] = compute_momentum(candles, entry_ts, 120)
                up_p, down_p = get_token_prices_at_entry(candles, window_ts, entry_offset=offset)
                w[f"up_{label}"] = up_p
                w[f"down_{label}"] = down_p

            # BTC price change over the full window (for fade strategy)
            open_candle = None
            for c in candles:
                if c["time"] <= window_ts:
                    open_candle = c
                else:
                    break
            mid_candle = None
            for c in candles:
                if c["time"] <= window_ts + 600:  # 10 min in
                    mid_candle = c
                else:
                    break
            if open_candle and mid_candle and open_candle["close"] > 0:
                w["btc_10min_move"] = (mid_candle["close"] - open_candle["close"]) / open_candle["close"]
            else:
                w["btc_10min_move"] = None

            windows.append(w)
        window_ts += interval_secs
        time.sleep(0.15)

    up_ct = sum(1 for w in windows if w["resolution"] == "UP")
    print(f"  {len(windows)} windows ({up_ct} UP, {len(windows) - up_ct} DOWN)")
    print()

    # ======================================================================
    # ANALYSIS 1: Streak patterns in resolutions
    # ======================================================================
    print("=" * 85)
    print("STREAK ANALYSIS — How often do consecutive same-direction windows continue?")
    print("=" * 85)

    for streak_len in [1, 2, 3, 4, 5]:
        continues = 0
        reverses = 0
        for i in range(streak_len, len(windows)):
            # Check if last streak_len windows all went same direction
            streak_dir = windows[i - 1]["resolution"]
            all_same = all(windows[i - j - 1]["resolution"] == streak_dir
                          for j in range(streak_len))
            if not all_same:
                continue

            # Check if current window continues
            if windows[i]["resolution"] == streak_dir:
                continues += 1
            else:
                reverses += 1

        total = continues + reverses
        if total > 0:
            cont_rate = continues / total
            print(f"  After {streak_len} consecutive same: {continues}/{total} continue "
                  f"({cont_rate:.1%}), {reverses} reverse ({1 - cont_rate:.1%})")

    print()

    # Alternation pattern
    alternates = 0
    same = 0
    for i in range(1, len(windows)):
        if windows[i]["resolution"] != windows[i - 1]["resolution"]:
            alternates += 1
        else:
            same += 1
    print(f"  Overall: {same} same-as-prev ({same/(same+alternates):.1%}), "
          f"{alternates} alternates ({alternates/(same+alternates):.1%})")
    print()

    # ======================================================================
    # ANALYSIS 2: Time-of-day bias
    # ======================================================================
    print("=" * 85)
    print("TIME-OF-DAY BIAS — UP rate by hour (UTC)")
    print("=" * 85)

    hour_stats = {}
    for w in windows:
        hour = datetime.fromtimestamp(w["window_ts"], tz=timezone.utc).hour
        if hour not in hour_stats:
            hour_stats[hour] = {"up": 0, "down": 0}
        if w["resolution"] == "UP":
            hour_stats[hour]["up"] += 1
        else:
            hour_stats[hour]["down"] += 1

    for hour in sorted(hour_stats.keys()):
        s = hour_stats[hour]
        total = s["up"] + s["down"]
        up_rate = s["up"] / total
        bar = "#" * int(up_rate * 30)
        print(f"  {hour:02d}:00 | {s['up']:>3}U {s['down']:>3}D | {up_rate:>5.0%} | {bar}")

    print()

    # ======================================================================
    # STRATEGY 1: Multi-Window Trend Following
    # ======================================================================
    print("=" * 85)
    print("STRATEGY 1: TREND FOLLOWING — Bet on continuation after N-streak")
    print("=" * 85)

    hdr = (f"{'Streak':>6} | {'Entry':>5} | {'Trades':>6} | {'Wins':>4} | {'Loss':>4} | "
           f"{'WR':>5} | {'AvgEnt':>7} | {'P/L':>9} | {'MaxDD':>7} | {'CL':>3}")
    print(hdr)
    print("-" * len(hdr))

    for streak_req in [1, 2, 3, 4]:
        for entry_label in ["T300", "T180", "T120", "T60"]:
            trades = []
            for i in range(streak_req, len(windows)):
                streak_dir = windows[i - 1]["resolution"]
                all_same = all(windows[i - j - 1]["resolution"] == streak_dir
                               for j in range(streak_req))
                if not all_same:
                    continue

                # Bet on continuation
                bet_dir = streak_dir
                w = windows[i]

                up_key = f"up_{entry_label}"
                down_key = f"down_{entry_label}"
                entry_price = w[up_key] if bet_dir == "UP" else w[down_key]
                if entry_price is None:
                    continue

                won = bet_dir == w["resolution"]
                trades.append({
                    "won": won, "entry_price": entry_price,
                    "direction": bet_dir, "resolution": w["resolution"],
                    "day": w["day"], "time": w["time"],
                    "position_size": 20.0,
                })

            if not trades:
                continue
            r = simulate_trades(trades)
            print(f"{streak_req:>6} | {entry_label:>5} | {len(r['trades']):>6} | "
                  f"{r['wins']:>4} | {r['losses']:>4} | {r['win_rate']:>4.0%} | "
                  f"${r['avg_entry']:>.4f} | ${r['total_pnl']:>+8.2f} | "
                  f"${r['max_dd']:>6.2f} | {r['max_consec']:>3}")

    print()

    # ======================================================================
    # STRATEGY 2: Fade / Contrarian — Bet on reversal after streak
    # ======================================================================
    print("=" * 85)
    print("STRATEGY 2: CONTRARIAN — Bet on REVERSAL after N-streak")
    print("=" * 85)
    print(hdr)
    print("-" * len(hdr))

    for streak_req in [1, 2, 3, 4]:
        for entry_label in ["T300", "T180", "T120", "T60"]:
            trades = []
            for i in range(streak_req, len(windows)):
                streak_dir = windows[i - 1]["resolution"]
                all_same = all(windows[i - j - 1]["resolution"] == streak_dir
                               for j in range(streak_req))
                if not all_same:
                    continue

                # Bet AGAINST the streak
                bet_dir = "DOWN" if streak_dir == "UP" else "UP"
                w = windows[i]

                up_key = f"up_{entry_label}"
                down_key = f"down_{entry_label}"
                entry_price = w[up_key] if bet_dir == "UP" else w[down_key]
                if entry_price is None:
                    continue

                won = bet_dir == w["resolution"]
                trades.append({
                    "won": won, "entry_price": entry_price,
                    "direction": bet_dir, "resolution": w["resolution"],
                    "day": w["day"], "time": w["time"],
                    "position_size": 20.0,
                })

            if not trades:
                continue
            r = simulate_trades(trades)
            print(f"{streak_req:>6} | {entry_label:>5} | {len(r['trades']):>6} | "
                  f"{r['wins']:>4} | {r['losses']:>4} | {r['win_rate']:>4.0%} | "
                  f"${r['avg_entry']:>.4f} | ${r['total_pnl']:>+8.2f} | "
                  f"${r['max_dd']:>6.2f} | {r['max_consec']:>3}")

    print()

    # ======================================================================
    # STRATEGY 3: Fade Extreme In-Window Move — buy the cheap token
    # ======================================================================
    print("=" * 85)
    print("STRATEGY 3: FADE EXTREME MOVE — When BTC moves >Xbps in first 10min,")
    print("            buy the CHEAP opposing token at T-60s or T-120s")
    print("=" * 85)

    hdr2 = (f"{'MinMove':>7} | {'Entry':>5} | {'MaxP':>5} | {'Trades':>6} | {'Wins':>4} | "
            f"{'Loss':>4} | {'WR':>5} | {'AvgEnt':>7} | {'P/L':>9} | {'MaxDD':>7}")
    print(hdr2)
    print("-" * len(hdr2))

    for min_move_bps in [10, 20, 30, 50, 75, 100]:
        min_move = min_move_bps / 10000
        for entry_label in ["T120", "T60"]:
            for max_entry in [0.30, 0.50, 1.00]:
                trades = []
                for w in windows:
                    btc_move = w.get("btc_10min_move")
                    if btc_move is None or abs(btc_move) < min_move:
                        continue

                    # Bet AGAINST the move (fade)
                    if btc_move > 0:
                        bet_dir = "DOWN"  # BTC went up, bet it reverses
                    else:
                        bet_dir = "UP"

                    up_key = f"up_{entry_label}"
                    down_key = f"down_{entry_label}"
                    entry_price = w[up_key] if bet_dir == "UP" else w[down_key]
                    if entry_price is None or entry_price > max_entry:
                        continue

                    won = bet_dir == w["resolution"]
                    trades.append({
                        "won": won, "entry_price": entry_price,
                        "direction": bet_dir, "resolution": w["resolution"],
                        "day": w["day"], "time": w["time"],
                        "position_size": 20.0,
                    })

                if len(trades) < 3:
                    continue
                r = simulate_trades(trades)
                print(f"{min_move_bps:>5}bp | {entry_label:>5} | ${max_entry:.2f} | "
                      f"{len(r['trades']):>6} | {r['wins']:>4} | {r['losses']:>4} | "
                      f"{r['win_rate']:>4.0%} | ${r['avg_entry']:>.4f} | "
                      f"${r['total_pnl']:>+8.2f} | ${r['max_dd']:>6.2f}")

    print()

    # ======================================================================
    # STRATEGY 4: Trend + Momentum Confirmation
    # ======================================================================
    print("=" * 85)
    print("STRATEGY 4: TREND + MOMENTUM — Streak continuation + momentum confirms")
    print("=" * 85)

    hdr3 = (f"{'Streak':>6} | {'Entry':>5} | {'MomW':>4} | {'Thresh':>6} | {'Trades':>6} | "
            f"{'Wins':>4} | {'Loss':>4} | {'WR':>5} | {'AvgEnt':>7} | {'P/L':>9} | {'MaxDD':>7}")
    print(hdr3)
    print("-" * len(hdr3))

    for streak_req in [1, 2, 3]:
        for entry_label in ["T300", "T180"]:
            for mom_window in ["m120"]:
                for thresh in [0.0003, 0.0005, 0.001]:
                    trades = []
                    for i in range(streak_req, len(windows)):
                        streak_dir = windows[i - 1]["resolution"]
                        all_same = all(windows[i - j - 1]["resolution"] == streak_dir
                                       for j in range(streak_req))
                        if not all_same:
                            continue

                        w = windows[i]
                        mkey = f"{mom_window}_{entry_label}"
                        m = w.get(mkey)
                        if m is None:
                            continue

                        # Momentum must agree with streak direction
                        mom_dir = "UP" if m > 0 else "DOWN"
                        if mom_dir != streak_dir or abs(m) < thresh:
                            continue

                        bet_dir = streak_dir
                        up_key = f"up_{entry_label}"
                        down_key = f"down_{entry_label}"
                        entry_price = w[up_key] if bet_dir == "UP" else w[down_key]
                        if entry_price is None:
                            continue

                        won = bet_dir == w["resolution"]
                        trades.append({
                            "won": won, "entry_price": entry_price,
                            "direction": bet_dir, "resolution": w["resolution"],
                            "day": w["day"], "time": w["time"],
                            "position_size": 20.0,
                            "momentum": m,
                        })

                    if len(trades) < 5:
                        continue
                    r = simulate_trades(trades)
                    print(f"{streak_req:>6} | {entry_label:>5} | {mom_window:>4} | "
                          f"{thresh*10000:>4.0f}bp | {len(r['trades']):>6} | "
                          f"{r['wins']:>4} | {r['losses']:>4} | {r['win_rate']:>4.0%} | "
                          f"${r['avg_entry']:>.4f} | ${r['total_pnl']:>+8.2f} | "
                          f"${r['max_dd']:>6.2f}")

    print()

    # ======================================================================
    # FIND OVERALL BEST — across all strategies
    # ======================================================================
    print("=" * 85)
    print("OVERALL BEST CONFIGS (min 10 trades, positive P/L)")
    print("=" * 85)

    all_configs = []

    # Re-run all strategies and collect results
    # Strategy 1: Trend
    for streak_req in [1, 2, 3, 4]:
        for entry_label in ["T300", "T180", "T120", "T60"]:
            trades = []
            for i in range(streak_req, len(windows)):
                streak_dir = windows[i - 1]["resolution"]
                all_same = all(windows[i - j - 1]["resolution"] == streak_dir
                               for j in range(streak_req))
                if not all_same:
                    continue
                bet_dir = streak_dir
                w = windows[i]
                entry_price = w[f"up_{entry_label}"] if bet_dir == "UP" else w[f"down_{entry_label}"]
                if entry_price is None:
                    continue
                won = bet_dir == w["resolution"]
                trades.append({"won": won, "entry_price": entry_price, "direction": bet_dir,
                               "resolution": w["resolution"], "day": w["day"], "time": w["time"],
                               "position_size": 20.0})
            if len(trades) >= 10:
                r = simulate_trades(trades)
                if r["total_pnl"] > 0:
                    all_configs.append((f"TREND streak={streak_req} {entry_label}", r))

    # Strategy 2: Contrarian
    for streak_req in [1, 2, 3, 4]:
        for entry_label in ["T300", "T180", "T120", "T60"]:
            trades = []
            for i in range(streak_req, len(windows)):
                streak_dir = windows[i - 1]["resolution"]
                all_same = all(windows[i - j - 1]["resolution"] == streak_dir
                               for j in range(streak_req))
                if not all_same:
                    continue
                bet_dir = "DOWN" if streak_dir == "UP" else "UP"
                w = windows[i]
                entry_price = w[f"up_{entry_label}"] if bet_dir == "UP" else w[f"down_{entry_label}"]
                if entry_price is None:
                    continue
                won = bet_dir == w["resolution"]
                trades.append({"won": won, "entry_price": entry_price, "direction": bet_dir,
                               "resolution": w["resolution"], "day": w["day"], "time": w["time"],
                               "position_size": 20.0})
            if len(trades) >= 10:
                r = simulate_trades(trades)
                if r["total_pnl"] > 0:
                    all_configs.append((f"CONTRA streak={streak_req} {entry_label}", r))

    # Strategy 3: Fade
    for min_move_bps in [10, 20, 30, 50, 75, 100]:
        min_move = min_move_bps / 10000
        for entry_label in ["T120", "T60"]:
            for max_entry in [0.30, 0.50, 1.00]:
                trades = []
                for w in windows:
                    btc_move = w.get("btc_10min_move")
                    if btc_move is None or abs(btc_move) < min_move:
                        continue
                    bet_dir = "DOWN" if btc_move > 0 else "UP"
                    entry_price = w[f"up_{entry_label}"] if bet_dir == "UP" else w[f"down_{entry_label}"]
                    if entry_price is None or entry_price > max_entry:
                        continue
                    won = bet_dir == w["resolution"]
                    trades.append({"won": won, "entry_price": entry_price, "direction": bet_dir,
                                   "resolution": w["resolution"], "day": w["day"], "time": w["time"],
                                   "position_size": 20.0})
                if len(trades) >= 10:
                    r = simulate_trades(trades)
                    if r["total_pnl"] > 0:
                        all_configs.append((f"FADE {min_move_bps}bp {entry_label} max${max_entry:.2f}", r))

    # Strategy 4: Trend + Momentum
    for streak_req in [1, 2, 3]:
        for entry_label in ["T300", "T180"]:
            for thresh in [0.0003, 0.0005, 0.001]:
                trades = []
                for i in range(streak_req, len(windows)):
                    streak_dir = windows[i - 1]["resolution"]
                    all_same = all(windows[i - j - 1]["resolution"] == streak_dir
                                   for j in range(streak_req))
                    if not all_same:
                        continue
                    w = windows[i]
                    m = w.get(f"m120_{entry_label}")
                    if m is None or abs(m) < thresh:
                        continue
                    mom_dir = "UP" if m > 0 else "DOWN"
                    if mom_dir != streak_dir:
                        continue
                    bet_dir = streak_dir
                    entry_price = w[f"up_{entry_label}"] if bet_dir == "UP" else w[f"down_{entry_label}"]
                    if entry_price is None:
                        continue
                    won = bet_dir == w["resolution"]
                    trades.append({"won": won, "entry_price": entry_price, "direction": bet_dir,
                                   "resolution": w["resolution"], "day": w["day"], "time": w["time"],
                                   "position_size": 20.0, "momentum": m})
                if len(trades) >= 10:
                    r = simulate_trades(trades)
                    if r["total_pnl"] > 0:
                        all_configs.append((f"TREND+MOM s={streak_req} {entry_label} {thresh*10000:.0f}bp", r))

    # Sort by P/L
    all_configs.sort(key=lambda x: x[1]["total_pnl"], reverse=True)

    print(f"{'Strategy':<40} | {'Trades':>6} | {'WR':>5} | {'AvgEnt':>7} | "
          f"{'P/L':>9} | {'MaxDD':>7} | {'P/L per trade':>12}")
    print("-" * 100)

    for name, r in all_configs[:15]:
        pnl_per = r["total_pnl"] / len(r["trades"]) if r["trades"] else 0
        print(f"{name:<40} | {len(r['trades']):>6} | {r['win_rate']:>4.0%} | "
              f"${r['avg_entry']:>.4f} | ${r['total_pnl']:>+8.2f} | "
              f"${r['max_dd']:>6.2f} | ${pnl_per:>+11.2f}")

    # Deep dive on #1
    if all_configs:
        best_name, best_r = all_configs[0]
        print()
        print("=" * 85)
        print(f"BEST: {best_name}")
        print("=" * 85)
        print(f"Trades: {len(best_r['trades'])} ({best_r['wins']}W-{best_r['losses']}L)")
        print(f"Win Rate: {best_r['win_rate']:.1%}")
        print(f"Total P/L: ${best_r['total_pnl']:+.2f}")
        print(f"Avg Entry: ${best_r['avg_entry']:.4f}")
        print(f"Max Drawdown: ${best_r['max_dd']:.2f}")
        print(f"Max Consec Losses: {best_r['max_consec']}")
        print()
        print("Daily P/L:")
        winning_days = 0
        for day in sorted(best_r["daily_pnl"].keys()):
            pnl = best_r["daily_pnl"][day]
            if pnl > 0:
                winning_days += 1
            print(f"  {day}: ${pnl:+.2f}")
        total_days = len(best_r["daily_pnl"])
        print(f"  Day Win Rate: {winning_days}/{total_days} ({winning_days/total_days:.0%})")
        print()
        print("Trade Details:")
        for t in best_r["trades"]:
            res = "WIN " if t["won"] else "LOSS"
            mom_str = f" mom={t['momentum']:+.5%}" if "momentum" in t else ""
            print(f"  {t['time']} | {t['direction']:>4} | entry=${t['entry_price']:.4f} | "
                  f"{res} | P/L=${t['pnl']:+.2f} | bal=${t['balance']:.2f}{mom_str}")

    print()
    print("=" * 85)
    print("CONCLUSIONS")
    print("=" * 85)

    if not all_configs:
        print("- NO PROFITABLE CONFIGS found with >= 10 trades")
        print("- These 15-min binary markets may be too efficient to trade with simple signals")
    else:
        best_name, best_r = all_configs[0]
        pnl_per = best_r["total_pnl"] / len(best_r["trades"])
        print(f"- Best strategy: {best_name}")
        print(f"- {best_r['win_rate']:.0%} win rate, ${pnl_per:+.2f}/trade, "
              f"${best_r['total_pnl']:+.2f} over 7 days")

        # Check if P/L is dominated by outliers
        sorted_pnl = sorted([t["pnl"] for t in best_r["trades"]], reverse=True)
        top3_pnl = sum(sorted_pnl[:3])
        if top3_pnl > best_r["total_pnl"] * 0.8:
            print(f"  WARNING: Top 3 trades account for ${top3_pnl:.2f} of ${best_r['total_pnl']:.2f} "
                  f"({top3_pnl/best_r['total_pnl']:.0%}) — edge may be luck-driven")
        else:
            print(f"  Top 3 trades: ${top3_pnl:.2f} ({top3_pnl/best_r['total_pnl']:.0%} of total) — "
                  f"edge is distributed, not luck-driven")


if __name__ == "__main__":
    main()
