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
import flow  # noqa: E402
from meme_engine import ExitState, exit_orders  # noqa: E402
import safety  # noqa: E402
import indicators as ind  # noqa: E402
import research  # noqa: E402
import sqlite3  # noqa: E402
from agents.base import Agent  # noqa: E402
from agents.guards import RiskAgent, SafetyAgent  # noqa: E402
from agents.scanners import MemeScannerAgent  # noqa: E402
from agents.supervisor import Supervisor, latest_cycle  # noqa: E402
import altcoin  # noqa: E402
import numpy as np  # noqa: E402
import tempfile  # noqa: E402
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


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ft = flow.FlowTracker(Path(self.tmp.name) / "f.db", clock=lambda: 0)
        self.tok = SimpleNamespace(mint="M", symbol="X", launch_price=30 / 1000)

    def tearDown(self):
        self.ft.db.close()
        self.tmp.cleanup()

    def trade(self, t, who, side, sol, tok, v_sol, v_tok=1000.0):
        self.ft.on_trade({"mint": "M", "txType": side, "traderPublicKey": who, "solAmount": sol, "tokenAmount": tok,
                          "vSolInBondingCurve": v_sol, "vTokensInBondingCurve": v_tok, "pool": "pump"}, now=t)

    def test_snapshot_missed_sol_and_rug_label(self):
        self.ft.start(self.tok, "DEV", dev_tokens=100.0, launch_v_sol=30.0, now=0)
        self.assertEqual(self.ft.take_changes(), (["M"], []))
        self.trade(1.0, "A", "buy", 1.0, 10, 34.0)      # 3 SOL masuk sebelum transaksi pertama yang kita lihat
        self.trade(2.0, "B", "buy", 1.0, 10, 35.0)
        self.trade(5.0, "A", "sell", 0.5, 5, 34.5)      # A beli lalu jual = round-trip
        self.trade(20.0, "DEV", "sell", 2.0, 80, 32.5)  # dev jual 80%
        self.trade(40.0, "C", "buy", 0.1, 1, 6.0)       # harga jatuh jauh dari puncak
        it = self.ft.items["M"]
        self.assertAlmostEqual(it["missed"], 3.0)
        snap = flow.snapshot(it["raw"], 60, "DEV", 100.0, set())
        self.assertEqual((snap["n_buy"], snap["n_sell"], snap["buyers"], snap["snipers"]), (3, 2, 3, 2))
        self.assertEqual(snap["round_trippers"], 1)
        self.assertAlmostEqual(snap["dev_sell_frac"], 0.8)
        self.ft.tick(now=flow.TRACK_SEC + 1)
        row = self.ft.db.execute("SELECT label, dev_sold_frac, n_trades FROM token_flow WHERE mint='M'").fetchone()
        self.assertEqual(row, ("rug", 0.8, 5))
        self.assertEqual(self.ft.take_changes(), ([], ["M"]))   # berhenti berlangganan setelah 20 menit

    def test_label_rules(self):
        self.assertEqual(flow.label_token(3.0, 0.5, 2.0, 0.0, True), "lulus")
        self.assertEqual(flow.label_token(1.1, 0.9, 1.0, 0.0, False), "datar")
        self.assertEqual(flow.label_token(2.0, 0.1, 0.2, 0.1, False), "dump")
        self.assertEqual(flow.label_token(2.0, 0.6, 1.5, 0.0, False), "normal")

    def test_safety_layers_add_up_and_cap(self):
        clean, why = safety.assess({"buyers": 30, "n_buy": 30, "n_sell": 10, "buy_ratio": 0.7, "top1_share": 0.1}, {}, 0.2)
        self.assertEqual((clean, why), (0.0, []))
        bad, why = safety.assess({"buyers": 3, "n_buy": 6, "n_sell": 6, "buy_ratio": 0.3, "top1_share": 0.6, "wash_share": 0.5,
                                  "same_size": 0.8, "snipers": 9, "serial_snipers": 4, "dev_sell_frac": 0.5},
                                 {"creator_n24": 5, "dev_pct": 20, "name_n1h": 3}, 5.0, {"top1": 0.3, "top5": 0.6})
        self.assertEqual(bad, 100.0)
        self.assertGreater(len(why), 8)


