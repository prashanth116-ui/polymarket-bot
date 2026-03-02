"""CLI market watcher — discover and inspect Polymarket markets.

Usage:
    python -m runners.market_monitor                    # Top 20 by volume
    python -m runners.market_monitor --search "trump"   # Search for markets
    python -m runners.market_monitor --trending         # Trending markets
    python -m runners.market_monitor --spreads          # Wide-spread markets (MM opportunities)
    python -m runners.market_monitor --near             # Markets resolving soon
    python -m runners.market_monitor --book TOKEN_ID    # Show order book for a token
"""

import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.market_scanner import MarketScanner
from data.clob_client import ClobReader


def format_price(p: float) -> str:
    return f"${p:.2f}" if p > 0.01 else f"${p:.3f}"


def print_markets(markets, title: str = "Markets"):
    if not markets:
        print(f"No markets found.")
        return

    print(f"\n{'=' * 90}")
    print(f" {title} ({len(markets)} results)")
    print(f"{'=' * 90}")
    print(f"{'#':>3}  {'YES':>6}  {'NO':>6}  {'Spread':>7}  {'Vol 24h':>10}  {'Liquidity':>10}  Question")
    print(f"{'-' * 90}")

    for i, m in enumerate(markets, 1):
        vol = f"${m.volume:,.0f}" if hasattr(m, 'volume') else "—"
        liq = f"${m.liquidity:,.0f}" if hasattr(m, 'liquidity') else "—"

        # Get 24h volume from raw data if available
        question = m.question[:50] + "..." if len(m.question) > 50 else m.question

        print(
            f"{i:>3}  "
            f"{format_price(m.last_price_yes):>6}  "
            f"{format_price(m.last_price_no):>6}  "
            f"{m.spread_yes:>6.1%}  "
            f"{vol:>10}  "
            f"{liq:>10}  "
            f"{question}"
        )

        # Show token IDs if verbose
        if hasattr(m, '_verbose') and m._verbose:
            print(f"     YES: {m.yes_token_id[:30]}...")
            print(f"     NO:  {m.no_token_id[:30]}...")

    print()


def print_book(token_id: str, depth: int = 10):
    reader = ClobReader()
    book = reader.get_order_book(token_id)
    if not book:
        print(f"Could not fetch book for {token_id[:20]}...")
        return

    print(f"\nOrder Book for {token_id[:30]}...")
    print(f"Best Bid: {format_price(book.best_bid)}  |  Best Ask: {format_price(book.best_ask)}  |  Spread: {book.spread:.3f}  |  Mid: {format_price(book.midpoint)}")
    print()

    # Print side by side
    top_bids = book.bids[:depth]
    top_asks = book.asks[:depth]
    max_levels = max(len(top_bids), len(top_asks))

    print(f"{'BIDS':^25}  |  {'ASKS':^25}")
    print(f"{'Price':>10} {'Size':>12}  |  {'Price':>10} {'Size':>12}")
    print(f"{'-' * 25}  |  {'-' * 25}")

    for i in range(max_levels):
        bid_str = ""
        ask_str = ""
        if i < len(top_bids):
            bid_str = f"{format_price(top_bids[i].price):>10} {top_bids[i].size:>12.2f}"
        if i < len(top_asks):
            ask_str = f"{format_price(top_asks[i].price):>10} {top_asks[i].size:>12.2f}"
        print(f"{bid_str:>25}  |  {ask_str:>25}")

    bid_total = sum(l.size for l in top_bids)
    ask_total = sum(l.size for l in top_asks)
    print(f"{'Total:':>10} {bid_total:>12.2f}  |  {'Total:':>10} {ask_total:>12.2f}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Polymarket Market Monitor")
    parser.add_argument("--search", "-s", type=str, help="Search markets by keyword")
    parser.add_argument("--trending", "-t", action="store_true", help="Show trending markets")
    parser.add_argument("--spreads", action="store_true", help="Show wide-spread markets (MM opportunities)")
    parser.add_argument("--near", action="store_true", help="Show markets resolving soon")
    parser.add_argument("--book", "-b", type=str, help="Show order book for a token ID")
    parser.add_argument("--top", "-n", type=int, default=20, help="Number of results (default: 20)")
    parser.add_argument("--min-volume", type=float, default=100, help="Min 24h volume (default: $100)")
    parser.add_argument("--min-liquidity", type=float, default=0, help="Min liquidity")
    args = parser.parse_args()

    if args.book:
        print_book(args.book)
        return

    scanner = MarketScanner()

    if args.search:
        markets = scanner.search(args.search, limit=args.top)
        print_markets(markets, f"Search: '{args.search}'")
    elif args.trending:
        markets = scanner.get_trending(limit=args.top)
        print_markets(markets, "Trending Markets (24h Volume)")
    elif args.spreads:
        markets = scanner.get_wide_spread(limit=args.top)
        print_markets(markets, "Wide Spread Markets (MM Opportunities)")
    elif args.near:
        markets = scanner.get_near_resolution(hours=72, limit=args.top)
        print_markets(markets, "Markets Resolving Within 72h")
    else:
        markets = scanner.scan(
            limit=args.top,
            min_volume_24h=args.min_volume,
            min_liquidity=args.min_liquidity,
        )
        print_markets(markets, "Active Markets")


if __name__ == "__main__":
    main()
