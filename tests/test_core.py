"""Unit tests for the core math. Run: python -m unittest discover tests"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alt_ai import proven_sides  # noqa: E402
from altcoin import MMR, AltSettings, AltStrategy, Book, Engine, Pos, pos_value  # noqa: E402
from config import load_settings  # noqa: E402
from meme_ai import simulate  # noqa: E402
from report import kelly  # noqa: E402
from strategies import RETIRED, build_variants  # noqa: E402


def fut_engine(lev=3):
    s = AltSettings()
    st = AltStrategy("t", "t", "", "1h", 0, market="futures", leverage=lev)
    book = Book(st, 100.0, 0)
    return Engine(s, {"t": book}, lambda sym: 0.0), book, s


class FuturesTest(unittest.TestCase):
    def test_long_liquidation_price(self):
        eng, book, _ = fut_engine(lev=3)
        pos = eng.buy(book, "XUSDT", 100.0, 0, stop=0.0, size=30.0, side=1)
        self.assertAlmostEqual(pos.liq, 100 * (1 - (1 / 3 - MMR)))
        self.assertGreater(pos.stop, pos.liq)   # stop is always placed before liquidation

    def test_short_profits_when_price_falls(self):
        eng, book, s = fut_engine(lev=2)
        pos = eng.buy(book, "XUSDT", 100.0, 0, stop=200.0, size=20.0, side=-1)
        self.assertLess(pos.stop, pos.liq)
        margin = pos.qty * pos.entry / pos.lev
        self.assertAlmostEqual(pos_value(pos, 90.0), margin + pos.qty * 10.0)
        eng.sell(book, pos, 90.0, 1, "test")
        pnl, ret = book.closed[-1]
        self.assertGreater(pnl, 0)

    def test_gap_through_liquidation_loses_margin(self):
        eng, book, _ = fut_engine(lev=3)
        pos = eng.buy(book, "XUSDT", 100.0, 0, stop=0.0, size=30.0, side=1)
        cash_before = book.cash
        self.assertTrue(eng.check_stop(book, pos, low=50.0, high=50.0, ts=1, gap_open=50.0))
        self.assertEqual(book.cash, cash_before)       # nothing returned
        self.assertEqual(book.closed[-1][1], 0.0)

    def test_funding_long_pays_positive_rate(self):
        eng, book, _ = fut_engine(lev=2)
        pos = eng.buy(book, "XUSDT", 100.0, 0, stop=0.0, size=20.0, side=1)
        eng.apply_funding(book, {"XUSDT": 100.0}, {"XUSDT": 0.001})
        self.assertAlmostEqual(pos.funding, pos.qty * 100.0 * 0.001)

    def test_spot_position_value(self):
        p = Pos("s", "XUSDT", 0, 10.0, 2.0, 20.0, 5.0, 10.0)
        self.assertFalse(p.is_fut)
        self.assertEqual(pos_value(p, 12.0), 24.0)


class AiGateTest(unittest.TestCase):
    def test_only_proven_sides(self):
        meta = {"long_n": 500, "long_hit": 0.56, "long_ret": 0.4,
                "short_n": 96, "short_hit": 0.70, "short_ret": 1.6}   # short has too few signals
        self.assertEqual(proven_sides(meta), {1})
        self.assertEqual(proven_sides({}), set())


class KellyTest(unittest.TestCase):
    def test_kelly_formula(self):
        cfg = SimpleNamespace(kelly_min_sample=30)
        k = kelly(100, 40, 40 * 3.0, 60 * 1.0, cfg)    # p=0.4, b=3 -> f = 0.4 - 0.6/3 = 0.2
        self.assertAlmostEqual(k["f"], 0.2)
        self.assertEqual(kelly(10, 5, 5, 5, cfg)["status"], "belajar")


class MemeExitSimTest(unittest.TestCase):
    def setUp(self):
        self.s = load_settings()
        self.c = next(v for v in build_variants(self.s) if v.name == "filter")

    def test_flat_coin_loses_only_costs(self):
        flat = [(a, 1.5, 10) for a in range(0, 1200, 60)]
        r = simulate(flat, 90, self.c, self.s, tx_frac=0.0)
        self.assertLess(r, 1.0)
        self.assertGreater(r, 0.9)

    def test_dump_hits_stop_loss(self):
        dump = [(0, 1.5, 10), (90, 1.5, 10), (94, 1.2, 8), (98, 0.5, 2), (160, 0.4, 1)]
        self.assertLess(simulate(dump, 90, self.c, self.s, tx_frac=0.0), 0.5)

    def test_pump_takes_profit(self):
        pump = [(a, 1.5 * (1 + (a - 90) / 30), 20) for a in range(90, 300, 4)]
        self.assertGreater(simulate(pump, 90, self.c, self.s, tx_frac=0.0), 1.5)


class VariantsTest(unittest.TestCase):
    def test_retired_variants_not_built(self):
        names = {v.name for v in build_variants(load_settings())}
        self.assertFalse(names & RETIRED)


if __name__ == "__main__":
    unittest.main()