class ExitEngineTest(unittest.TestCase):
    RULES = SimpleNamespace(stop_loss=0.4, tp1_mult=2.0, tp1_sell=0.5, tp2_mult=4.0, tp2_sell=0.25, trailing_stop=0.3,
                            max_hold_min=30, max_hold_after_tp_min=180, stale_min=10)

    def play(self, prices, step=10):
        st = ExitState(qty0=100, remaining=100, entry=1.0, peak=1.0, opened=0, last_change=0)
        out, last = [], 1.0
        for i, px in enumerate(prices, 1):
            now = i * step
            if px != last:
                st.last_change, last = now, px
            for q, why in exit_orders(self.RULES, st, px, now):
                st.remaining -= q
                out.append((round(q, 6), why))
            if st.remaining <= 1e-9:
                break
        return out, st

    def test_stop_loss(self):
        self.assertEqual(self.play([0.9, 0.59])[0], [(100, "stop loss")])

    def test_tp1_tp2_then_trailing(self):
        out, _ = self.play([2.1, 4.5, 3.0])
        self.assertEqual(out, [(50, "TP1 2x"), (25, "TP2 4x"), (25, "trailing stop")])

    def test_time_limit_after_tp1_now_applies_everywhere(self):
        prices = [2.1] + [2.0 + 0.001 * i for i in range(1, 200)]      # naik pelan terus: tidak kena trailing / koin mati
        out, _ = self.play(prices, step=60)
        self.assertEqual(out[-1][1], "batas waktu")
        self.assertEqual(len(out), 2)

    def test_stale_coin(self):
        self.assertEqual(self.play([1.1] * 70, step=10)[0], [(100, "koin mati (harga diam)")])

    def test_replay_uses_same_engine(self):
        # replay (meme_ai.simulate) harus menghasilkan keluaran yang sama dengan menjalankan mesin langsung
        s = SimpleNamespace(fee_pct=0.0, slippage_min=0.0, slippage_max=0.0, slip_buy_k=0.0, slip_sell_k=0.0)
        path = [(0, 1.0, 0), (10, 2.1, 0), (20, 4.5, 0), (30, 3.0, 0)]
        ret = simulate(path, 0, self.RULES, s, 0.0)
        self.assertAlmostEqual(ret, 0.5 * 2.1 + 0.25 * 4.5 + 0.25 * 3.0)


