import unittest

from copybot.paper import PaperBroker


class PaperBrokerTest(unittest.TestCase):
    def broker(self):
        return PaperBroker(1000.0, fee_bps=0.0, slippage_bps=0.0)

    def test_long_profit(self):
        b = self.broker()
        b.execute("BTC", 0.01, 50_000)
        self.assertAlmostEqual(b.equity({"BTC": 51_000}), 1010.0)
        fill = b.execute("BTC", -0.01, 51_000)
        self.assertAlmostEqual(fill.realized, 10.0)
        self.assertEqual(b.positions, {})
        self.assertAlmostEqual(b.cash, 1010.0)

    def test_short_profit(self):
        b = self.broker()
        b.execute("ETH", -1, 2000)
        b.execute("ETH", 1, 1900)
        self.assertAlmostEqual(b.realized_pnl, 100.0)

    def test_average_entry_and_partial_close(self):
        b = self.broker()
        b.execute("SOL", 1, 100)
        b.execute("SOL", 1, 110)
        self.assertAlmostEqual(b.positions["SOL"].entry, 105)
        b.execute("SOL", -1, 120)
        self.assertAlmostEqual(b.realized_pnl, 15)
        self.assertAlmostEqual(b.positions["SOL"].size, 1)
        self.assertAlmostEqual(b.positions["SOL"].entry, 105)

    def test_flip(self):
        b = self.broker()
        b.execute("SOL", 1, 100)
        b.execute("SOL", -3, 90)
        self.assertAlmostEqual(b.realized_pnl, -10)
        self.assertAlmostEqual(b.positions["SOL"].size, -2)
        self.assertAlmostEqual(b.positions["SOL"].entry, 90)

    def test_fees_and_slippage(self):
        b = PaperBroker(1000.0, fee_bps=10, slippage_bps=10)
        fill = b.execute("BTC", 0.01, 50_000)
        self.assertAlmostEqual(fill.price, 50_050)
        self.assertAlmostEqual(fill.fee, 0.5005)
        self.assertLess(b.equity({"BTC": 50_000}), 1000.0)

    def test_roundtrip_dict(self):
        b = self.broker()
        b.execute("BTC", 0.01, 50_000)
        c = PaperBroker.from_dict(b.to_dict(), 0, 0)
        self.assertEqual(c.to_dict(), b.to_dict())


if __name__ == "__main__":
    unittest.main()
