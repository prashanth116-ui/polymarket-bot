"""Full CryptoTrader V2 backtest — no price filter + earlier entry testing."""

import time
from datetime import datetime, timezone

from runners.backtest_crypto import (
    fetch_btc_candles,
    fetch_window_resolution,
    compute_momentum,
    get_token_prices_at_entry,
)


def crypto_fee_rate(price):
    return 0.25 * (price * (1 - price)) ** 2


def simulate(windows, momentum_key, price_up_key, price_down_key, threshold,
             min_price=0.0, max_price=1.0, base_size=20.0, bankroll=200.0):
    balance = bankroll
    trades = []
    consec_losses = 0
    max_consec = 0
    peak = balance
    max_dd = 0
    daily_pnl = {}

    for w in windows:
        m = w.get(momentum_key)
        if m is None or abs(m) < threshold:
            continue
        direction = "UP" if m > 0 else "DOWN"
        entry_price = w[price_up_key] if direction == "UP" else w[price_down_key]
        if entry_price is None:
            continue
        if entry_price < min_price or entry_price > max_price:
            continue

        fee = crypto_fee_rate(entry_price)
        mom_str = abs(m) / threshold
        size_mult = min(mom_str, 2.0)
        position_size = base_size * size_mult
        shares = position_size / (entry_price + fee)
        cost = shares * (entry_price + fee)

        won = direction == w["resolution"]
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

        day = datetime.fromtimestamp(w["window_ts"], tz=timezone.utc).strftime("%Y-%m-%d")
        daily_pnl[day] = daily_pnl.get(day, 0) + pnl

        trades.append({
            "direction": direction, "resolution": w["resolution"],
            "entry_price": entry_price, "momentum": m, "won": won,
            "pnl": pnl, "cost": cost, "balance": balance, "window_ts": w["window_ts"],
        })

    wins = sum(1 for t in trades if t["won"])
    losses = len(trades) - wins
    total_pnl = balance - bankroll
    return {
        "trades": trades, "wins": wins, "losses": losses,
        "win_rate": wins / len(trades) if trades else 0,
        "total_pnl": total_pnl,
        "max_dd": max_dd, "max_consec": max_consec,
        "daily_pnl": daily_pnl,
        "avg_entry": sum(t["entry_price"] for t in trades) / len(trades) if trades else 0,
    }


