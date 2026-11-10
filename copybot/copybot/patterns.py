"""Chart-pattern trading: find classic patterns on candles and trade the breakout.

Patterns (each with entry on the breakout, stop-loss (SL) and take-profit (TP)):
  reversal:     double bottom/top, triple bottom/top, (inverse) head & shoulders
  continuation: ascending/descending triangle, falling/rising wedge, bull/bear flag

Swing points ("pivots") are only confirmed `PIVOT_K` candles after they happen, so the
detector never looks into the future. That makes the backtest honest: at every candle it
only sees what a live bot would have seen.
"""

from __future__ import annotations

from dataclasses import dataclass, field

PIVOT_K = 3            # a pivot is the highest/lowest point of K candles on each side
TOL = 0.006            # two prices count as "the same level" within 0.6%
MIN_HEIGHT = 0.008     # a pattern must be at least 0.8% tall
MAX_BARS_OPEN = 96     # close a trade after 24h (15m candles) if neither SL nor TP was hit
SL_BUFFER = 0.002      # stop a little beyond the pattern's extreme


@dataclass
class Candle:
    t: int
    o: float
    h: float
    l: float
    c: float


@dataclass
class Signal:
    pattern: str
    side: int          # +1 long, -1 short
    entry: float
    sl: float
    tp: float


@dataclass
class Pivot:
    i: int
    price: float
    kind: str          # "H" or "L"


def parse_candles(raw: list[dict]) -> list[Candle]:
    return [Candle(int(x["t"]), float(x["o"]), float(x["h"]), float(x["l"]), float(x["c"])) for x in raw]


class PivotTracker:
    """Keeps the alternating list of swing highs/lows up to date, one candle at a time.
    A pivot at candle i only becomes known at candle i + k (no peeking ahead)."""

    def __init__(self, k: int = PIVOT_K):
        self.k = k
        self.list: list[Pivot] = []
        self.seen = -1

    def update(self, candles: list[Candle], upto: int) -> list[Pivot]:
        k = self.k
        for i in range(max(k, self.seen + 1), upto - k + 1):
            window = candles[i - k:i + k + 1]
            hi, lo = candles[i].h, candles[i].l
            for kind, price, is_pivot in (("H", hi, hi == max(c.h for c in window)), ("L", lo, lo == min(c.l for c in window))):
                if not is_pivot:
                    continue
                out = self.list
                if out and out[-1].kind == kind:  # keep only the more extreme of two in a row
                    if (kind == "H" and price > out[-1].price) or (kind == "L" and price < out[-1].price):
                        out[-1] = Pivot(i, price, kind)
                    continue
                out.append(Pivot(i, price, kind))
            self.seen = i
        return self.list


def pivots(candles: list[Candle], upto: int, k: int = PIVOT_K) -> list[Pivot]:
    """Alternating swing highs/lows confirmed by candle `upto` (inclusive)."""
    return list(PivotTracker(k).update(candles, upto))


def _same(a: float, b: float, tol: float = TOL) -> bool:
    return abs(a - b) / ((a + b) / 2) <= tol


def _broke_up(candles: list[Candle], j: int, level: float) -> bool:
    return candles[j].c > level >= candles[j - 1].c


def _broke_down(candles: list[Candle], j: int, level: float) -> bool:
    return candles[j].c < level <= candles[j - 1].c


def _line(p1: Pivot, p2: Pivot, at: int) -> float:
    return p1.price + (p2.price - p1.price) * (at - p1.i) / (p2.i - p1.i)


