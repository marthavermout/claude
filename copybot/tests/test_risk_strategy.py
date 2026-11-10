import unittest

from copybot.hyperliquid import LeaderSnapshot
from copybot.risk import RiskConfig, RiskManager
from copybot.selector import SelectionRules, screen
from copybot.strategy import filter_baseline, target_notionals


class StrategyTest(unittest.TestCase):
    def test_proportional(self):
        ldr = LeaderSnapshot("0xa", 100_000, {"BTC": 0.4})  # $20k = 20% long
        t = target_notionals([ldr], {"BTC": 50_000}, 1000)
        self.assertAlmostEqual(t["BTC"], 200)

    def test_multiple_leaders_split_equity(self):
        a = LeaderSnapshot("0xa", 100_000, {"BTC": 0.4})
        b = LeaderSnapshot("0xb", 10_000, {"BTC": -0.04})  # 20% short
        t = target_notionals([a, b], {"BTC": 50_000}, 1000)
        self.assertAlmostEqual(t["BTC"], 0)

    def test_baseline_ignored_until_closed(self):
        base = {"BTC": 1}
        snap = LeaderSnapshot("0xa", 1000, {"BTC": 1, "ETH": 2})
        self.assertEqual(filter_baseline(snap, base).positions, {"ETH": 2})
        filter_baseline(LeaderSnapshot("0xa", 1000, {}), base)
        self.assertEqual(base, {})
        self.assertEqual(filter_baseline(snap, base).positions, {"BTC": 1, "ETH": 2})

    def test_baseline_flip_is_copied(self):
        base = {"BTC": 1}
        snap = LeaderSnapshot("0xa", 1000, {"BTC": -1})
        self.assertEqual(filter_baseline(snap, base).positions, {"BTC": -1})


class RiskTest(unittest.TestCase):
    def test_caps(self):
        r = RiskManager(RiskConfig(max_leverage=1.0, max_position_pct=0.5, blocked_coins=["DOGE"]))
        out = r.apply_limits({"BTC": 900, "ETH": -400, "SOL": 300, "DOGE": 100}, 1000)
        self.assertNotIn("DOGE", out)
        self.assertAlmostEqual(sum(abs(v) for v in out.values()), 1000)
        self.assertAlmostEqual(out["BTC"] / out["ETH"], -500 / 400)

    def test_daily_limit_resets_next_day(self):
        r = RiskManager(RiskConfig(daily_loss_limit_pct=0.05, max_drawdown_pct=0.5))
        self.assertIsNone(r.halt_reason(1000, "d1"))
        self.assertIn("daily", r.halt_reason(940, "d1"))
        self.assertIsNone(r.halt_reason(940, "d2"))

    def test_drawdown_kill_is_sticky(self):
        r = RiskManager(RiskConfig(daily_loss_limit_pct=0.9, max_drawdown_pct=0.2))
        r.halt_reason(1000, "d1")
        self.assertIn("drawdown", r.halt_reason(790, "d2"))
        self.assertIn("drawdown", r.halt_reason(2000, "d3"))


class SelectorTest(unittest.TestCase):
    def row(self, addr, acct, week, month, alltime, alltime_pnl, month_vlm):
        return {"ethAddress": addr, "accountValue": str(acct), "windowPerformances": [
            ["week", {"pnl": "0", "roi": str(week), "vlm": "0"}],
            ["month", {"pnl": "0", "roi": str(month), "vlm": str(month_vlm)}],
            ["allTime", {"pnl": str(alltime_pnl), "roi": str(alltime), "vlm": "0"}]]}

    def test_filters(self):
        rows = [
            self.row("0xgood", 500_000, 0.02, 0.10, 0.8, 400_000, 2_000_000),
            self.row("0xsmall", 5_000, 0.02, 0.10, 0.8, 400_000, 20_000),
            self.row("0xlucky", 500_000, 0.5, 3.0, 3.0, 400_000, 2_000_000),
            self.row("0xmm", 500_000, 0.02, 0.10, 0.8, 400_000, 500_000_000),
            self.row("0xloser", 500_000, 0.02, 0.10, -0.2, -50_000, 2_000_000),
        ]
        self.assertEqual([c.address for c in screen(rows, SelectionRules())], ["0xgood"])


if __name__ == "__main__":
    unittest.main()
