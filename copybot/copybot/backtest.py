"""Replay recorded history (data/replay/*.jsonl.gz) through the current code.

Used to test whether a code or settings change would have done better on the
same real market data. The history is split in time: the first part
(in-sample) is where you look for ideas, the last part (out-of-sample) is the
exam the idea has to pass. Only the out-of-sample result counts."""

from __future__ import annotations

import copy
import gzip
import json
import logging
import tempfile
from datetime import datetime
from pathlib import Path

from .config import BotConfig
from .engine import Engine
from .hyperliquid import LeaderSnapshot
from .review import build_review


def load_records(data_dir: str | Path, start: str | None = None, end: str | None = None) -> list[dict]:
    records = []
    for path in sorted((Path(data_dir) / "replay").glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as fh:
            records += [json.loads(line) for line in fh if line.strip()]
    records.sort(key=lambda r: r["t"])
    if start:
        records = [r for r in records if r["t"] >= start]
    if end:
        records = [r for r in records if r["t"] < end]
    return records


class ReplayClient:
    def __init__(self):
        self.rec: dict = {}
        self.decimals: dict[str, int] = {}

    def load(self, rec: dict) -> None:
        self.rec = rec
        self.decimals.update(rec.get("dec", {}))

    def mids(self) -> dict[str, float]:
        return dict(self.rec["mids"])

    def size_decimals(self) -> dict[str, int]:
        return self.decimals  # same dict object: stays current as records load

    def snapshot(self, address: str) -> LeaderSnapshot:
        ldr = self.rec["leaders"][address]
        return LeaderSnapshot(address, ldr["equity"], dict(ldr["positions"]), dict(ldr.get("entries", {})))


def run_backtest(cfg: BotConfig, records: list[dict]) -> dict:
    """Run the engine over records in a throwaway data dir and return the metrics."""
    if not records:
        return {"records": 0}
    cfg = copy.deepcopy(cfg)
    log = logging.getLogger("copybot")
    level = log.level
    log.setLevel(logging.WARNING)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cfg.data_dir = tmp
            client = ReplayClient()
            engine = Engine(cfg, client=client)
            engine.record_replay = False
            for rec in records:
                client.load(rec)
                engine.cfg.leaders = list(rec["leaders"])
                engine.tick(datetime.fromisoformat(rec["t"]))
            r = build_review(tmp)
            return {
                "records": len(records),
                "from": records[0]["t"],
                "to": records[-1]["t"],
                "hours": round(r.hours, 1),
                "final_equity": round(r.end_equity, 2),
                "total_return": round(r.end_equity / cfg.starting_balance - 1, 5),
                "btc_return": round(r.btc_return, 5) if r.btc_return is not None else None,
                "max_drawdown": round(r.max_drawdown, 5),
                "trades": r.trades,
                "fees": round(r.fees, 2),
                "realized": round(r.realized, 2),
                "churn_ratio": round(r.churn_ratio, 4),
                "halt_days": len(r.halt_days),
            }
    finally:
        log.setLevel(level)


def split_backtest(cfg: BotConfig, records: list[dict], split: float = 0.7) -> dict:
    if not records:
        return {"error": "no replay data yet (data/replay is empty)"}
    t0 = datetime.fromisoformat(records[0]["t"])
    t1 = datetime.fromisoformat(records[-1]["t"])
    cut = (t0 + (t1 - t0) * split).isoformat()
    return {
        "split_at": cut,
        "in_sample": run_backtest(cfg, [r for r in records if r["t"] < cut]),
        "out_of_sample": run_backtest(cfg, [r for r in records if r["t"] >= cut]),
    }


def apply_sets(cfg: BotConfig, sets: list[str]) -> BotConfig:
    """--set risk.min_trade_usd=30 / --set copy_ratio=0.5"""
    for item in sets:
        key, _, raw = item.partition("=")
        value = json.loads(raw) if raw not in ("", None) else None
        target = cfg
        parts = key.split(".")
        for part in parts[:-1]:
            target = getattr(target, part)
        if not hasattr(target, parts[-1]):
            raise ValueError(f"unknown setting: {key}")
        setattr(target, parts[-1], value)
    return cfg