def detect(candles: list[Candle], j: int, pv: list[Pivot] | None = None) -> Signal | None:
    """Return a breakout signal on closed candle j, or None. Pass `pv` (pivots up to j) to
    avoid recomputing them."""
    if j < 30:
        return None
    pv = pivots(candles, j) if pv is None else pv
    if len(pv) < 3:
        return None
    c = candles[j].c
    kinds = "".join(p.kind for p in pv[-5:])
    last = pv[-5:]

    def long(name, sl, target_height, level):
        return Signal(name, 1, c, sl * (1 - SL_BUFFER), level + target_height)

    def short(name, sl, target_height, level):
        return Signal(name, -1, c, sl * (1 + SL_BUFFER), level - target_height)

    # --- reversal: head & shoulders (5 pivots) ---
    if len(last) == 5:
        a, b, h, d, e = last
        if kinds == "LHLHL" and h.price < min(a.price, e.price) and _same(a.price, e.price, 2 * TOL):
            neck = max(b.price, d.price)
            if (neck - h.price) / neck >= MIN_HEIGHT and _broke_up(candles, j, neck):
                return long("inverted head & shoulders", min(a.price, e.price), neck - h.price, neck)
        if kinds == "HLHLH" and h.price > max(a.price, e.price) and _same(a.price, e.price, 2 * TOL):
            neck = min(b.price, d.price)
            if (h.price - neck) / neck >= MIN_HEIGHT and _broke_down(candles, j, neck):
                return short("head & shoulders", max(a.price, e.price), h.price - neck, neck)
        # triple bottom / top
        if kinds == "LHLHL" and _same(a.price, h.price) and _same(h.price, e.price) and _same(a.price, e.price):
            neck = max(b.price, d.price)
            low = min(a.price, h.price, e.price)
            if (neck - low) / neck >= MIN_HEIGHT and _broke_up(candles, j, neck):
                return long("triple bottom", low, neck - low, neck)
        if kinds == "HLHLH" and _same(a.price, h.price) and _same(h.price, e.price) and _same(a.price, e.price):
            neck = min(b.price, d.price)
            high = max(a.price, h.price, e.price)
            if (high - neck) / neck >= MIN_HEIGHT and _broke_down(candles, j, neck):
                return short("triple top", high, high - neck, neck)

    # --- reversal: double bottom / top (3 pivots) ---
    x, y, z = pv[-3:]
    if x.kind + y.kind + z.kind == "LHL" and _same(x.price, z.price):
        low = min(x.price, z.price)
        if (y.price - low) / y.price >= MIN_HEIGHT and _broke_up(candles, j, y.price):
            return long("double bottom", low, y.price - low, y.price)
    if x.kind + y.kind + z.kind == "HLH" and _same(x.price, z.price):
        high = max(x.price, z.price)
        if (high - y.price) / y.price >= MIN_HEIGHT and _broke_down(candles, j, y.price):
            return short("double top", high, high - y.price, y.price)

    # --- continuation: triangles and wedges (last two highs and two lows) ---
    highs = [p for p in pv[-4:] if p.kind == "H"]
    lows = [p for p in pv[-4:] if p.kind == "L"]
    if len(highs) == 2 and len(lows) == 2:
        h1, h2 = highs
        l1, l2 = lows
        height = max(h1.price, h2.price) - min(l1.price, l2.price)
        if height / c >= MIN_HEIGHT:
            # ascending triangle: flat top, rising lows -> break up
            if _same(h1.price, h2.price) and l2.price > l1.price * (1 + TOL):
                top = max(h1.price, h2.price)
                if _broke_up(candles, j, top):
                    return long("ascending triangle", l2.price, height, top)
            # descending triangle: flat bottom, falling highs -> break down
            if _same(l1.price, l2.price) and h2.price < h1.price * (1 - TOL):
                bottom = min(l1.price, l2.price)
                if _broke_down(candles, j, bottom):
                    return short("descending triangle", h2.price, height, bottom)
            # falling wedge (bullish): both falling, highs falling faster -> break above upper line
            hs, ls = (h1.price - h2.price) / h1.price, (l1.price - l2.price) / l1.price
            if hs > TOL and ls > 0 and hs > ls:
                upper = _line(h1, h2, j)
                if candles[j].c > upper and candles[j - 1].c <= _line(h1, h2, j - 1):
                    return long("bullish wedge", l2.price, height, upper)
            # rising wedge (bearish): both rising, lows rising faster -> break below lower line
            hr, lr = (h2.price - h1.price) / h1.price, (l2.price - l1.price) / l1.price
            if lr > TOL and hr > 0 and lr > hr:
                lower = _line(l1, l2, j)
                if candles[j].c < lower and candles[j - 1].c >= _line(l1, l2, j - 1):
                    return short("bearish wedge", h2.price, height, lower)

    # --- continuation: flags (strong pole, small counter-move, break in pole direction) ---
    if len(pv) >= 3:
        start, top, low = pv[-3:]
        if start.kind == "L" and top.kind == "H" and low.kind == "L":
            pole = top.price - start.price
            if pole / start.price >= 3 * MIN_HEIGHT and top.i - start.i <= 16 and (top.price - low.price) < 0.5 * pole \
                    and j - top.i <= 20 and _broke_up(candles, j, top.price):
                return long("bullish flag", low.price, pole, top.price)
        if start.kind == "H" and top.kind == "L" and low.kind == "H":
            pole = start.price - top.price
            if pole / start.price >= 3 * MIN_HEIGHT and top.i - start.i <= 16 and (low.price - top.price) < 0.5 * pole \
                    and j - top.i <= 20 and _broke_down(candles, j, top.price):
                return short("bearish flag", low.price, pole, top.price)
    return None