class IndicatorTest(unittest.TestCase):
    def walk(self, n=800, seed=7, drift=0.0):
        rng = np.random.default_rng(seed)
        c = 100 * np.exp(np.cumsum(rng.normal(drift, 0.01, n)))
        h, l = c * (1 + rng.uniform(0, 0.01, n)), c * (1 - rng.uniform(0, 0.01, n))
        return c, h, l, rng.uniform(100, 200, n)

    def test_matches_existing_altcoin_engine(self):
        c, h, l, _ = self.walk()
        self.assertAlmostEqual(ind.rsi(c)[-1], altcoin.rsi(list(c))[-1], places=6)
        self.assertAlmostEqual(ind.atr(h, l, c)[-1], altcoin.atr(list(h), list(l), list(c))[-1], places=6)
        self.assertAlmostEqual(ind.ema(c, 50)[-1], altcoin.ema(list(c), 50)[-1], places=9)

    def test_basic_properties(self):
        up = np.linspace(1, 2, 300)
        self.assertEqual(ind.rsi(up)[-1], 100.0)
        flat = np.full(300, 5.0)
        self.assertAlmostEqual(ind.ema(flat, 20)[-1], 5.0)
        self.assertAlmostEqual(np.nan_to_num(ind.macd(flat)[2])[-1], 0.0)
        self.assertAlmostEqual(ind.vwap(flat, flat, flat, np.ones(300), np.arange(300) * 3600)[-1], 5.0)
        self.assertAlmostEqual(ind.rvol(np.r_[np.ones(30), 3.0])[-1], 3.0)
        a = np.random.default_rng(1).normal(0, 1, 200)
        self.assertAlmostEqual(ind.rolling_corr(a, a, 50)[-1], 1.0)

    def test_regime_and_trend(self):
        c, h, l, _ = self.walk(drift=0.003)
        r = ind.regime(c, h, l)
        self.assertEqual(r["direction"], "trend naik")
        c2, h2, l2, _ = self.walk(drift=-0.003)
        self.assertEqual(ind.regime(c2, h2, l2)["direction"], "trend turun")

    def test_breakout_and_retest(self):
        c = np.r_[np.full(60, 10.0), 10.6, 10.5, 10.05, 10.4]           # tembus 10, turun menyentuh 10, naik lagi
        h, l = c + 0.05, c - 0.05
        l[-2] = 10.02
        a = np.full(len(c), 0.5)
        b = ind.breakout_retest(h, l, c, a, [{"price": 10.0, "touches": 3, "kind": "R"}])
        self.assertEqual((b["breakout"], b["dir"], b["retest"]), (10.0, "naik", True))

    def test_levels_cluster_repeated_highs(self):
        c = np.r_[np.tile(np.r_[np.linspace(9, 10, 10), np.linspace(10, 9, 10)], 6)]
        lv = ind.levels(c, c, c, atr_now=0.1, k=3)
        self.assertAlmostEqual(lv[0]["price"], 10.0, places=6)
        self.assertGreaterEqual(lv[0]["touches"], 5)


