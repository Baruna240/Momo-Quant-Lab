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
import metrics  # noqa: E402
import risk  # noqa: E402
import costs  # noqa: E402
from report import kelly  # noqa: E402
from strategies import RETIRED, build_variants  # noqa: E402
from wallet_lab import build_trips, parse_tx, sop_exit, trade_price, wallet_stats  # noqa: E402


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


def raw_tx(payer, sol_change, mint, tok_pre, tok_post, t, fee=5000):
    return {"blockTime": t, "slot": t, "transaction": {"message": {"accountKeys": [payer, "curve"]}},
            "meta": {"err": None, "fee": fee, "preBalances": [10_000_000_000, 0],
                     "postBalances": [10_000_000_000 + int(sol_change * 1e9), 0],
                     "preTokenBalances": [{"owner": payer, "mint": mint, "uiTokenAmount": {"uiAmount": tok_pre}}],
                     "postTokenBalances": [{"owner": payer, "mint": mint, "uiTokenAmount": {"uiAmount": tok_post}}]}}


class WalletLabTest(unittest.TestCase):
    def test_round_trip_with_partial_take_profit(self):
        rows = [parse_tx(raw_tx("W", -1.0, "Mpump", 0, 1000, 100)),
                parse_tx(raw_tx("W", 1.0, "Mpump", 1000, 500, 160)),     # jual 50% di 2x
                parse_tx(raw_tx("W", 0.5, "Mpump", 500, 0, 400))]        # sisanya di 1x
        trips = build_trips(rows)
        self.assertEqual(len(trips), 1)
        t = trips[0]
        self.assertTrue(t["closed"])
        self.assertAlmostEqual(t["first_sell_mult"], 2.0)
        self.assertAlmostEqual(t["first_sell_frac"], 0.5)
        s = wallet_stats(trips, {"Mpump": {"created": 98}})
        self.assertAlmostEqual(s["pnl_sol"], 0.5)
        self.assertAlmostEqual(s["med_delay"], 2)

    def test_transfer_and_unseen_buy_are_skipped(self):
        rows = [parse_tx(raw_tx("W", 0.3, "A", 100, 0, 10)),               # jual tanpa beli di jendela
                parse_tx(raw_tx("W", -1.0, "B", 0, 100, 20)),
                parse_tx(raw_tx("W", -0.000005, "B", 100, 0, 30))]         # token dipindah, bukan dijual
        self.assertEqual(build_trips(rows), [])

    def test_pool_price_with_two_buyers_in_one_tx(self):
        # bot membeli untuk 2 dompet dalam 1 transaksi: harga tetap = SOL masuk pool / token keluar pool
        tb = lambda owner, amt: {"owner": owner, "mint": "M", "uiTokenAmount": {"uiAmount": amt}}
        raw = {"transaction": {"message": {"accountKeys": ["W1", "POOL", "W2"]}},
               "meta": {"err": None, "fee": 5000, "preBalances": [5_000_000_000, 30_000_000_000, 0],
                        "postBalances": [4_000_000_000, 31_000_000_000, 0],
                        "preTokenBalances": [tb("POOL", 1_000_000)], "postTokenBalances": [
                            tb("POOL", 990_000), tb("W1", 4_000), tb("W2", 6_000)]}}
        self.assertAlmostEqual(trade_price(raw, "M"), 1.0 / 10_000)

    def test_sop_ladder_recovers_capital_then_holds(self):
        # 1,2x -> 1,5x -> 2x (60% terjual) -> puncak baru 2,5x: tutup modal -> leader keluar di 0,5x
        path = [(i, px) for i, px in enumerate([1.0, 1.2, 1.25, 1.5, 1.55, 2.0, 2.05, 2.5, 2.5, 1.0], start=1)]
        ret, why = sop_exit(path, 1.0, 100, 0.5, 1.0, 0.0, 0.0)
        self.assertEqual(why, "modal kembali, sisa ditahan")
        self.assertGreater(ret, 1.0)   # modal + fee sudah kembali walau sisanya dijual rugi

    def test_sop_trailing_after_sixty_percent(self):
        path = [(i, px) for i, px in enumerate([1.2, 1.2, 1.5, 1.5, 2.0, 2.0, 1.4, 1.3], start=1)]
        ret, why = sop_exit(path, 1.0, 100, 1.0, 1.0, 0.0, 0.0)
        self.assertEqual(why, "trailing")
        self.assertAlmostEqual(ret, 0.1 * 1.2 + 0.25 * 1.5 + 0.25 * 2.0 + 0.4 * 1.3 - 5 * 0.0005, places=6)