def main():
    interval_secs = 900
    end_ts = time.time() - 900
    start_ts = end_ts - (7 * 86400)

    print("=== CryptoTrader V2 — Full Backtest ===")
    print(f"Period: {datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC "
          f"to {datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print()

    # Fetch data
    print("Fetching BTC/USD 1-min candles...")
    candles = fetch_btc_candles(start_ts - 600, end_ts + 300)
    print(f"  {len(candles)} candles")

    print("Fetching window resolutions...")
    windows = []
    window_ts = int(start_ts // interval_secs) * interval_secs
    while window_ts < end_ts:
        slug = f"btc-updown-15m-{window_ts}"
        resolution = fetch_window_resolution(slug)
        if resolution:
            w = {"resolution": resolution, "window_ts": window_ts}
            # Compute at multiple entry offsets
            for label, offset in [("T60", 840), ("T120", 780), ("T180", 720), ("T300", 600)]:
                entry_ts = window_ts + offset
                w[f"m30_{label}"] = compute_momentum(candles, entry_ts, 30)
                w[f"m60_{label}"] = compute_momentum(candles, entry_ts, 60)
                w[f"m120_{label}"] = compute_momentum(candles, entry_ts, 120)
                up_p, down_p = get_token_prices_at_entry(candles, window_ts, entry_offset=offset)
                w[f"up_{label}"] = up_p
                w[f"down_{label}"] = down_p
            windows.append(w)
        window_ts += interval_secs
        time.sleep(0.15)

    up_ct = sum(1 for w in windows if w["resolution"] == "UP")
    down_ct = len(windows) - up_ct
    print(f"  {len(windows)} resolved windows ({up_ct} UP, {down_ct} DOWN)")
    print()

    # ========== SIMULATION 1: NO PRICE FILTER ==========
    hdr = f"{'Entry':>5} | {'Mom':>4} | {'Thresh':>6} | {'Trades':>6} | {'Wins':>4} | {'Loss':>4} | {'WR':>5} | {'AvgEnt':>7} | {'P/L':>9} | {'MaxDD':>7} | {'CL':>3}"
    sep = "-" * len(hdr)

    print("=" * len(hdr))
    print("SIMULATION 1: NO PRICE FILTER ($0.00 - $1.00)")
    print("=" * len(hdr))
    print(hdr)
    print(sep)

    for offset_label in ["T60", "T120", "T180", "T300"]:
        for mom_window in ["m30", "m120"]:
            for thresh in [0.0005, 0.001, 0.0015, 0.002, 0.003]:
                mkey = f"{mom_window}_{offset_label}"
                up_key = f"up_{offset_label}"
                down_key = f"down_{offset_label}"
                r = simulate(windows, mkey, up_key, down_key, thresh)
                if r["trades"]:
                    print(f"{offset_label:>5} | {mom_window:>4} | {thresh*10000:>4.0f}bp | "
                          f"{len(r['trades']):>6} | {r['wins']:>4} | {r['losses']:>4} | "
                          f"{r['win_rate']:>4.0%} | ${r['avg_entry']:>.4f} | "
                          f"${r['total_pnl']:>+8.2f} | ${r['max_dd']:>6.2f} | {r['max_consec']:>3}")
        print(sep)

    print()

    # ========== SIMULATION 2: WIDER PRICE FILTER ==========
    print("=" * len(hdr))
    print("SIMULATION 2: PRICE FILTER $0.05 - $0.85")
    print("=" * len(hdr))
    print(hdr)
    print(sep)

    for offset_label in ["T60", "T120", "T180", "T300"]:
        for mom_window in ["m30", "m120"]:
            for thresh in [0.0005, 0.001, 0.0015, 0.002, 0.003]:
                mkey = f"{mom_window}_{offset_label}"
                up_key = f"up_{offset_label}"
                down_key = f"down_{offset_label}"
                r = simulate(windows, mkey, up_key, down_key, thresh,
                             min_price=0.05, max_price=0.85)
                if r["trades"]:
                    print(f"{offset_label:>5} | {mom_window:>4} | {thresh*10000:>4.0f}bp | "
                          f"{len(r['trades']):>6} | {r['wins']:>4} | {r['losses']:>4} | "
                          f"{r['win_rate']:>4.0%} | ${r['avg_entry']:>.4f} | "
                          f"${r['total_pnl']:>+8.2f} | ${r['max_dd']:>6.2f} | {r['max_consec']:>3}")
        print(sep)

    print()

    # ========== FIND BEST CONFIG ==========
    best_pnl = -9999
    best_config = None
    best_result = None
    all_results = []

    for offset_label in ["T60", "T120", "T180", "T300"]:
        for mom_window in ["m30", "m60", "m120"]:
            for thresh in [0.0005, 0.001, 0.0015, 0.002, 0.003]:
                for max_p in [0.55, 0.70, 0.85, 1.0]:
                    mkey = f"{mom_window}_{offset_label}"
                    up_key = f"up_{offset_label}"
                    down_key = f"down_{offset_label}"
                    r = simulate(windows, mkey, up_key, down_key, thresh,
                                 min_price=0.05, max_price=max_p)
                    if len(r["trades"]) >= 5:
                        all_results.append((offset_label, mom_window, thresh, max_p, r))
                        if r["total_pnl"] > best_pnl:
                            best_pnl = r["total_pnl"]
                            best_config = (offset_label, mom_window, thresh, max_p)
                            best_result = r

    # Top 10 configs by P/L
    all_results.sort(key=lambda x: x[4]["total_pnl"], reverse=True)
    print("=" * len(hdr))
    print("TOP 10 CONFIGS (min 5 trades)")
    print("=" * len(hdr))
    print(f"{'Entry':>5} | {'Mom':>4} | {'Thresh':>6} | {'MaxP':>5} | {'Trades':>6} | {'WR':>5} | {'P/L':>9} | {'MaxDD':>7}")
    print("-" * 70)
    for offset, mom, thresh, maxp, r in all_results[:10]:
        print(f"{offset:>5} | {mom:>4} | {thresh*10000:>4.0f}bp | ${maxp:.2f} | "
              f"{len(r['trades']):>6} | {r['win_rate']:>4.0%} | "
              f"${r['total_pnl']:>+8.2f} | ${r['max_dd']:>6.2f}")

    print()

    # Deep dive on best config
    if best_config and best_result:
        print("=" * len(hdr))
        print(f"BEST CONFIG: Entry={best_config[0]}, Mom={best_config[1]}, "
              f"Threshold={best_config[2]*10000:.0f}bps, MaxPrice=${best_config[3]:.2f}")
        print("=" * len(hdr))
        r = best_result
        print(f"Trades: {len(r['trades'])} ({r['wins']}W-{r['losses']}L)")
        print(f"Win Rate: {r['win_rate']:.1%}")
        print(f"Total P/L: ${r['total_pnl']:+.2f}")
        print(f"Avg Entry Price: ${r['avg_entry']:.4f}")
        print(f"Max Drawdown: ${r['max_dd']:.2f}")
        print(f"Max Consecutive Losses: {r['max_consec']}")
        print()
        print("Daily P/L:")
        for day in sorted(r["daily_pnl"].keys()):
            pnl = r["daily_pnl"][day]
            marker = " ***" if abs(pnl) > 50 else ""
            print(f"  {day}: ${pnl:+.2f}{marker}")
        print()
        print("Trade Details:")
        for t in r["trades"]:
            dt = datetime.fromtimestamp(t["window_ts"], tz=timezone.utc).strftime("%m/%d %H:%M")
            res = "WIN " if t["won"] else "LOSS"
            print(f"  {dt} | {t['direction']:>4} | entry=${t['entry_price']:.4f} | "
                  f"mom={t['momentum']:+.5%} | {res} | P/L=${t['pnl']:+.2f} | "
                  f"bal=${t['balance']:.2f}")

    print()
    print("=" * len(hdr))
    print("RECOMMENDATIONS")
    print("=" * len(hdr))

    if best_result:
        if best_result["win_rate"] < 0.55:
            print("- STRATEGY IS NOT VIABLE: Win rate below 55% across all configs")
            print("  Momentum at T-60s through T-300s is too weak a predictor")
            print("  Consider: different signal (order book imbalance, volume spike, etc.)")
        elif best_result["win_rate"] < 0.65:
            print("- MARGINAL EDGE: Win rate 55-65% — profitable but fragile")
            print(f"  Best config: {best_config[0]} entry, {best_config[1]} momentum, "
                  f"{best_config[2]*10000:.0f}bps threshold")
        else:
            print(f"- VIABLE EDGE: {best_result['win_rate']:.0%} win rate")
            print(f"  Recommended: {best_config[0]} entry, {best_config[1]} momentum, "
                  f"{best_config[2]*10000:.0f}bps threshold, max price ${best_config[3]:.2f}")

        if best_config[0] != "T60":
            print(f"- ENTER EARLIER: {best_config[0]} beats T-60s — prices are better earlier")

        if best_config[1] != "m30":
            print(f"- USE LONGER MOMENTUM: {best_config[1]} beats m30 — more signal in longer window")


if __name__ == "__main__":
    main()
