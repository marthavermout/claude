"""Two rule-based strategies that read the market themselves (no traders copied).

Trend follower ("time-series momentum", Moskowitz, Ooi & Pedersen 2012):
  long a coin whose fast average (24h) is above its slow average (7 days), short when below.
  Position size is scaled to each coin's volatility so a calm and a wild coin carry similar risk.

Funding earner:
  On perpetual futures, longs and shorts pay each other every hour ("funding"). When funding
  is strongly positive, the crowd is long and longs pay shorts, so the bot goes short to collect
  it (and the other way around). It is NOT hedged: the price can move against it, so every
  position has a stop-loss and is closed once funding is back to normal.

All settings below were fixed before any backtest was run; they are not fitted to the data.
"""

from __future__ import annotations

import math

# --- trend follower ---
FAST_HOURS = 24
SLOW_HOURS = 168
VOL_HOURS = 168
TARGET_DAILY_VOL = 0.03      # each coin sized as if it moved 3% a day...
BASE_WEIGHT = 0.05           # ...at 5% of equity (halved after the first backtest showed a 32% drawdown, above the 20% kill switch); wilder coins get less
REBALANCE_BAND = 0.25        # only resize when the target differs 25%+ (saves fees)

# --- funding earner ---
HIGH_FUNDING = 0.0000375     # per hour: 3x Hyperliquid's base rate (~33%/year)
NORMAL_FUNDING = 0.0000125   # per hour: base rate (~11%/year); exit when back to this
MAX_FUNDING_POSITIONS = 5
FUNDING_WEIGHT = 0.20        # 20% of equity per position (max 5 -> 1x total)
FUNDING_STOP = 0.04          # close if price moves 4% against the position


def ema_last(values: list[float], n: int) -> float:
    k, e = 2 / (n + 1), values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def trend_target(closes: list[float], equity: float, max_position_pct: float) -> float:
    """Signed target notional for one coin from its hourly closes (oldest first)."""
    if len(closes) < SLOW_HOURS + 2:
        return 0.0
    fast, slow = ema_last(closes[-SLOW_HOURS * 3:], FAST_HOURS), ema_last(closes[-SLOW_HOURS * 3:], SLOW_HOURS)
    rets = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - VOL_HOURS, len(closes))]
    mean = sum(rets) / len(rets)
    hourly = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
    daily = hourly * math.sqrt(24)
    if daily <= 0:
        return 0.0
    size = min(equity * BASE_WEIGHT * TARGET_DAILY_VOL / daily, equity * max_position_pct)
    return size if fast > slow else -size


def keep_or_resize(current: float, target: float) -> float:
    """Avoid churn: keep the current position unless the side flips or the size is off by 25%+."""
    if target == 0 or current == 0 or (current > 0) != (target > 0):
        return target
    if abs(target - current) / abs(target) < REBALANCE_BAND:
        return current
    return target


def funding_targets(funding: dict[str, float], prices: dict[str, float], open_pos: dict[str, dict],
                    equity: float) -> tuple[dict[str, float], dict[str, dict], list[tuple[str, str]]]:
    """Decide funding positions for this hour.

    funding: coin -> current hourly rate (positive = longs pay shorts)
    open_pos: coin -> {"side": +1/-1, "entry": price, "notional": $} (kept between calls)
    Returns (targets coin->signed notional, new open_pos, events [(coin, reason)])."""
    events = []
    pos = dict(open_pos)
    for coin, p in list(pos.items()):
        rate, px = funding.get(coin), prices.get(coin)
        if rate is None or px is None:
            continue
        adverse = (px / p["entry"] - 1) * p["side"]
        if adverse <= -FUNDING_STOP:
            events.append((coin, "stop-loss"))
            del pos[coin]
        elif abs(rate) <= NORMAL_FUNDING or (rate > 0) == (p["side"] > 0):
            events.append((coin, "funding normaal"))
            del pos[coin]
    candidates = sorted((c for c, r in funding.items() if abs(r) >= HIGH_FUNDING and c not in pos and c in prices),
                        key=lambda c: -abs(funding[c]))
    for coin in candidates[:max(0, MAX_FUNDING_POSITIONS - len(pos))]:
        side = -1 if funding[coin] > 0 else 1
        pos[coin] = {"side": side, "entry": prices[coin], "notional": equity * FUNDING_WEIGHT}
        events.append((coin, "open"))
    return {c: p["side"] * p["notional"] for c, p in pos.items()}, pos, events


def simulate(candles: dict[str, list[list]], funding: dict[str, list[list]], strategy: str, start: int, end: int,
             equity: float = 1000.0, cost_bps: float = 6.5, max_position_pct: float = 0.25, max_leverage: float = 2.0) -> dict:
    """Hour-by-hour portfolio backtest between hour indexes [start, end) on aligned hourly data.
    Price PnL, funding payments and trading costs (fee + slippage) are all included."""
    coins = [c for c in candles if len(candles[c]) >= end]
    fund_at = {c: {t: r for t, r in funding.get(c, [])} for c in coins}
    hour = lambda t: t - t % 3_600_000
    fund_by_hour = {c: {hour(t): r for t, r in fund_at[c].items()} for c in coins}
    cost = cost_bps / 10_000
    pos: dict[str, float] = {}
    fstate: dict[str, dict] = {}
    eq, peak, max_dd, costs, fund_pnl, n_trades = equity, equity, 0.0, 0.0, 0.0, 0
    daily = []
    day_start = equity
    for i in range(start, end - 1):
        prices = {c: candles[c][i][4] for c in coins}
        rates = {c: fund_by_hour[c].get(hour(candles[c][i][0])) for c in coins}
        rates = {c: r for c, r in rates.items() if r is not None}
        if strategy == "trend":
            targets = {c: keep_or_resize(pos.get(c, 0.0), trend_target([x[4] for x in candles[c][max(0, i - 600):i + 1]], eq, max_position_pct))
                       for c in coins}
        elif strategy == "funding":
            targets, fstate, _ = funding_targets(rates, prices, fstate, eq)
        elif strategy == "hold_btc":
            targets = {"BTC": equity} if i == start else dict(pos)
        else:
            raise ValueError(strategy)
        gross = sum(abs(v) for v in targets.values())
        if gross > eq * max_leverage and strategy != "hold_btc":
            targets = {c: v * eq * max_leverage / gross for c, v in targets.items()}
        for c in set(targets) | set(pos):
            delta = abs(targets.get(c, 0.0) - pos.get(c, 0.0))
            if delta > 1e-9:
                costs += delta * cost
                eq -= delta * cost
                n_trades += 1
        pos = {c: v for c, v in targets.items() if v}
        for c, v in pos.items():
            nxt = candles[c][i + 1][4] / candles[c][i][4] - 1
            eq += v * nxt
            r = rates.get(c, 0.0)
            eq -= v * r          # positive rate: longs pay, shorts receive
            fund_pnl -= v * r
            pos[c] = v * (1 + nxt)
        peak = max(peak, eq)
        max_dd = max(max_dd, 1 - eq / peak)
        if (i - start) % 24 == 23:
            daily.append(eq / day_start - 1)
            day_start = eq
    days = len(daily)
    mean = sum(daily) / days if days else 0.0
    sd = math.sqrt(sum((d - mean) ** 2 for d in daily) / (days - 1)) if days > 1 else 0.0
    return {"return": round(eq / equity - 1, 4), "max_drawdown": round(max_dd, 4), "trades": n_trades,
            "costs": round(costs, 2), "funding_pnl": round(fund_pnl, 2), "days": days,
            "sharpe_daily_t": round(mean / (sd / math.sqrt(days)), 2) if sd else None}
