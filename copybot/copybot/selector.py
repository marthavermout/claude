"""Pick traders worth copying from the public leaderboard.

Goal is to avoid the usual traps: lucky one-hit wonders, market makers/HFT bots
(impossible to copy with a delay), and gamblers on extreme leverage."""

from __future__ import annotations

from dataclasses import dataclass

from .hyperliquid import ApiError, HyperliquidClient


@dataclass
class Candidate:
    address: str
    account_value: float
    week_roi: float
    month_roi: float
    alltime_roi: float
    alltime_pnl: float
    turnover: float  # monthly volume / account value
    leverage: float | None = None
    score: float = 0.0


@dataclass
class SelectionRules:
    min_account_value: float = 100_000  # skin in the game
    min_alltime_pnl: float = 100_000  # long-term profitable, not just this month
    min_month_roi: float = 0.03
    max_month_roi: float = 1.0  # >100%/month is usually luck or extreme leverage
    min_week_roi: float = -0.05  # not currently blowing up
    max_turnover: float = 40.0  # higher = market maker / HFT, uncopyable
    max_leverage: float = 5.0


def _windows(row: dict) -> dict[str, dict[str, float]]:
    return {name: {k: float(v) for k, v in perf.items()} for name, perf in row["windowPerformances"]}


def screen(rows: list[dict], rules: SelectionRules) -> list[Candidate]:
    out = []
    for row in rows:
        acct = float(row["accountValue"])
        if acct < rules.min_account_value:
            continue
        w = _windows(row)
        week, month, alltime = w.get("week"), w.get("month"), w.get("allTime")
        if not (week and month and alltime):
            continue
        turnover = month["vlm"] / acct
        if (
            alltime["pnl"] < rules.min_alltime_pnl
            or alltime["roi"] <= 0
            or not rules.min_month_roi <= month["roi"] <= rules.max_month_roi
            or week["roi"] < rules.min_week_roi
            or turnover > rules.max_turnover
            or month["vlm"] <= 0
        ):
            continue
        c = Candidate(row["ethAddress"].lower(), acct, week["roi"], month["roi"], alltime["roi"], alltime["pnl"], turnover)
        # Reward consistency over a single spike; cap each term so one window can't dominate.
        c.score = min(c.month_roi, 0.5) + 0.5 * min(c.alltime_roi, 2.0) + 0.5 * min(max(c.week_roi, -0.1), 0.2)
        out.append(c)
    return sorted(out, key=lambda c: c.score, reverse=True)


def select(client: HyperliquidClient, rules: SelectionRules, top: int = 10) -> list[Candidate]:
    chosen = []
    mids = client.mids()
    for cand in screen(client.leaderboard(), rules):
        if len(chosen) >= top:
            break
        try:
            snap = client.snapshot(cand.address)
        except ApiError:
            continue
        if snap.equity <= 0:
            continue
        gross = sum(abs(s) * mids.get(c, 0.0) for c, s in snap.positions.items())
        cand.leverage = gross / snap.equity
        if cand.leverage <= rules.max_leverage:
            chosen.append(cand)
    return chosen
