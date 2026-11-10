import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from copybot.config import BotConfig
from copybot.engine import Engine
from copybot.patterns import Candle, detect, exit_reason, PatternTrade, risk_notional, with_trend, Signal
from copybot.risk import RiskConfig


def series(closes, start=0):
    """Candles from a list of closes; high/low a hair around the close."""
    out = []
    for i, c in enumerate(closes):
        out.append(Candle(start + i * 900_000, c, c * 1.0005, c * 0.9995, c))
    return out


def double_bottom():
    # flat, drop to 95, bounce to 100, drop to 95.2, then break above 100
    path = [100] * 30
    path += [99, 98, 97, 96, 95, 96, 97, 98, 99, 100, 99, 98, 97, 96, 95.2, 96, 97, 98, 99, 99.8, 100.6]
    return series(path)


class DetectTest(unittest.TestCase):
    def test_double_bottom_breakout(self):
        cs = double_bottom()
        sig = detect(cs, len(cs) - 1)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.pattern, "double bottom")
        self.assertEqual(sig.side, 1)
        self.assertLess(sig.sl, 95.2)
        self.assertGreater(sig.tp, sig.entry)

    def test_no_signal_before_breakout(self):
        cs = double_bottom()
        self.assertIsNone(detect(cs, len(cs) - 2))

    def test_double_top_is_short(self):
        cs = series([2 * 100 - c.c for c in double_bottom()])
        sig = detect(cs, len(cs) - 1)
        self.assertEqual((sig.pattern, sig.side), ("double top", -1))

    def test_trend_filter(self):
        sig = Signal("x", 1, 100, 95, 110)
        self.assertIsNone(with_trend(sig, 100, 101))
        self.assertIs(with_trend(sig, 100, 99), sig)

    def test_exit_and_size(self):
        t = PatternTrade("BTC", "x", 1, 100, 95, 110, 0)
        self.assertEqual(exit_reason(t, 101, 94, 1)[0], "stop-loss")
        self.assertEqual(exit_reason(t, 111, 99, 1)[0], "take-profit")
        self.assertEqual(exit_reason(t, 101, 99, 96)[0], "time")
        self.assertAlmostEqual(risk_notional(1000, 100, 95, 0.01, 1000), 200)  # 5% stop * $200 = $10 = 1%


class FakeCandleClient:
    def __init__(self, candles, price):
        self.cs, self.price = candles, price

    def mids(self):
        return {"BTC": self.price}

    def size_decimals(self):
        return {"BTC": 5}

    def candles(self, coin, interval="15m", count=250):
        return [{"t": c.t, "o": c.o, "h": c.h, "l": c.l, "c": c.c} for c in self.cs]


class PatternEngineTest(unittest.TestCase):
    def test_opens_on_signal_and_closes_on_take_profit(self):
        import copybot.engine as eng
        cs = double_bottom()
        now = datetime.fromtimestamp(cs[-1].t / 1000 + 900, tz=timezone.utc) + timedelta(seconds=5)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = BotConfig(leaders=[], data_dir=tmp, strategy="patterns", pattern_coins=["BTC"], fee_bps=0, slippage_bps=0,
                            risk=RiskConfig(max_position_pct=1.0, max_leverage=5))
            client = FakeCandleClient(cs, 100.7)
            original = eng.scan
            eng.scan = lambda candles: detect(candles, len(candles) - 1)  # no trend filter on this tiny series
            try:
                e = Engine(cfg, client=client)
                fills = e.tick(now)
                self.assertEqual(len(fills), 1)
                self.assertGreater(e.broker.size("BTC"), 0)
                self.assertEqual(e.pattern_trades["BTC"]["pattern"], "double bottom")
                client.price = 110
                e.tick(now + timedelta(minutes=1))
                self.assertEqual(e.broker.positions, {})
                self.assertGreater(e.broker.realized_pnl, 0)
            finally:
                eng.scan = original
