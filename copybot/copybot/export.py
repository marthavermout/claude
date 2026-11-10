"""Write dashboard.json (+ the dashboard page) into the data directory.

The page reads dashboard.json and fetches live prices from Hyperliquid in the
browser, so open positions show live profit/loss between bot updates."""

from __future__ import annotations

import csv
import json
import shutil
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .config import BotConfig, load_overrides
from .review import build_review

PAGE = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"
MAX_POINTS = 720


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def _downsample(rows: list[dict], n: int) -> list[dict]:
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    picked = [rows[int(i * step)] for i in range(n - 1)]
    return picked + [rows[-1]]


def build_dashboard(cfg: BotConfig) -> dict:
    d = Path(cfg.data_dir)
    state = json.loads((d / "state.json").read_text()) if (d / "state.json").exists() else {}
    ov = load_overrides(d)
    equity = _rows(d / "equity.csv")
    trades = _rows(d / "trades.csv")
    review = build_review(d, active_leaders=cfg.leaders)
    broker = state.get("broker", {"starting_balance": cfg.starting_balance, "realized_pnl": 0.0,
                                  "fees_paid": 0.0, "positions": {}})
    risk = state.get("risk", {})
    last = equity[-1] if equity else {}

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "last_tick": last.get("time"),
        "poll_seconds": cfg.poll_seconds,
        "starting_balance": broker["starting_balance"],
        "realized_pnl": broker["realized_pnl"],
        "fees_paid": broker["fees_paid"],
        "equity": float(last["equity"]) if last else broker["starting_balance"],
        "halt": last.get("halt") or None,
        "killed": risk.get("killed", False),
        "kill_reason": risk.get("kill_reason", ""),
        "peak_equity": risk.get("peak_equity", 0.0),
        "positions": [{"coin": c, **p, **({"why": state["pattern_trades"][c]["pattern"]} if c in state.get("pattern_trades", {}) else {})}
                      for c, p in sorted(broker["positions"].items())],
        "risk": asdict(cfg.risk),
        "leaders": [asdict(s) for s in review.leaders],
        "following": list(cfg.leaders),
        "stats": {
            "hours": review.hours,
            "total_return": review.total_return,
            "btc_return": review.btc_return,
            "max_drawdown": review.max_drawdown,
            "trades": review.trades,
            "trades_per_day": review.trades_per_day,
            "churn_ratio": review.churn_ratio,
            "realized": review.realized,
            "fees": review.fees,
        },
        "series": [{"t": r["time"], "equity": float(r["equity"]), "btc": float(r["btc"]) if r.get("btc") else None}
                   for r in _downsample(equity, MAX_POINTS)],
        "trades": [{"t": t["time"], "coin": t["coin"], "size": float(t["size"]), "price": float(t["price"]),
                    "fee": float(t["fee"]), "realized": float(t["realized_pnl"])} for t in trades[-60:]][::-1],
        "reviews": ov.get("review_log", [])[-15:][::-1],
        "changes": ov.get("history", [])[::-1],
    }


def export(cfg: BotConfig) -> Path:
    d = Path(cfg.data_dir)
    d.mkdir(parents=True, exist_ok=True)
    out = d / "dashboard.json"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(build_dashboard(cfg), separators=(",", ":")))
    tmp.replace(out)
    for name in ("index.html", "manifest.json", "icon-180.png", "icon-512.png"):
        src = PAGE.parent / name
        if src.exists():
            shutil.copyfile(src, d / name)
    (d / ".nojekyll").touch()  # GitHub Pages: serve files as-is
    return out


def bot_summary(name: str, description: str, cfg: BotConfig) -> dict:
    """Compact numbers for the bot comparison ('Bots' tab)."""
    full = build_dashboard(cfg)
    st = full["stats"]
    return {
        "name": name,
        "description": description,
        "equity": full["equity"],
        "starting_balance": full["starting_balance"],
        "realized_pnl": full["realized_pnl"],
        "fees_paid": full["fees_paid"],
        "positions": full["positions"],
        "trades": st["trades"],
        "max_drawdown": st["max_drawdown"],
        "btc_return": st["btc_return"],
        "following": len(full["following"]),
        "last_tick": full["last_tick"],
        "halt": full["halt"],
        "killed": full["killed"],
        "series": [{"t": p["t"], "equity": p["equity"]} for p in _downsample(full["series"], 240)],
    }


def export_bots(bots: list[tuple[str, str, BotConfig]], out: str | Path) -> Path:
    data = {"generated_at": datetime.now(timezone.utc).isoformat(),
            "bots": [bot_summary(n, desc, cfg) for n, desc, cfg in bots]}
    out = Path(out)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    tmp.replace(out)
    return out
