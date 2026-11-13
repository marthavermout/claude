"""The main loop: read leaders -> compute targets -> apply risk -> trade on paper.

Besides the real (combined) paper book, every leader gets its own *shadow* book that
copies only that leader. The review step uses those to see which leader makes or
loses money, so bad picks can be replaced."""

from __future__ import annotations

import csv
import gzip
import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .config import BotConfig
from .hyperliquid import ApiError, HyperliquidClient
from .paper import Fill, PaperBroker
from .patterns import PatternTrade, exit_reason, parse_candles, risk_notional, scan
from . import quant
from .risk import RiskManager, RiskState
from .strategy import filter_baseline, target_notionals

log = logging.getLogger("copybot")

TRADE_HEADER = ["time", "coin", "size", "price", "fee", "realized_pnl"]


class Engine:
    def __init__(self, cfg: BotConfig, client: HyperliquidClient | None = None):
        self.cfg = cfg
        self.client = client or HyperliquidClient()
        self.dir = Path(cfg.data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.baselines: dict[str, dict[str, int]] = {}
        self.shadows: dict[str, PaperBroker] = {}
        self.shadow_started: dict[str, str] = {}
        self.meta: dict = {}
        self.pattern_trades: dict[str, dict] = {}  # coin -> PatternTrade fields + opened (ISO)
        self.pattern_scanned: dict[str, int] = {}  # coin -> start time of the last candle scanned
        self.quant_sizes: dict[str, float] = {}     # trend/funding: coin -> signed target size
        self.funding_pos: dict[str, dict] = {}      # funding bot: coin -> {side, entry, notional, size}
        self.quant_hour: int = 0                    # last hour the trend/funding bot decided
        self.broker = PaperBroker(cfg.starting_balance, cfg.fee_bps, cfg.slippage_bps)
        self.risk = RiskManager(cfg.risk)
        self._decimals: dict[str, int] | None = None
        self.record_replay = True
        # With fast polling, write history rows (equity, leaders, replay) at most this often.
        # Trades are always written. 0 = every round (single-bot runs and tests).
        self.log_every_s = 0.0
        self._last_log: datetime | None = None
        # leaders.csv only needs the first and last row per trader (for the review), so it is
        # written far less often; it was the main reason the data branch grew ~2 MB/bot/day.
        self.leaders_log_every_s = 0.0
        self._last_leaders_log: datetime | None = None
        self._replay: list[dict] = []
        self._load()

    # ---------- persistence ----------
    def _load(self) -> None:
        if not self.state_path.exists():
            return
        data = json.loads(self.state_path.read_text())
        fee, slip = self.cfg.fee_bps, self.cfg.slippage_bps
        self.broker = PaperBroker.from_dict(data["broker"], fee, slip)
        self.risk.state = RiskState(**data["risk"])
        self.baselines = data.get("baselines", {})
        self.shadows = {a: PaperBroker.from_dict(b, fee, slip) for a, b in data.get("shadows", {}).items()}
        self.shadow_started = data.get("shadow_started", {})
        self.meta = data.get("meta", {})
        self.pattern_trades = data.get("pattern_trades", {})
        self.pattern_scanned = data.get("pattern_scanned", {})
        self.quant_sizes = data.get("quant_sizes", {})
        self.funding_pos = data.get("funding_pos", {})
        self.quant_hour = data.get("quant_hour", 0)

    def save(self) -> None:
        data = {
            "broker": self.broker.to_dict(),
            "risk": asdict(self.risk.state),
            "baselines": self.baselines,
            "shadows": {a: b.to_dict() for a, b in self.shadows.items()},
            "shadow_started": self.shadow_started,
            "meta": self.meta,
            "pattern_trades": self.pattern_trades,
            "pattern_scanned": self.pattern_scanned,
            "quant_sizes": self.quant_sizes,
            "funding_pos": self.funding_pos,
            "quant_hour": self.quant_hour,
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.state_path)

    def _append_csv(self, name: str, header: list[str], row: list) -> None:
        path = self.dir / name
        new = not path.exists()
        with path.open("a", newline="") as fh:
            writer = csv.writer(fh)
            if new:
                writer.writerow(header)
            writer.writerow(row)

    # ---------- trading ----------
    def _decimals_for(self, coin: str) -> int:
        if self._decimals is None:
            self._decimals = self.client.size_decimals()
        return self._decimals.get(coin, 4)

    def _round(self, coin: str, size: float) -> float:
        return round(size, self._decimals_for(coin))

    def flush_replay(self) -> Path | None:
        """Write recorded rounds to replay/<first-tick>.jsonl.gz (immutable files keep git small).
        `python -m copybot backtest` replays them to test code changes on real history."""
        if not self._replay:
            return None
        folder = self.dir / "replay"
        folder.mkdir(exist_ok=True)
        name = self._replay[0]["t"][:19].replace(":", "").replace("-", "") + ".jsonl.gz"
        path = folder / name
        with gzip.open(path, "at") as fh:
            for rec in self._replay:
                fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        self._replay = []
        return path

    def _rebalance(self, broker: PaperBroker, targets: dict[str, float], mids: dict[str, float]) -> list[Fill]:
        fills = []
        for coin in sorted(set(targets) | set(broker.positions)):
            mid = mids.get(coin)
            if not mid:
                continue
            desired = self._round(coin, targets.get(coin, 0.0) / mid)
            current = broker.size(coin)
            delta = desired - current
            closing = desired == 0 and current != 0
            if delta == 0 or (not closing and abs(delta) * mid < self.cfg.risk.min_trade_usd):
                continue
            fills.append(broker.execute(coin, delta, mid))
        return fills

    def tick(self, now: datetime | None = None) -> list[Fill]:
        now = now or datetime.now(timezone.utc)
        ts = now.isoformat()
        patterns = self.cfg.strategy != "copy"
        try:
            mids = self.client.mids()
            raw_snaps = [] if patterns else [self.client.snapshot(a) for a in self.cfg.leaders]
        except ApiError as exc:
            log.warning("data unavailable, skipping this round (no trades): %s", exc)
            return []

        snaps = []
        for snap in raw_snaps:
            if not self.cfg.copy_existing_positions:
                if snap.address not in self.baselines:
                    self.baselines[snap.address] = {c: (1 if s > 0 else -1) for c, s in snap.positions.items()}
                    if snap.positions:
                        log.info("%s: ignoring %d existing positions until they close: %s",
                                 snap.address[:10], len(snap.positions), ", ".join(snap.positions))
                before = set(self.baselines[snap.address])
                snap = filter_baseline(snap, self.baselines[snap.address], mids, self.cfg.join_within_pct)
                joined = before - set(self.baselines[snap.address])
                for coin in sorted(c for c in joined if c in snap.positions):
                    log.info("%s: joining existing %s position (price within %.1f%% of their entry)",
                             snap.address[:10], coin, self.cfg.join_within_pct * 100)
            snaps.append(snap)

        log_now = self._last_log is None or (now - self._last_log).total_seconds() >= self.log_every_s
        if self.record_replay and log_now:
            coins = {"BTC"} | set(self.broker.positions)
            for snap in raw_snaps:
                coins |= set(snap.positions)
            self._replay.append({
                "t": ts,
                "mids": {c: mids[c] for c in sorted(coins) if c in mids},
                "dec": {c: self._decimals_for(c) for c in sorted(coins)},
                "leaders": {s.address: {"equity": s.equity, "positions": s.positions, "entries": s.entries}
                            for s in raw_snaps},
            })

        # shadow books: one per leader, no halts, same position limits.
        # A mini mirrors its big bot's traders and never judges them, so it skips this work.
        log_leaders = self._last_leaders_log is None or (now - self._last_leaders_log).total_seconds() >= self.leaders_log_every_s
        if log_leaders and raw_snaps and not self.cfg.follow_bot:
            self._last_leaders_log = now
        for raw, snap in ([] if self.cfg.follow_bot else zip(raw_snaps, snaps)):
            shadow = self.shadows.get(snap.address)
            if shadow is None:
                shadow = self.shadows[snap.address] = PaperBroker(
                    self.cfg.starting_balance, self.cfg.fee_bps, self.cfg.slippage_bps)
                self.shadow_started[snap.address] = ts
            s_eq = shadow.equity(mids)
            s_targets = self.risk.apply_limits(target_notionals([snap], mids, s_eq, self.cfg.copy_ratio), s_eq)
            for f in self._rebalance(shadow, s_targets, mids):
                self._append_csv("shadow_trades.csv", ["time", "leader"] + TRADE_HEADER[1:],
                                 [ts, snap.address, f.coin, f.size, f.price, f.fee, f.realized])
            if log_leaders:
                self._append_csv("leaders.csv", ["time", "leader", "leader_equity", "shadow_equity"],
                                 [ts, snap.address, round(raw.equity, 2), round(shadow.equity(mids), 4)])

        # funding data is only needed once per hour (settlement, and the funding bot's decision)
        hour = int(now.timestamp() // 3600)
        need_ctxs = hour != self.meta.get("funding_hour") or (self.cfg.strategy == "funding" and hour != self.quant_hour)
        ctxs = self._asset_ctxs() if need_ctxs else {}
        self._accrue_funding(ctxs, now)

        # the real paper book
        equity = self.broker.equity(mids)
        halt = self.risk.halt_reason(equity, now.strftime("%Y-%m-%d"))
        if halt:
            if self.broker.positions:
                log.warning("RISK STOP: %s -> closing all positions", halt)
            for coin in list(self.pattern_trades):
                self._close_pattern(coin, "risicostop", mids.get(coin), ts)
            targets = {}
        elif self.cfg.strategy == "patterns":
            targets = self.risk.apply_limits(self._pattern_targets(mids, now, equity), equity)
        elif self.cfg.strategy == "trend":
            targets = self.risk.apply_limits(self._trend_targets(mids, now, equity), equity)
        elif self.cfg.strategy == "funding":
            targets = self.risk.apply_limits(self._funding_targets(ctxs, mids, now, equity), equity)
        else:
            targets = self.risk.apply_limits(target_notionals(snaps, mids, equity, self.cfg.copy_ratio), equity)

        fills = self._rebalance(self.broker, targets, mids)
        for f in fills:
            log.info("%s %s %g @ %.4f  fee %.4f  realized %+.2f",
                     "BUY " if f.size > 0 else "SELL", f.coin, abs(f.size), f.price, f.fee, f.realized)
            self._append_csv("trades.csv", TRADE_HEADER, [ts, f.coin, f.size, f.price, f.fee, f.realized])

        equity = self.broker.equity(mids)
        if log_now or fills:
            self._append_csv("equity.csv", ["time", "equity", "positions", "btc", "halt"],
                             [ts, round(equity, 4), len(self.broker.positions), mids.get("BTC", ""), halt or ""])
            self._last_log = now
        if log_now or fills or halt:
            self.save()
        return fills

    # ---------- funding payments (all bots) ----------
    def _asset_ctxs(self) -> dict[str, dict]:
        getter = getattr(self.client, "asset_ctxs", None)
        if getter is None:
            return {}
        try:
            return getter()
        except (ApiError, KeyError, ValueError, TypeError) as exc:
            log.warning("funding data unavailable: %s", exc)
            return {}

    def _accrue_funding(self, ctxs: dict[str, dict], now: datetime) -> None:
        """Hyperliquid settles funding every hour. Charge (or pay) it once per hour boundary,
        to the real book and every shadow book, so results match what a real account would get."""
        if not ctxs:
            return
        hour = int(now.timestamp() // 3600)
        last = self.meta.get("funding_hour")
        self.meta["funding_hour"] = hour
        if last is None or hour <= last:
            return
        rates = {c: v["funding"] for c, v in ctxs.items()}
        marks = {c: v["mark"] for c, v in ctxs.items()}
        for _ in range(min(hour - last, 3)):  # a long gap means we weren't running: charge at most a few hours
            paid = self.broker.apply_funding(rates, marks)
            for shadow in self.shadows.values():
                shadow.apply_funding(rates, marks)
            if paid:
                log.info("funding %s $%.4f", "paid" if paid > 0 else "received", abs(paid))

    # ---------- trend follower and funding earner ----------
    def _log_event(self, ts: str, coin: str, event: str, detail: str = "") -> None:
        self._append_csv("strategy_events.csv", ["time", "coin", "event", "detail"], [ts, coin, event, detail])

    def _trend_targets(self, mids: dict[str, float], now: datetime, equity: float) -> dict[str, float]:
        hour = int(now.timestamp() // 3600)
        if hour != self.quant_hour or not self.quant_sizes:
            hour_start_ms = hour * 3_600_000
            for coin in self.cfg.pattern_coins:
                mid = mids.get(coin)
                if not mid:
                    continue
                try:
                    candles = [c for c in parse_candles(self.client.candles(coin, "1h", 600)) if c.t < hour_start_ms]
                except (ApiError, KeyError, ValueError) as exc:
                    log.warning("candles for %s unavailable: %s", coin, exc)
                    continue
                current = self.quant_sizes.get(coin, 0.0) * mid
                target = quant.keep_or_resize(current, quant.trend_target([c.c for c in candles], equity, self.cfg.risk.max_position_pct))
                if (target > 0) != (current > 0) or (target == 0) != (current == 0):
                    self._log_event(now.isoformat(), coin, "trend " + ("omhoog: long" if target > 0 else "omlaag: short" if target < 0 else "geen"))
                self.quant_sizes[coin] = target / mid
            self.quant_hour = hour
        return {c: s * mids[c] for c, s in self.quant_sizes.items() if c in mids and s}

    def _funding_targets(self, ctxs: dict[str, dict], mids: dict[str, float], now: datetime, equity: float) -> dict[str, float]:
        ts = now.isoformat()
        # stop-loss every tick
        for coin, p in list(self.funding_pos.items()):
            mid = mids.get(coin)
            if mid and (mid / p["entry"] - 1) * p["side"] <= -quant.FUNDING_STOP:
                del self.funding_pos[coin]
                self._log_event(ts, coin, "stop-loss")
        hour = int(now.timestamp() // 3600)
        if hour != self.quant_hour and ctxs:
            rates = {c: ctxs[c]["funding"] for c in self.cfg.pattern_coins if c in ctxs}
            prices = {c: mids[c] for c in rates if c in mids}
            # only collect funding where the price trend agrees (tested: loss -9.8%/-13.3% -> -1.3%/-0.5%)
            trend: dict[str, int] = {}
            for coin, rate in rates.items():
                if abs(rate) < quant.HIGH_FUNDING or coin in self.funding_pos or coin not in prices:
                    continue
                try:
                    closes = [c.c for c in parse_candles(self.client.candles(coin, "1h", 600)) if c.t < hour * 3_600_000]
                    trend[coin] = quant.trend_side(closes)
                except (ApiError, KeyError, ValueError) as exc:
                    log.warning("candles for %s unavailable: %s", coin, exc)
            _, pos, events = quant.funding_targets(rates, prices, self.funding_pos, equity, trend)
            for coin, p in pos.items():
                p.setdefault("size", p["side"] * p["notional"] / p["entry"])
            for coin, reason in events:
                self._log_event(ts, coin, reason, f"funding {rates.get(coin, 0) * 100:.4f}%/uur")
            self.funding_pos = pos
            self.quant_hour = hour
        return {c: p["size"] * mids[c] for c, p in self.funding_pos.items() if c in mids}

    # ---------- chart patterns ----------
    def _close_pattern(self, coin: str, reason: str, price: float | None, ts: str) -> None:
        tr = self.pattern_trades.pop(coin, None)
        if tr is None:
            return
        log.info("pattern %s %s closed: %s", coin, tr["pattern"], reason)
        self._append_csv("pattern_trades.csv", ["time", "coin", "event", "pattern", "side", "entry", "sl", "tp", "price"],
                         [ts, coin, reason, tr["pattern"], tr["side"], tr["entry"], tr["sl"], tr["tp"], price or ""])

    def _pattern_targets(self, mids: dict[str, float], now: datetime, equity: float) -> dict[str, float]:
        ts = now.isoformat()
        # 1) exits: stop-loss, take-profit or too long open
        for coin, tr in list(self.pattern_trades.items()):
            mid = mids.get(coin)
            if mid is None:
                continue
            bars = int((now - datetime.fromisoformat(tr["opened"])).total_seconds() // 900)
            t = PatternTrade(coin, tr["pattern"], tr["side"], tr["entry"], tr["sl"], tr["tp"], 0)
            hit = exit_reason(t, mid, mid, bars)
            if hit:
                self._close_pattern(coin, hit[0], mid, ts)
        # 2) entries: scan each coin once per new closed 15m candle
        bar_start = int(now.timestamp() // 900 * 900 * 1000)
        for coin in self.cfg.pattern_coins:
            if coin in self.pattern_trades or self.pattern_scanned.get(coin) == bar_start or coin not in mids:
                continue
            try:
                candles = [c for c in parse_candles(self.client.candles(coin, "15m", 250)) if c.t < bar_start]
            except (ApiError, KeyError, ValueError) as exc:
                log.warning("candles for %s unavailable: %s", coin, exc)
                continue
            self.pattern_scanned[coin] = bar_start
            sig = scan(candles)
            mid = mids[coin]
            if not sig or not ((sig.side > 0 and sig.sl < mid < sig.tp) or (sig.side < 0 and sig.tp < mid < sig.sl)):
                continue
            notional = risk_notional(equity, mid, sig.sl, self.cfg.risk_per_trade, equity * self.cfg.risk.max_position_pct)
            if notional < self.cfg.risk.min_trade_usd:
                continue
            self.pattern_trades[coin] = {"pattern": sig.pattern, "side": sig.side, "entry": mid, "sl": sig.sl, "tp": sig.tp,
                                         "size": sig.side * notional / mid, "opened": ts}
            log.info("pattern %s: %s -> %s, SL %.4g TP %.4g", coin, sig.pattern, "long" if sig.side > 0 else "short", sig.sl, sig.tp)
            self._append_csv("pattern_trades.csv", ["time", "coin", "event", "pattern", "side", "entry", "sl", "tp", "price"],
                             [ts, coin, "open", sig.pattern, sig.side, mid, sig.sl, sig.tp, mid])
        return {coin: tr["size"] * mids[coin] for coin, tr in self.pattern_trades.items() if coin in mids}

    def run(self, minutes: float | None = None) -> None:
        log.info("paper trading %d leader(s), start balance $%.2f, polling every %ss",
                 len(self.cfg.leaders), self.cfg.starting_balance, self.cfg.poll_seconds)
        deadline = time.monotonic() + minutes * 60 if minutes else None
        last_flush = time.monotonic()
        try:
            while deadline is None or time.monotonic() < deadline:
                started = time.monotonic()
                try:
                    self.tick()
                except Exception:  # never let one bad round kill a 24/7 bot
                    log.exception("unexpected error in tick")
                if time.monotonic() - last_flush > 600:
                    self.flush_replay()
                    last_flush = time.monotonic()
                time.sleep(max(1.0, self.cfg.poll_seconds - (time.monotonic() - started)))
        finally:
            self.flush_replay()
        log.info("run finished (time limit reached)")
