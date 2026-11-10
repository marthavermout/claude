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
        self.broker = PaperBroker(cfg.starting_balance, cfg.fee_bps, cfg.slippage_bps)
        self.risk = RiskManager(cfg.risk)
        self._decimals: dict[str, int] | None = None
        self.record_replay = True
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

    def save(self) -> None:
        data = {
            "broker": self.broker.to_dict(),
            "risk": asdict(self.risk.state),
            "baselines": self.baselines,
            "shadows": {a: b.to_dict() for a, b in self.shadows.items()},
            "shadow_started": self.shadow_started,
            "meta": self.meta,
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
        try:
            mids = self.client.mids()
            raw_snaps = [self.client.snapshot(a) for a in self.cfg.leaders]
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
                snap = filter_baseline(snap, self.baselines[snap.address])
            snaps.append(snap)

        if self.record_replay:
            coins = {"BTC"} | set(self.broker.positions)
            for snap in raw_snaps:
                coins |= set(snap.positions)
            self._replay.append({
                "t": ts,
                "mids": {c: mids[c] for c in sorted(coins) if c in mids},
                "dec": {c: self._decimals_for(c) for c in sorted(coins)},
                "leaders": {s.address: {"equity": s.equity, "positions": s.positions} for s in raw_snaps},
            })

        # shadow books: one per leader, no halts, same position limits
        for raw, snap in zip(raw_snaps, snaps):
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
            self._append_csv("leaders.csv", ["time", "leader", "leader_equity", "shadow_equity"],
                             [ts, snap.address, round(raw.equity, 2), round(shadow.equity(mids), 4)])

        # the real paper book
        equity = self.broker.equity(mids)
        halt = self.risk.halt_reason(equity, now.strftime("%Y-%m-%d"))
        if halt:
            if self.broker.positions:
                log.warning("RISK STOP: %s -> closing all positions", halt)
            targets = {}
        else:
            targets = self.risk.apply_limits(target_notionals(snaps, mids, equity, self.cfg.copy_ratio), equity)

        fills = self._rebalance(self.broker, targets, mids)
        for f in fills:
            log.info("%s %s %g @ %.4f  fee %.4f  realized %+.2f",
                     "BUY " if f.size > 0 else "SELL", f.coin, abs(f.size), f.price, f.fee, f.realized)
            self._append_csv("trades.csv", TRADE_HEADER, [ts, f.coin, f.size, f.price, f.fee, f.realized])

        equity = self.broker.equity(mids)
        self._append_csv("equity.csv", ["time", "equity", "positions", "btc", "halt"],
                         [ts, round(equity, 4), len(self.broker.positions), mids.get("BTC", ""), halt or ""])
        self.save()
        return fills

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