class MetricsTest(unittest.TestCase):
    def trades(self, pnls):
        return [{"closed": i, "pnl": p, "cost": 1.0, "fees": 0.02, "slip": 0.03} for i, p in enumerate(pnls)]

    def test_profit_factor_expectancy_drawdown(self):
        m = metrics.summarize(self.trades([2.0, -1.0, -1.0, 3.0, -1.0]))
        self.assertAlmostEqual(m["profit_factor"], 5 / 3)
        self.assertAlmostEqual(m["expectancy"], 0.4)
        self.assertAlmostEqual(m["win_rate"], 0.4)
        self.assertAlmostEqual(m["avg_rr"], 2.5)
        self.assertAlmostEqual(m["max_dd"], -2.0)       # puncak +2 lalu turun ke 0
        self.assertAlmostEqual(m["fees"], 0.10)
        self.assertAlmostEqual(m["slippage"], 0.15)

    def test_win_rate_alone_never_makes_a_candidate(self):
        # 90% menang tapi kerugian besar: PF < 1 -> tetap "rugi"
        m = metrics.summarize(self.trades([0.1] * 180 + [-2.0] * 20))
        self.assertGreater(m["win_rate"], 0.85)
        self.assertEqual(m["verdict"], "rugi")

    def test_profit_only_in_sample_is_flagged(self):
        m = metrics.summarize(self.trades([1.0, -0.5] * 70 + [-1.0, 0.2] * 30))
        self.assertGreater(m["is_pf"], 1)
        self.assertEqual(m["verdict"], "rugi di out-of-sample")


class RiskTest(unittest.TestCase):
    def setUp(self):
        self.kill = Path(__file__).resolve().parent / "_kill_test"
        self.kill.unlink(missing_ok=True)

    def tearDown(self):
        self.kill.unlink(missing_ok=True)

    def rm(self, mode):
        return risk.RiskManager(risk.RiskLimits(max_daily_loss=5, max_drawdown=10, dup_window_sec=60),
                                mode=mode, kill_file=self.kill)

    def test_kill_switch_and_duplicate_block_in_every_mode(self):
        for mode in ("shadow", "enforce"):
            rm = self.rm(mode)
            rm.on_open("s", "MINT", 1.0, now=1000)
            self.assertIn("order ganda", rm.check_entry("s", "MINT", 1.0, now=1030).blocked_by)
            self.assertTrue(rm.check_entry("s", "MINT", 1.0, now=1100).allowed)
            self.kill.touch()
            self.assertFalse(rm.check_entry("s", "LAIN", 1.0, now=1100).allowed)
            self.kill.unlink()

    def test_daily_loss_blocks_only_when_enforced_and_resets_next_day(self):
        for mode, allowed in (("shadow", True), ("enforce", False)):
            rm = self.rm(mode)
            for i in range(6):
                rm.on_open("s", f"m{i}", 1.0, now=100 + i)
                rm.on_close("s", 1.0, -1.0, now=200 + i)
            d = rm.check_entry("s", "baru", 1.0, now=300)
            self.assertEqual(d.allowed, allowed)
            self.assertTrue(any("rugi harian" in r for r in d.blocked_by + d.shadow))
            self.assertTrue(rm.check_entry("s", "besok", 1.0, now=300 + risk.DAY).allowed)

    def test_drawdown_halts_until_reset(self):
        rm = self.rm("enforce")
        for day in range(3):                       # rugi 4/hari: tidak kena batas harian, tapi drawdown 12 > 10
            rm.on_open("s", f"m{day}", 1.0, now=day * risk.DAY)
            rm.on_close("s", 1.0, -4.0, now=day * risk.DAY + 10)
        self.assertFalse(rm.check_entry("s", "x", 1.0, now=5 * risk.DAY).allowed)

    def test_replay_counts_blocked_trades(self):
        trades = [{"opened": i, "closed": i + 0.5, "pnl": -1.0, "cost": 1.0} for i in range(10)]
        kept, blocked = risk.replay(trades, risk.RiskLimits(max_daily_loss=3, max_drawdown=100))
        self.assertEqual((len(kept), blocked), (3, 7))


class CostModelTest(unittest.TestCase):
    def test_calibrated_slippage_is_asymmetric(self):
        s = load_settings()
        self.assertAlmostEqual(costs.slippage(s, 0.30, "buy"), s.slippage_min)       # beli tidak ikut gerak harga
        self.assertAlmostEqual(costs.slippage(s, 0.30, "sell"), s.slippage_min + s.slip_sell_k * 0.30)
        self.assertEqual(costs.slippage(s, 5.0, "sell"), s.slippage_max)             # selalu dibatasi
        self.assertLess(costs.slippage(s, 0.1, "buy"), costs.slippage(s, 0.1, "sell"))


if __name__ == "__main__":
    unittest.main()
