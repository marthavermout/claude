"""Look back at what the bot did and find its mistakes.

Everything here is computed from the CSV logs, so a review can be re-run at any time."""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s)


def max_drawdown(values: list[float]) -> float:
    peak, worst = float("-inf"), 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, 1 - v / peak)
    return worst


@dataclass
class LeaderStats:
    address: str
    hours: float
    shadow_return: float
    leader_return: float
    trades: int
    closed_trades: int
    fees: float


@dataclass
class Review:
    hours: float = 0.0
    start_equity: float = 0.0
    end_equity: float = 0.0
    total_return: float = 0.0
    max_drawdown: float = 0.0
    btc_return: float | None = None
    trades: int = 0
    trades_per_day: float = 0.0
    fees: float = 0.0
    realized: float = 0.0
    fee_drag: float = 0.0  # fees / |result before fees| (closed AND open trades); >1 = fees ate more than the edge
    churn_trades: int = 0  # trades reversing the previous trade on that coin within an hour
    halt_days: list[str] = field(default_factory=list)
    coin_pnl: dict[str, float] = field(default_factory=dict)
    leaders: list[LeaderStats] = field(default_factory=list)

    @property
    def churn_ratio(self) -> float:
        return self.churn_trades / self.trades if self.trades else 0.0


def build_review(data_dir: str | Path, active_leaders: list[str] | None = None, since_days: float | None = None) -> Review:
    d = Path(data_dir)
    equity = _rows(d / "equity.csv")
    trades = _rows(d / "trades.csv")
    shadow_trades = _rows(d / "shadow_trades.csv")
    leader_rows = _rows(d / "leaders.csv")

    if since_days is not None and equity:
        cutoff = _t(equity[-1]["time"]) - timedelta(days=since_days)
        equity = [r for r in equity if _t(r["time"]) >= cutoff]
        trades = [r for r in trades if _t(r["time"]) >= cutoff]
        shadow_trades = [r for r in shadow_trades if _t(r["time"]) >= cutoff]
        leader_rows = [r for r in leader_rows if _t(r["time"]) >= cutoff]

    r = Review()
    if not equity:
        return r
    r.hours = (_t(equity[-1]["time"]) - _t(equity[0]["time"])).total_seconds() / 3600
    r.start_equity = float(equity[0]["equity"])
    r.end_equity = float(equity[-1]["equity"])
    r.total_return = r.end_equity / r.start_equity - 1 if r.start_equity else 0.0
    r.max_drawdown = max_drawdown([float(e["equity"]) for e in equity])
    btc = [float(e["btc"]) for e in equity if e.get("btc")]
    if len(btc) >= 2:
        r.btc_return = btc[-1] / btc[0] - 1
    r.halt_days = sorted({e["time"][:10] for e in equity if e.get("halt")})

    r.trades = len(trades)
    r.trades_per_day = r.trades / (r.hours / 24) if r.hours >= 1 else float(r.trades)
    r.fees = sum(float(t["fee"]) for t in trades)
    r.realized = sum(float(t["realized_pnl"]) for t in trades)
    gross = r.end_equity - r.start_equity + r.fees  # result before fees, open positions included
    r.fee_drag = r.fees / abs(gross) if abs(gross) > 1e-9 else (float("inf") if r.fees else 0.0)

    coin_pnl: dict[str, float] = defaultdict(float)
    last: dict[str, tuple[datetime, float]] = {}
    for t in trades:
        ts, size = _t(t["time"]), float(t["size"])
        coin_pnl[t["coin"]] += float(t["realized_pnl"]) - float(t["fee"])
        prev = last.get(t["coin"])
        if prev and (prev[1] > 0) != (size > 0) and ts - prev[0] <= timedelta(hours=1):
            r.churn_trades += 1
        last[t["coin"]] = (ts, size)
    r.coin_pnl = dict(sorted(coin_pnl.items(), key=lambda kv: kv[1]))

    by_leader: dict[str, list[dict]] = defaultdict(list)
    for row in leader_rows:
        by_leader[row["leader"]].append(row)
    s_trades: dict[str, list[dict]] = defaultdict(list)
    for t in shadow_trades:
        s_trades[t["leader"]].append(t)
    for addr, rows in by_leader.items():
        if active_leaders is not None and addr not in active_leaders:
            continue
        first, lastrow = rows[0], rows[-1]
        se0, se1 = float(first["shadow_equity"]), float(lastrow["shadow_equity"])
        le0, le1 = float(first["leader_equity"]), float(lastrow["leader_equity"])
        st = s_trades.get(addr, [])
        r.leaders.append(LeaderStats(
            address=addr,
            hours=(_t(lastrow["time"]) - _t(first["time"])).total_seconds() / 3600,
            shadow_return=se1 / se0 - 1 if se0 else 0.0,
            leader_return=le1 / le0 - 1 if le0 else 0.0,
            trades=len(st),
            closed_trades=sum(1 for t in st if float(t["realized_pnl"]) != 0),
            fees=sum(float(t["fee"]) for t in st),
        ))
    r.leaders.sort(key=lambda s: s.shadow_return)
    return r


def to_markdown(r: Review, title: str = "Review") -> str:
    if not r.hours and not r.trades:
        return f"## {title}\n\nNog geen data.\n"
    lines = [f"## {title}", ""]
    btc = f"{r.btc_return:+.2%}" if r.btc_return is not None else "n.v.t."
    lines += [
        "| Meting | Waarde |", "|---|---|",
        f"| Periode | {r.hours:.1f} uur |",
        f"| Vermogen | ${r.start_equity:,.2f} → ${r.end_equity:,.2f} ({r.total_return:+.2%}) |",
        f"| BTC vasthouden (benchmark) | {btc} |",
        f"| Grootste daling (drawdown) | {r.max_drawdown:.2%} |",
        f"| Trades | {r.trades} ({r.trades_per_day:.1f}/dag) |",
        f"| Gerealiseerd / kosten | ${r.realized:+,.2f} / ${r.fees:,.2f} |",
        f"| Heen-en-weer trades (churn) | {r.churn_trades} ({r.churn_ratio:.0%}) |",
        f"| Dagen met risicostop | {', '.join(r.halt_days) or 'geen'} |",
        "",
    ]
    if r.leaders:
        lines += ["**Per trader (schaduwboek: wat als je alleen deze trader kopieerde)**", "",
                  "| Trader | Uren | Kopie-rendement | Rendement trader zelf | Trades |", "|---|---|---|---|---|"]
        for s in r.leaders:
            lines.append(f"| `{s.address[:10]}…` | {s.hours:.0f} | {s.shadow_return:+.2%} | "
                         f"{s.leader_return:+.2%} | {s.trades} |")
        lines.append("")
    if r.coin_pnl:
        worst = list(r.coin_pnl.items())[:3]
        best = list(r.coin_pnl.items())[-3:][::-1]
        lines.append("**Slechtste munten:** " + ", ".join(f"{c} ${v:+.2f}" for c, v in worst))
        lines.append("**Beste munten:** " + ", ".join(f"{c} ${v:+.2f}" for c, v in best))
        lines.append("")
    return "\n".join(lines)
