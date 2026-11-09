"""Command line: select | run | status | reset-kill"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import load_config
from .engine import Engine
from .hyperliquid import HyperliquidClient
from .selector import SelectionRules, select


def cmd_select(args) -> int:
    rules = SelectionRules(max_leverage=args.max_leverage)
    print("Loading leaderboard (~40MB, takes a few seconds)...", file=sys.stderr)
    picks = select(HyperliquidClient(), rules, top=args.top)
    if not picks:
        print("No traders passed the filters.")
        return 1
    print(f"{'address':44} {'account':>12} {'week':>7} {'month':>7} {'allTime':>8} {'turnover':>8} {'lev':>5}")
    for c in picks:
        print(f"{c.address:44} {c.account_value:12,.0f} {c.week_roi:7.1%} {c.month_roi:7.1%} "
              f"{c.alltime_roi:8.1%} {c.turnover:8.1f} {c.leverage:5.2f}")
    print("\nPaste into config.toml under [bot] (2-4 traders spreads the risk):")
    print("leaders = [\n" + "".join(f'  "{c.address}",\n' for c in picks[:3]) + "]")
    return 0


def cmd_run(args) -> int:
    engine = Engine(load_config(args.config))
    if args.once:
        engine.tick()
    else:
        engine.run()
    return 0


def cmd_status(args) -> int:
    cfg = load_config(args.config)
    engine = Engine(cfg)
    mids = engine.client.mids()
    b = engine.broker
    eq = b.equity(mids)
    print(f"Start balance : ${b.starting_balance:,.2f}")
    print(f"Equity now    : ${eq:,.2f}  ({eq / b.starting_balance - 1:+.2%})")
    print(f"Realized PnL  : ${b.realized_pnl:+,.2f}")
    print(f"Unrealized    : ${b.unrealized(mids):+,.2f}")
    print(f"Fees paid     : ${b.fees_paid:,.2f}")
    print(f"Leverage      : {b.gross_notional(mids) / eq if eq > 0 else 0:.2f}x")
    s = engine.risk.state
    print(f"Peak equity   : ${s.peak_equity:,.2f}")
    if s.killed:
        print(f"KILLED        : {s.kill_reason}")
    if b.positions:
        print("\nOpen positions:")
        for coin, p in sorted(b.positions.items()):
            px = mids.get(coin, p.entry)
            print(f"  {coin:8} {'LONG ' if p.size > 0 else 'SHORT'} {abs(p.size):>12g} "
                  f"entry {p.entry:>12.4f} now {px:>12.4f} pnl {(px - p.entry) * p.size:+10.2f}")
    return 0


def cmd_reset_kill(args) -> int:
    engine = Engine(load_config(args.config))
    engine.risk.state.killed = False
    engine.risk.state.kill_reason = ""
    engine.risk.state.peak_equity = 0.0
    engine.save()
    print("Kill switch reset. Peak equity will restart from current equity.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="copybot", description="Paper copytrading bot for Hyperliquid")
    parser.add_argument("-c", "--config", default="config.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("select", help="find traders worth copying")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--max-leverage", type=float, default=5.0)
    p.set_defaults(func=cmd_select)

    p = sub.add_parser("run", help="start paper trading")
    p.add_argument("--once", action="store_true", help="run a single round and exit")
    p.set_defaults(func=cmd_run)

    sub.add_parser("status", help="show results").set_defaults(func=cmd_status)
    sub.add_parser("reset-kill", help="resume after the drawdown kill switch").set_defaults(func=cmd_reset_kill)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
