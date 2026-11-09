"""The main loop: read leaders -> compute targets -> apply risk -> trade on paper."""

from __future__ import annotations

import csv
import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .config import BotConfig
from .hyperliquid import ApiError, HyperliquidClient
from .paper import PaperBroker
from .risk import RiskManager, RiskState
from .strategy import filter_baseline, target_notionals

log = logging.getLogger("copybot")


class Engine:
    def __init__(self, cfg: BotConfig, client: HyperliquidClient | None = None):
        self.cfg = cfg
        self.client = client or HyperliquidClient()
        self.dir = Path(cfg.data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.trades_path = self.dir / "trades.csv"
        self.equity_path = self.dir / "equity.csv"
        self.baselines: dict[str, dict[str, int]] = {}
        self.broker = PaperBroker(cfg.starting_balance, cfg.fee_bps, cfg.slippage_bps)
        self.risk = RiskManager(cfg.risk)
        self._decimals: dict[str, int] | None = None
        self._load()

    # ---------- persistence ----------
    def _load(self) -> None:
        if not self.state_path.exists():
            return
        data = json.loads(self.state_path.read_text())
        self.broker = PaperBroker.from_dict(data["broker"], self.cfg.fee_bps, self.cfg.slippage_bps)
        self.risk.state = RiskState(**data["risk"])
        self.baselines = data.get("baselines", {})

    def save(self) -> None:
        data = {"broker": self.broker.to_dict(), "risk": asdict(self.risk.state), "baselines": self.baselines}
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.state_path)

    def _append_csv(self, path: Path, header: list[str], row: list) -> None:
        new = not path.exists()
        with path.open("a", newline="") as fh:
            writer = csv.writer(fh)
            if new:
                writer.writerow(header)
            writer.writerow(row)

    # ---------- trading ----------
    def _round(self, coin: str, size: float) -> float:
        if self._decimals is None:
            self._decimals = self.client.size_decimals()
        return round(size, self._decimals.get(coin, 4))

    def tick(self, now: datetime | None = None) -> list:
        now = now or datetime.now(timezone.utc)
        try:
            mids = self.client.mids()
            snaps = [self.client.snapshot(a) for a in self.cfg.leaders]
        except ApiError as exc:
            log.warning("data unavailable, skipping this round (no trades): %s", exc)
            return []

        if not self.cfg.copy_existing_positions:
            for i, snap in enumerate(snaps):
                if snap.address not in self.baselines:
                    self.baselines[snap.address] = {c: (1 if s > 0 else -1) for c, s in snap.positions.items()}
                    if snap.positions:
                        log.info("%s: ignoring %d existing positions until they close: %s",
                                 snap.address[:10], len(snap.positions), ", ".join(snap.positions))
                snaps[i] = filter_baseline(snap, self.baselines[snap.address])

        equity = self.broker.equity(mids)
        halt = self.risk.halt_reason(equity, now.strftime("%Y-%m-%d"))
        if halt:
            if self.broker.positions:
                log.warning("RISK STOP: %s -> closing all positions", halt)
            targets = {}
        else:
            raw = target_notionals(snaps, mids, equity, self.cfg.copy_ratio)
            targets = self.risk.apply_limits(raw, equity)

        fills = []
        for coin in sorted(set(targets) | set(self.broker.positions)):
            mid = mids.get(coin)
            if not mid:
                continue
            desired = self._round(coin, targets.get(coin, 0.0) / mid)
            current = self.broker.size(coin)
            delta = desired - current
            closing = desired == 0 and current != 0
            if delta == 0 or (not closing and abs(delta) * mid < self.cfg.risk.min_trade_usd):
                continue
            fill = self.broker.execute(coin, delta, mid)
            fills.append(fill)
            side = "BUY " if delta > 0 else "SELL"
            log.info("%s %s %g @ %.4f  fee %.4f  realized %+.2f", side, coin, abs(delta), fill.price, fill.fee, fill.realized)
            self._append_csv(self.trades_path, ["time", "coin", "size", "price", "fee", "realized_pnl"],
                             [now.isoformat(), coin, delta, fill.price, fill.fee, fill.realized])

        equity = self.broker.equity(mids)
        self._append_csv(self.equity_path, ["time", "equity", "positions", "halt"],
                         [now.isoformat(), round(equity, 4), len(self.broker.positions), halt or ""])
        self.save()
        return fills

    def run(self) -> None:
        log.info("paper trading %d leader(s), start balance $%.2f, polling every %ss",
                 len(self.cfg.leaders), self.cfg.starting_balance, self.cfg.poll_seconds)
        while True:
            started = time.monotonic()
            try:
                self.tick()
            except Exception:  # never let one bad round kill a 24/7 bot
                log.exception("unexpected error in tick")
            time.sleep(max(1.0, self.cfg.poll_seconds - (time.monotonic() - started)))