@dataclass
class PatternTrade:
    coin: str
    pattern: str
    side: int
    entry: float
    sl: float
    tp: float
    opened_bar_t: int
    size: float = 0.0  # signed coin amount (live bot)


def exit_reason(trade: PatternTrade, high: float, low: float, bars_open: int) -> tuple[str, float] | None:
    """Did this candle (or price range) hit SL or TP? SL is checked first (conservative)."""
    if trade.side > 0:
        if low <= trade.sl:
            return "stop-loss", trade.sl
        if high >= trade.tp:
            return "take-profit", trade.tp
    else:
        if high >= trade.sl:
            return "stop-loss", trade.sl
        if low <= trade.tp:
            return "take-profit", trade.tp
    if bars_open >= MAX_BARS_OPEN:
        return "time", None  # caller uses the close
    return None


def risk_notional(equity: float, entry: float, sl: float, risk_per_trade: float, cap: float) -> float:
    """Size so that hitting the stop-loss costs `risk_per_trade` of equity, capped at `cap` dollars."""
    dist = abs(entry - sl) / entry
    if dist <= 0:
        return 0.0
    return min(cap, equity * risk_per_trade / dist)


@dataclass
class BacktestResult:
    trades: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        by: dict[str, list[dict]] = {}
        for t in self.trades:
            by.setdefault(t["pattern"], []).append(t)
        rows = {}
        for name, ts in sorted(by.items()):
            rows[name] = {
                "trades": len(ts),
                "win_rate": round(sum(1 for t in ts if t["pnl"] > 0) / len(ts), 3),
                "pnl_usd": round(sum(t["pnl"] for t in ts), 2),
            }
        total = sum(t["pnl"] for t in self.trades)
        return {"trades": len(self.trades), "pnl_usd": round(total, 2),
                "win_rate": round(sum(1 for t in self.trades if t["pnl"] > 0) / len(self.trades), 3) if self.trades else None,
                "by_pattern": rows}


def backtest(candles_by_coin: dict[str, list[Candle]], equity: float = 1000.0, risk_per_trade: float = 0.01,
             max_position_pct: float = 0.25, fee_bps: float = 4.5, slippage_bps: float = 2.0,
             trend_filter: bool = True) -> BacktestResult:
    """Walk forward candle by candle per coin; at most one open trade per coin.
    Each trade risks `risk_per_trade` of the starting equity (no compounding: keeps coins comparable)."""
    cost = (fee_bps + slippage_bps) / 10_000
    res = BacktestResult()
    for coin, cs in candles_by_coin.items():
        open_t: PatternTrade | None = None
        notional = 0.0
        open_i = 0
        tracker = PivotTracker()
        trend = ema([c.c for c in cs], TREND_EMA) if cs else []
        for j in range(1, len(cs)):
            pv = tracker.update(cs, j)
            if open_t:
                hit = exit_reason(open_t, cs[j].h, cs[j].l, j - open_i)
                if hit:
                    reason, px = hit
                    px = cs[j].c if px is None else px
                    ret = open_t.side * (px - open_t.entry) / open_t.entry - 2 * cost
                    res.trades.append({"coin": coin, "pattern": open_t.pattern, "side": open_t.side, "reason": reason,
                                       "entry_t": open_t.opened_bar_t, "exit_t": cs[j].t, "ret": ret, "pnl": notional * ret})
                    open_t = None
                continue
            sig = detect(cs, j, pv)
            if trend_filter:
                sig = with_trend(sig, cs[j].c, trend[j])
            if sig:
                notional = risk_notional(equity, sig.entry, sig.sl, risk_per_trade, equity * max_position_pct)
                if notional > 0:
                    open_t = PatternTrade(coin, sig.pattern, sig.side, sig.entry, sig.sl, sig.tp, cs[j].t)
                    open_i = j
    return res


TREND_EMA = 200  # ~2 days of 15m candles: only trade patterns in the direction of this trend


def ema(values: list[float], n: int) -> list[float]:
    k, out, e = 2 / (n + 1), [], values[0]
    for v in values:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def with_trend(sig: Signal | None, close: float, trend: float) -> Signal | None:
    """Drop longs below the trend line and shorts above it. Tested on 30 days of 10 coins:
    it cut losses on both the first 20 days and the 10 days it was not designed on."""
    if sig is None:
        return None
    if (sig.side > 0 and close < trend) or (sig.side < 0 and close > trend):
        return None
    return sig


def scan(candles: list[Candle]) -> Signal | None:
    """Signal on the last *closed* candle (pass closed candles only)."""
    if len(candles) < 40:
        return None
    j = len(candles) - 1
    trend = ema([c.c for c in candles], TREND_EMA)[j]
    return with_trend(detect(candles, j), candles[j].c, trend)
