"""Command line: select | run | status | review | tune | reset-kill"""

from __future__ import annotations

import argparse
import logging
import sys

import json

from .backtest import apply_sets, load_records, split_backtest
from .config import apply_overrides, load_config
from .engine import Engine
from .export import export, export_bots
from .hyperliquid import HyperliquidClient
from .review import build_review, to_markdown
from .selector import SelectionRules, select
from .tune import TuneRules, tune


def _cfg(args):
    cfg = load_config(args.config)
    if args.data_dir:
        cfg.data_dir = args.data_dir
    return apply_overrides(cfg)


def _selector_for(randomize: bool):
    def pick(exclude: set[str], n: int) -> list[str]:
        picks = select(HyperliquidClient(), SelectionRules(), top=n + len(exclude), randomize=randomize)
        return [c.address for c in picks if c.address not in exclude][:n]
    return pick


def cmd_select(args) -> int:
    rules = SelectionRules(max_leverage=args.max_leverage)
    print("Loading leaderboard (~40MB, takes a few seconds)...", file=sys.stderr)
    picks = select(HyperliquidClient(), rules, top=args.top)
    if not picks:
        print("No traders passed the filters.")
        return 1
    print(f"{'address':44} {'account':>12} {'week':>7} {'month':>7} {'allTime':>8} {'turnover':>8} {'lev':>5} {'fills24h':>8}")
    for c in picks:
        print(f"{c.address:44} {c.account_value:12,.0f} {c.week_roi:7.1%} {c.month_roi:7.1%} "
              f"{c.alltime_roi:8.1%} {c.turnover:8.1f} {c.leverage:5.2f} {c.fills_24h or 0:8d}")
    print("\nPaste into config.toml under [bot] (2-4 traders spreads the risk):")
    print("leaders = [\n" + "".join(f'  "{c.address}",\n' for c in picks[:3]) + "]")
    return 0


def cmd_run(args) -> int:
    cfg = _cfg(args)
    if not cfg.leaders and cfg.strategy == "copy":
        print("No leaders configured. Run `select` and add them to the config, or run `tune` to pick automatically.")
        return 1
    engine = Engine(cfg)
    if args.once:
        engine.tick()
    else:
        engine.run(minutes=args.minutes)
    return 0


def cmd_review(args) -> int:
    cfg = _cfg(args)
    r = build_review(cfg.data_dir, active_leaders=cfg.leaders, since_days=args.days)
    print(to_markdown(r, f"Laatste {args.days:g} dagen" if args.days else "Sinds de start"))
    return 0


def cmd_tune(args) -> int:
    cfg = _cfg(args)
    d = tune(cfg, _selector_for(cfg.select_random), rules=TuneRules(target_leaders=cfg.target_leaders), if_due_hours=args.if_due)
    if d is None:
        print("Review not due yet.")
        return 0
    for m in d.mistakes:
        print("MISTAKE:", m)
    for o in d.observations:
        print("NOTE:   ", o)
    print("DECISION:", d.change or d.skipped_reason or "no change")
    return 0


def cmd_status(args) -> int:
    engine = Engine(_cfg(args))
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


def cmd_backtest(args) -> int:
    cfg = apply_sets(_cfg(args), args.set or [])
    records = load_records(cfg.data_dir if not args.replay_dir else args.replay_dir, args.start, args.end)
    result = split_backtest(cfg, records, args.split)
    print(json.dumps(result, indent=2))
    return 0 if "error" not in result else 1


def cmd_export(args) -> int:
    print(export(_cfg(args)))
    return 0


def _description(path: str) -> str:
    with open(path) as fh:
        first = fh.readline().strip()
    return first.split(":", 1)[1].strip() if first.startswith("# Bot") and ":" in first else ""


def cmd_compare(args) -> int:
    bots = []
    for spec in args.bot:
        name, _, rest = spec.partition("=")
        config, _, data_dir = rest.partition(":")
        cfg = load_config(config)
        cfg.data_dir = data_dir
        bots.append((name, _description(config) or "standaard-instellingen", apply_overrides(cfg)))
    print(export_bots(bots, args.out))
    return 0


def cmd_reset_kill(args) -> int:
    engine = Engine(_cfg(args))
    engine.risk.state.killed = False
    engine.risk.state.kill_reason = ""
    engine.risk.state.peak_equity = 0.0
    engine.save()
    print("Kill switch reset. Peak equity will restart from current equity.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="copybot", description="Paper copytrading bot for Hyperliquid")
    parser.add_argument("-c", "--config", default="config.toml")
    parser.add_argument("-d", "--data-dir", help="override [bot].data_dir")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("select", help="find traders worth copying")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--max-leverage", type=float, default=5.0)
    p.set_defaults(func=cmd_select)

    p = sub.add_parser("run", help="start paper trading")
    p.add_argument("--once", action="store_true", help="run a single round and exit")
    p.add_argument("--minutes", type=float, help="stop after this many minutes (for scheduled runs)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("review", help="show what went well and what went wrong")
    p.add_argument("--days", type=float, help="only the last N days")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("tune", help="review, find mistakes, and apply at most one improvement")
    p.add_argument("--if-due", type=float, metavar="HOURS", help="skip if the last review was less than HOURS ago")
    p.set_defaults(func=cmd_tune)

    sub.add_parser("status", help="show results").set_defaults(func=cmd_status)
    p = sub.add_parser("backtest", help="replay recorded history to test a change (in-sample vs out-of-sample)")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", help="override a setting, e.g. risk.min_trade_usd=30")
    p.add_argument("--split", type=float, default=0.7, help="fraction of history used as in-sample (default 0.7)")
    p.add_argument("--start", help="ISO time to start from")
    p.add_argument("--end", help="ISO time to stop at")
    p.add_argument("--replay-dir", help="data dir holding replay/ (default: the data dir)")
    p.set_defaults(func=cmd_backtest)

    sub.add_parser("export", help="write dashboard.json + dashboard page into the data dir").set_defaults(func=cmd_export)
    p = sub.add_parser("compare", help="write bots.json comparing several paper bots")
    p.add_argument("--bot", action="append", required=True, metavar="NAME=CONFIG:DATADIR")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_compare)

    sub.add_parser("reset-kill", help="resume after the drawdown kill switch").set_defaults(func=cmd_reset_kill)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
