import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from copybot.config import BotConfig
from copybot.engine import Engine
from copybot.fleet import SharedClient
from copybot.hyperliquid import LeaderSnapshot
from copybot.risk import RiskConfig


class CountingClient:
    def __init__(self):
        self.calls = {"mids": 0, "snap": 0}

    def mids(self):
        self.calls["mids"] += 1
        return {"BTC": 50_000.0}

    def snapshot(self, address):
        self.calls["snap"] += 1
        return LeaderSnapshot(address, 100_000.0, {"BTC": 0.4})

    def size_decimals(self):
        return {"BTC": 5}

    def asset_ctxs(self):
        return {}


class SharedClientTest(unittest.TestCase):
    def test_many_bots_one_request_per_trader(self):
        t = [0.0]
        inner = CountingClient()
        shared = SharedClient(inner, snapshot_ttl=4, clock=lambda: t[0])
        a = "0x" + "a" * 40
        with tempfile.TemporaryDirectory() as tmp:
            engines = [Engine(BotConfig(leaders=[a], data_dir=f"{tmp}/{i}", copy_existing_positions=True,
                                        risk=RiskConfig(max_position_pct=1.0)), client=shared) for i in range(6)]
            now = datetime(2026, 11, 12, tzinfo=timezone.utc)
            shared.new_round()
            for e in engines:
                e.tick(now)
            self.assertEqual(inner.calls, {"mids": 1, "snap": 1})
            # next round 5 s later: mids again, trader again (cache older than 4 s)
            t[0] = 5.0
            shared.new_round()
            for e in engines:
                e.tick(now + timedelta(seconds=5))
            self.assertEqual(inner.calls, {"mids": 2, "snap": 2})
            # a websocket fill makes the trader stale at once
            t[0] = 6.0
            shared.stale(a)
            shared.new_round()
            engines[0].tick(now + timedelta(seconds=6))
            self.assertEqual(inner.calls["snap"], 3)

    def test_history_rows_are_throttled_but_trades_are_not(self):
        import csv
        a = "0x" + "a" * 40
        with tempfile.TemporaryDirectory() as tmp:
            e = Engine(BotConfig(leaders=[a], data_dir=tmp, copy_existing_positions=True,
                                 risk=RiskConfig(max_position_pct=1.0)), client=CountingClient())
            e.log_every_s = 30
            t0 = datetime(2026, 11, 12, tzinfo=timezone.utc)
            for s in range(0, 60, 5):
                e.tick(t0 + timedelta(seconds=s))
            rows = list(csv.DictReader(open(f"{tmp}/equity.csv")))
            self.assertEqual(len(rows), 2)   # t=0 and t=30, not 12 rows
            self.assertEqual(len(list(csv.DictReader(open(f"{tmp}/trades.csv")))), 1)
