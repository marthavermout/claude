import tempfile
import unittest
from datetime import datetime, timezone

from copybot import quant
from copybot.config import BotConfig
from copybot.engine import Engine
from copybot.paper import PaperBroker
from copybot.risk import RiskConfig


class FundingPaymentTest(unittest.TestCase):
    def test_longs_pay_shorts_receive(self):
        b = PaperBroker(1000, fee_bps=0, slippage_bps=0)
        b.execute("BTC", 0.01, 50_000)   # $500 long
        b.execute("ETH", -1, 2_000)      # $2000 short
        paid = b.apply_funding({"BTC": 0.0001, "ETH": 0.0001}, {"BTC": 50_000, "ETH": 2_000})
        self.assertAlmostEqual(paid, 0.05 - 0.2)
        self.assertAlmostEqual(b.cash, 1000 + 0.15)
        self.assertAlmostEqual(PaperBroker.from_dict(b.to_dict(), 0, 0).funding_paid, b.funding_paid)


class TrendTest(unittest.TestCase):
    def test_direction_and_size(self):
        up = [100 * (1.001 ** i) * (1 + 0.004 * ((-1) ** i)) for i in range(400)]
        down = list(reversed(up))
        self.assertGreater(quant.trend_target(up, 1000, 0.25), 0)
        self.assertLess(quant.trend_target(down, 1000, 0.25), 0)
        self.assertLessEqual(abs(quant.trend_target(up, 1000, 0.25)), 250)
        self.assertEqual(quant.trend_target(up[:50], 1000, 0.25), 0)

    def test_keep_or_resize(self):
        self.assertEqual(quant.keep_or_resize(100, 110), 100)   # within the band: keep
        self.assertEqual(quant.keep_or_resize(100, 200), 200)   # far off: resize
        self.assertEqual(quant.keep_or_resize(100, -90), -90)   # flip: follow


class FundingStrategyTest(unittest.TestCase):
    def test_open_on_high_funding_and_close_when_normal(self):
        targets, pos, ev = quant.funding_targets({"BTC": 0.0001, "ETH": 0.00001}, {"BTC": 100, "ETH": 10}, {}, 1000)
        self.assertEqual(list(pos), ["BTC"])
        self.assertLess(targets["BTC"], 0)          # crowd is long -> we go short and receive
        targets, pos, ev = quant.funding_targets({"BTC": 0.00001}, {"BTC": 100}, pos, 1000)
        self.assertEqual(pos, {})
        self.assertIn(("BTC", "funding normaal"), ev)

    def test_stop_loss(self):
        _, pos, _ = quant.funding_targets({"BTC": -0.0001}, {"BTC": 100}, {}, 1000)   # long
        _, pos, ev = quant.funding_targets({"BTC": -0.0001}, {"BTC": 95}, pos, 1000)
        self.assertIn(("BTC", "stop-loss"), ev)


class FakeQuantClient:
    def __init__(self, closes, rate=0.0):
        self.closes, self.rate, self.price = closes, rate, closes[-1]

    def mids(self):
        return {"BTC": self.price}

    def size_decimals(self):
        return {"BTC": 5}

    def candles(self, coin, interval="1h", count=600):
        return [{"t": i * 3_600_000, "o": c, "h": c, "l": c, "c": c} for i, c in enumerate(self.closes)]

    def asset_ctxs(self):
        return {"BTC": {"funding": self.rate, "mark": self.price, "volume": 1e9}}


class QuantEngineTest(unittest.TestCase):
    def test_trend_bot_goes_long_and_pays_funding_hourly(self):
        closes = [100 * (1.002 ** i) * (1 + 0.004 * ((-1) ** i)) for i in range(400)]
        with tempfile.TemporaryDirectory() as tmp:
            cfg = BotConfig(leaders=[], data_dir=tmp, strategy="trend", pattern_coins=["BTC"], fee_bps=0, slippage_bps=0,
                            risk=RiskConfig(max_position_pct=0.25, max_leverage=2))
            client = FakeQuantClient(closes, rate=0.0001)
            e = Engine(cfg, client=client)
            t0 = datetime(2026, 11, 11, 10, 0, 30, tzinfo=timezone.utc)
            e.tick(t0)
            self.assertGreater(e.broker.size("BTC"), 0)
            self.assertEqual(e.broker.funding_paid, 0)       # first tick only notes the hour
            e.tick(t0.replace(hour=11))
            self.assertGreater(e.broker.funding_paid, 0)     # long pays positive funding


class ImprovementsTest(unittest.TestCase):
    def test_fee_drag_counts_open_profit(self):
        """A bot that is up $50 on open positions with $1 of fees must not be flagged for fees."""
        import csv
        from copybot.review import build_review
        with tempfile.TemporaryDirectory() as tmp:
            with open(f"{tmp}/equity.csv", "w", newline="") as fh:
                w = csv.writer(fh); w.writerow(["time", "equity", "positions", "btc", "halt"])
                w.writerow(["2026-11-10T00:00:00+00:00", 1000, 0, 70000, ""])
                w.writerow(["2026-11-11T00:00:00+00:00", 1050, 3, 70000, ""])
            with open(f"{tmp}/trades.csv", "w", newline="") as fh:
                w = csv.writer(fh); w.writerow(["time", "coin", "size", "price", "fee", "realized_pnl"])
                w.writerow(["2026-11-10T01:00:00+00:00", "BTC", 0.01, 70000, 1.0, 0.4])
            r = build_review(tmp)
            self.assertLess(r.fee_drag, 0.05)

    def test_follow_bot_mirrors_leaders(self):
        import json, os
        from copybot.config import apply_overrides
        a, b = "0x" + "a" * 40, "0x" + "b" * 40
        with tempfile.TemporaryDirectory() as tmp:
            json.dump({"leaders": [a]}, open(f"{tmp}/overrides.json", "w"))
            first = apply_overrides(BotConfig(leaders=[], data_dir=f"{tmp}/bots/klein", follow_bot="../.."))
            self.assertEqual(first.leaders, [a])  # own folder does not exist yet
            os.makedirs(f"{tmp}/bots/klein")
            json.dump({"leaders": [b], "risk": {"max_leverage": 1.5}}, open(f"{tmp}/bots/klein/overrides.json", "w"))
            cfg = apply_overrides(BotConfig(leaders=[], data_dir=f"{tmp}/bots/klein", follow_bot="../.."))
            self.assertEqual(cfg.leaders, [a])
            self.assertEqual(cfg.risk.max_leverage, 1.5)

    def test_pattern_needs_enough_reward(self):
        from copybot.patterns import Signal, reward_risk
        self.assertAlmostEqual(reward_risk(Signal("x", 1, 100, 98, 103)), 1.5)
        self.assertLess(reward_risk(Signal("x", -1, 100, 104, 98)), 1.5)
