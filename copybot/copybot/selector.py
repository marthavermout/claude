"""Pick traders worth copying from the public leaderboard.

Goal is to avoid the usual traps: lucky one-hit wonders, market makers/HFT bots
(impossible to copy with a delay), and gamblers on extreme leverage."""

from __future__ import annotations

import random
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
    fills_24h: int | None = None
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
    min_fills_24h: int = 3  # must actually be trading right now (main perps only)


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


def screen_random(rows: list[dict], rules: SelectionRules, rng: random.Random | None = None) -> list[Candidate]:
    """Control group: same basic filters (real money, not a market maker) but NO filter or
    ranking on past results, in random order. If the real selection can't beat this over
    weeks, picking 'good' traders adds nothing and any profit was luck."""
    rng = rng or random.Random()
    out = []
    for row in rows:
        acct = float(row["accountValue"])
        if acct < rules.min_account_value:
            continue
        w = _windows(row)
        month = w.get("month")
        if not month or month["vlm"] <= 0 or month["vlm"] / acct > rules.max_turnover:
            continue
        out.append(Candidate(row["ethAddress"].lower(), acct, w.get("week", {}).get("roi", 0.0), month["roi"],
                             w.get("allTime", {}).get("roi", 0.0), w.get("allTime", {}).get("pnl", 0.0), month["vlm"] / acct))
    rng.shuffle(out)
    return out


def select(client: HyperliquidClient, rules: SelectionRules, top: int = 10, randomize: bool = False) -> list[Candidate]:
    chosen = []
    mids = client.mids()
    rows = client.leaderboard()
    for cand in (screen_random(rows, rules) if randomize else screen(rows, rules)):
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
        if cand.leverage > rules.max_leverage:
            continue
        if rules.min_fills_24h:
            try:
                fills = client.recent_fills(cand.address, 24)
            except ApiError:
                continue
            # coins like "xyz:NVDA" live on a separate exchange section the bot can't copy
            cand.fills_24h = sum(1 for f in fills if ":" not in f.get("coin", ""))
            if cand.fills_24h < rules.min_fills_24h:
                continue
        chosen.append(cand)
    return chosen
