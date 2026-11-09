import tempfile
import unittest
from datetime import datetime, timezone

from copybot.config import BotConfig
from copybot.engine import Engine
from copybot.hyperliquid import ApiError, LeaderSnapshot
from copybot.risk import RiskConfig

ADDR = "0x" + "a" * 40


class FakeClient:
    def __init__(self):
        self.prices = {"BTC": 50_000.0, "ETH": 2_000.0}
        self.leader = {"ETH": 10.0}  # pre-existing position
        self.fail = False

    def mids(self):
        if self.fail:
            raise ApiError("down")
        return dict(self.prices)

    def size_decimals(self):
        return {"BTC": 5, "ETH": 4}

    def snapshot(self, address):
        return LeaderSnapshot(address, 100_000.0, dict(self.leader))


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = BotConfig(leaders=[ADDR], starting_balance=1000, fee_bps=0, slippage_bps=0,
                             data_dir=self.tmp.name, risk=RiskConfig(max_drawdown_pct=0.2))
        self.client = FakeClient()

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self):
        return Engine(self.cfg, client=self.client)

    def now(self, day=9):
        return datetime(2026, 11, day, tzinfo=timezone.utc)

    def test_full_cycle(self):
        e = self.engine()
        self.assertEqual(e.tick(self.now()), [])  # existing ETH ignored
        self.client.leader["BTC"] = 0.4  # new: 20% long
        fills = e.tick(self.now())
        self.assertEqual([f.coin for f in fills], ["BTC"])
        self.assertAlmostEqual(e.broker.size("BTC"), 0.004)

        # state survives restart
        e2 = self.engine()
        self.assertAlmostEqual(e2.broker.size("BTC"), 0.004)
        self.assertEqual(e2.baselines[ADDR], {"ETH": 1})

        del self.client.leader["BTC"]
        self.client.prices["BTC"] = 55_000
        fills = e2.tick(self.now())
        self.assertAlmostEqual(fills[0].realized, 20.0)
        self.assertEqual(e2.broker.positions, {})

    def test_api_failure_does_nothing(self):
        e = self.engine()
        self.client.fail = True
        self.assertEqual(e.tick(self.now()), [])

    def test_kill_switch_closes_all(self):
        self.cfg.copy_existing_positions = True
        self.cfg.risk.max_position_pct = 1.0
        self.cfg.risk.max_leverage = 5.0
        self.client.leader = {"BTC": 8.0}  # 400% long
        e = self.engine()
        e.tick(self.now())
        self.assertGreater(e.broker.size("BTC"), 0)
        self.client.prices["BTC"] = 40_000  # -20% move at 1x (capped by max_position_pct) = -20% equity
        e.tick(self.now())
        self.assertEqual(e.broker.positions, {})
        self.assertTrue(e.risk.state.killed)


if __name__ == "__main__":
    unittest.main()