class ResearchTest(unittest.TestCase):
    def bars(self, o, h, l, c):
        n = len(c)
        return {"o": np.array(o, float), "h": np.array(h, float), "l": np.array(l, float), "c": np.array(c, float),
                "t": np.arange(n) * 3600.0, "atr": np.full(n, 1.0)}

    def test_entry_at_next_open_and_gap_through_stop(self):
        d = self.bars(o=[10, 11, 12, 9, 9], h=[10, 12, 12.5, 9.5, 9.5], l=[10, 10.5, 11.5, 8.5, 8.5], c=[10, 11.5, 12, 9, 9])
        tr = research.simulate(d, [0], [10.0], cost=0.0)
        self.assertEqual(tr[0]["t_in"], 3600.0)                 # sinyal di candle 0 -> masuk di buka candle 1
        self.assertAlmostEqual(tr[0]["ret"], 9 / 11 - 1)         # gap di candle 3 melewati stop 10 -> keluar di buka 9
        self.assertEqual(tr[0]["why"], "stop")

    def test_one_position_per_coin(self):
        d = self.bars(o=[10] * 6, h=[10.5] * 6, l=[9.8] * 6, c=[10] * 6)
        sig = np.array([False, False, True, False, False, False])
        tr = research.simulate(d, [0, 1], [5.0, 5.0], cost=0.0, exit_sig=sig)
        self.assertEqual(len(tr), 1)                             # sinyal kedua datang saat posisi pertama masih terbuka

    def test_daily_value_only_after_daily_close(self):
        t4 = np.arange(12) * 14400.0                             # dua hari candle 4 jam
        td = np.array([0.0, 86400.0])
        v = research.align(t4, td, np.array([1.0, 2.0]), 86400, 14400)
        self.assertTrue(np.isnan(v[4]))                          # candle 4j terakhir hari pertama selesai tepat saat hari 1 selesai...
        self.assertEqual(v[5], 1.0)                              # ...nilai hari 1 baru terbaca di candle yang selesai pada/setelah itu
        self.assertEqual(v[10], 1.0)                             # hari ke-2 belum selesai -> masih nilai hari 1
        self.assertEqual(v[11], 2.0)                             # candle 4j terakhir selesai bersamaan dengan hari ke-2

    def test_portfolio_respects_slots(self):
        trades = [{"t_in": 0, "t_out": 10, "ret": 0.1} for _ in range(research.SLOTS + 3)]
        r = research.portfolio(trades, 0, 365 * 86400)
        self.assertAlmostEqual(r["total"], 0.1)                  # hanya SLOTS posisi diambil, masing-masing 1/SLOTS modal


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def sim_db(self):
        db = sqlite3.connect(self.dir / "simulasi.db")
        db.executescript("""CREATE TABLE stats(key TEXT PRIMARY KEY, value REAL);
            CREATE TABLE token_features(mint TEXT, symbol TEXT, created REAL, m60 REAL, s60 REAL, max_mult REAL, dev_sol REAL,
                dev_pct REAL, creator_n24 REAL, name_n1h REAL, tw REAL);
            CREATE TABLE positions(strategy TEXT, closed REAL, cost_usd REAL, pnl_usd REAL);""")
        import time as _t
        now = _t.time()
        db.execute("INSERT INTO stats VALUES ('heartbeat', ?), ('tokens_seen', 12)", (now,))
        db.executemany("INSERT INTO token_features VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
            ("A", "AAA", now - 120, 2.0, 9, 3.0, 1.0, 5, 0, 0, 1), ("B", "BBB", now - 60, 1.2, 4, 1.3, 0.1, 25, 6, 3, 0)])
        db.executemany("INSERT INTO positions VALUES (?,?,?,?)", [("s", now - 500 + i, 1.0, -1.0) for i in range(12)])
        db.commit()
        db.close()

    def test_failing_agent_is_contained(self):
        class Broken(Agent):
            name = "broken"
            def act(self, state):
                raise ValueError("data rusak")
        r = Broken(self.dir).run({})
        self.assertFalse(r.ok)
        self.assertIn("ValueError: data rusak", r.errors[0])

    def test_scanner_and_safety_structured_output(self):
        self.sim_db()
        m = MemeScannerAgent(self.dir).run({})
        self.assertTrue(m.ok, m.errors)
        self.assertEqual([c["symbol"] for c in m.data["candidates"]], ["AAA", "BBB"])
        s = SafetyAgent(self.dir).run({"meme_scanner": m.to_dict()})
        self.assertTrue(s.ok)
        scores = {x["symbol"]: x["score"] for x in s.data["scored"]}
        self.assertGreater(scores["BBB"], scores["AAA"])          # dev serial + porsi dev besar + nama tiruan
        self.assertTrue(any("belum boleh dipakai" in w for w in s.warnings))

    def test_risk_agent_vetoes_after_daily_loss(self):
        self.sim_db()
        r = RiskAgent(self.dir).run({"proposals": [{"strategy": "s", "key": "X", "cost": 1.0}]})
        self.assertTrue(r.ok, r.errors)
        self.assertIn("s", r.data["breaches"])                  # rugi 12 hari ini > batas harian 5
        self.assertTrue(r.data["decisions"][0]["shadow"] or not r.data["decisions"][0]["allowed"])

    def test_supervisor_records_cycle_even_when_an_agent_fails(self):
        self.sim_db()
        sup = Supervisor(self.dir, agents=[MemeScannerAgent(self.dir), SafetyAgent(self.dir), RiskAgent(self.dir)],
                         db_path=self.dir / "agents.db")
        state, summary = sup.cycle()
        self.assertEqual(summary["data"]["agents_ok"], 3)
        runs, ts = latest_cycle(self.dir / "agents.db")
        self.assertEqual(set(runs), {"meme_scanner", "safety", "risk", "supervisor"})
        (self.dir / "simulasi.db").unlink()                     # data hilang: agent gagal, siklus tetap tercatat
        state, summary = sup.cycle()
        self.assertFalse(summary["ok"])
        self.assertTrue(summary["errors"])

    def test_more_trials_raise_the_bar(self):
        self.assertGreater(research.t_needed(100), research.t_needed(28))


if __name__ == "__main__":
    unittest.main()
