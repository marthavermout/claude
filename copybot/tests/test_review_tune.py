import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from copybot.config import BotConfig, apply_overrides, load_overrides
from copybot.engine import Engine
from copybot.review import build_review, max_drawdown
from copybot.risk import RiskConfig
from copybot.tune import TuneRules, tune
from tests.test_engine import FakeClient

A, B, C, D = ("0x" + ch * 40 for ch in "abcd")
T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


class MultiClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.books = {A: {}, B: {}}

    def snapshot(self, address):
        from copybot.hyperliquid import LeaderSnapshot
        return LeaderSnapshot(address, 100_000.0, dict(self.books.get(address, {})))


class ReviewTuneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = BotConfig(leaders=[A, B], starting_balance=1000, fee_bps=4.5, slippage_bps=0,
                             data_dir=self.tmp.name, copy_existing_positions=True,
                             risk=RiskConfig(max_position_pct=1.0, max_leverage=3.0,
                                             daily_loss_limit_pct=0.9, max_drawdown_pct=0.9))
        self.client = MultiClient()
        self.picked = []

    def tearDown(self):
        self.tmp.cleanup()

    def selector(self, exclude, n):
        pool = [x for x in [C, D] if x not in exclude][:n]
        self.picked.append((set(exclude), n))
        return pool

    def simulate_bad_leader_a(self, hours=80, rounds=6):
        """Leader A repeatedly buys BTC and sells lower; leader B does nothing."""
        e = Engine(self.cfg, client=self.client)
        t = T0
        step = timedelta(hours=hours / (rounds * 2))
        for _ in range(rounds):
            self.client.prices["BTC"] = 50_000
            self.client.books[A] = {"BTC": 1.0}  # 50% long
            e.tick(t); t += step
            self.client.prices["BTC"] = 46_000
            self.client.books[A] = {}
            e.tick(t); t += step
        return t

    def test_max_drawdown(self):
        self.assertAlmostEqual(max_drawdown([100, 120, 90, 130]), 0.25)

    def test_review_attributes_losses_to_leader(self):
        self.simulate_bad_leader_a()
        r = build_review(self.tmp.name)
        self.assertLess(r.total_return, 0)
        stats = {s.address: s for s in r.leaders}
        self.assertLess(stats[A].shadow_return, -0.08)
        self.assertAlmostEqual(stats[B].shadow_return, 0.0)
        self.assertEqual(stats[A].closed_trades, 6)

    def test_tune_replaces_bad_leader_then_cools_down(self):
        end = self.simulate_bad_leader_a()
        d = tune(self.cfg, self.selector, rules=TuneRules(target_leaders=2), now=end)
        self.assertIn("vervangen", d.change)
        ov = load_overrides(self.tmp.name)
        self.assertEqual(ov["leaders"], [B, C])
        self.assertEqual(ov["blacklist"], [A])

        cfg2 = apply_overrides(BotConfig(leaders=[A, B], data_dir=self.tmp.name))
        self.assertEqual(cfg2.leaders, [B, C])

        d2 = tune(cfg2, self.selector, rules=TuneRules(target_leaders=2), now=end + timedelta(hours=1))
        self.assertIsNone(d2.change)  # cooldown, and A no longer active

    def test_not_judged_on_small_sample(self):
        end = self.simulate_bad_leader_a(hours=10)
        d = tune(self.cfg, self.selector, rules=TuneRules(target_leaders=2), now=end)
        self.assertIsNone(d.change)
        self.assertTrue(any("te weinig data" in o for o in d.observations))

    def test_bootstrap_fills_empty_seats(self):
        self.cfg.leaders = []
        d = tune(self.cfg, self.selector, now=T0)
        self.assertIn("aangevuld", d.change)
        self.assertEqual(load_overrides(self.tmp.name)["leaders"], [C, D])

    def test_if_due_skips(self):
        self.cfg.leaders = []
        tune(self.cfg, self.selector, now=T0)
        self.assertIsNone(tune(self.cfg, self.selector, now=T0 + timedelta(hours=2), if_due_hours=24))

    def test_halts_tighten_leverage_only(self):
        e = Engine(self.cfg, client=self.client)
        self.cfg.risk.daily_loss_limit_pct = 0.05
        self.client.books[A] = {"BTC": 2.0}
        for day in (1, 2):
            self.client.prices["BTC"] = 50_000
            e.tick(T0 + timedelta(days=day))
            self.client.prices["BTC"] = 45_000
            e.tick(T0 + timedelta(days=day, hours=1))
        d = tune(self.cfg, self.selector, rules=TuneRules(min_hours_to_judge=1000, target_leaders=2), now=T0 + timedelta(days=3))
        self.assertIn("hefboom", d.change)
        self.assertEqual(load_overrides(self.tmp.name)["risk"]["max_leverage"], 2.25)

    def test_journal_newest_first(self):
        self.cfg.leaders = []
        tune(self.cfg, self.selector, now=T0)
        tune(self.cfg, self.selector, now=T0 + timedelta(days=2))
        text = (__import__("pathlib").Path(self.tmp.name) / "JOURNAL.md").read_text()
        self.assertEqual(text.count("# Copybot logboek"), 1)
        self.assertLess(text.index("Review #2"), text.index("Review #1"))


if __name__ == "__main__":
    unittest.main()


class ExportTest(unittest.TestCase):
    def test_export_writes_dashboard(self):
        import json
        from pathlib import Path

        from copybot.export import export
        with tempfile.TemporaryDirectory() as tmp:
            cfg = BotConfig(leaders=[A], starting_balance=1000, fee_bps=0, slippage_bps=0, data_dir=tmp,
                            copy_existing_positions=True)
            client = FakeClient()
            client.leader = {"BTC": 0.4}
            Engine(cfg, client=client).tick(T0)
            out = export(cfg)
            data = json.loads(out.read_text())
            self.assertEqual(data["positions"][0]["coin"], "BTC")
            self.assertEqual(len(data["series"]), 1)
            self.assertTrue((Path(tmp) / "index.html").exists())
