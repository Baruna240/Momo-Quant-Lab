"""Research Engine (Fase 5): uji hipotesis strategi dengan validasi bertahap, tanpa memilih berdasarkan win rate.

Jalankan: python research.py              (uji semua hipotesis altcoin H5-H8, simpan ke research.db, cetak laporan)
          python research.py --report     (laporan hasil terakhir)

Hipotesis altcoin (data alt_history.db, 35 koin sejak 2022):
  H5 Breakout + Retest (4 jam)        H6 Trend following multi-timeframe (4 jam + harian)
  H7 VWAP + Relative Volume (1 jam)   H8 Adaptif terhadap regime pasar BTC (4 jam)
Hipotesis meme (H1-H4) diuji di modul masing-masing; statusnya ikut dilaporkan di sini.

Aturan main (semua tanpa melihat masa depan):
- sinyal dihitung saat candle i SELESAI, masuk di harga BUKA candle i+1; stop dicek dengan low/high setiap candle
  (gap melewati stop = keluar di harga buka); satu posisi per koin
- biaya per sisi: fee 0,1% + slippage 0,05% (10 koin terbesar) / 0,15% (lainnya)
- validasi: in-sample 2022-2024 (pilih parameter) -> out-of-sample 2025-sekarang (tidak pernah dipakai memilih)
  -> walk-forward (latih 12 bulan, uji 3 bulan, bergeser) -> stress test (biaya 2x, masuk telat 1 candle,
  buang 5% transaksi terbaik, Monte Carlo drawdown) -> signifikansi dengan syarat t-stat yang naik sesuai jumlah
  percobaan -> harus mengalahkan HODL BTC di out-of-sample
- portofolio: maksimal 5 posisi bersamaan, setiap posisi 1/5 ekuitas saat masuk (modal berbunga)
Catatan jujur: koin di semesta = koin besar HARI INI (yang bertahan) -> ada bias penyintas; hasil cenderung optimis.
"""
import argparse
import json
import math
import sqlite3
import statistics as stx
import sys
import time
from datetime import datetime, timezone
from statistics import NormalDist

import numpy as np

import indicators as ind
from alt_data import connect as hist_connect, load
from config import BASE_DIR

RESEARCH_DB = BASE_DIR / "research.db"
FEE = 0.001
SLIP_BIG, SLIP_SMALL = 0.0005, 0.0015
IS_END = datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp()
WF_TRAIN, WF_TEST = 365 * 86400, 91 * 86400
SLOTS = 5
MIN_IS_TRADES = 100
MIN_WF_TRADES = 30
TF_SEC = {"1h": 3600, "4h": 14400, "1d": 86400}


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ---------------------------------------------------------------- simulasi transaksi
def simulate(d, entry, stop_px, cost, exit_sig=None, trail=None, max_bars=None, delay=0, tag=""):
    """entry: indeks sinyal (candle i selesai). stop_px: harga stop awal per sinyal. trail: kelipatan ATR untuk trailing
    stop dari close tertinggi. Hasil: daftar transaksi {t_in, t_out, ret, bars, why}."""
    o, h, l, c, t, a = d["o"], d["h"], d["l"], d["c"], d["t"], d["atr"]
    n, trades, busy_until = len(c), [], -1
    for k, i in enumerate(entry):
        e = i + 1 + delay
        if e >= n or i <= busy_until:
            continue
        px_in = o[e]
        stop = stop_px[k]
        if not stop < px_in:                     # gap sudah melewati stop: pakai stop darurat 1 ATR
            stop = px_in - (a[i] if a[i] == a[i] else px_in * 0.02)
        peak, x, px_out, why = px_in, None, None, "akhir data"
        for j in range(e, n):
            if l[j] <= stop:
                x, px_out, why = j, min(o[j], stop) if j > e else stop, "stop"
                break
            peak = max(peak, c[j])
            if trail and a[j] == a[j]:
                stop = max(stop, peak - trail * a[j])
            if exit_sig is not None and exit_sig[j] and j + 1 < n:
                x, px_out, why = j + 1, o[j + 1], "sinyal keluar"
                break
            if max_bars and j - e + 1 >= max_bars and j + 1 < n:
                x, px_out, why = j + 1, o[j + 1], "batas waktu"
                break
        if x is None:
            x, px_out = n - 1, c[-1]
        ret = px_out * (1 - cost) / (px_in * (1 + cost)) - 1
        trades.append({"t_in": float(t[e]), "t_out": float(t[x]), "ret": float(ret), "bars": int(x - e + 1), "why": why, "tag": tag})
        busy_until = x - 1        # sinyal berikutnya baru boleh masuk di candle sesudah posisi ini keluar
    return trades


def cross_up(a, b):
    return np.r_[False, (a[1:] > b[1:]) & (a[:-1] <= b[:-1])]


def cross_down(a, b):
    return np.r_[False, (a[1:] < b[1:]) & (a[:-1] >= b[:-1])]


def align(t_fast, t_slow, values, slow_sec, fast_sec):
    """Nilai timeframe besar yang SUDAH SELESAI pada saat candle timeframe kecil selesai."""
    done = t_slow + slow_sec                    # waktu candle besar selesai
    idx = np.searchsorted(done, t_fast + fast_sec, side="right") - 1
    out = np.full(len(t_fast), np.nan)
    ok = idx >= 0
    out[ok] = values[idx[ok]]
    return out


def prep(d):
    d = dict(d)
    d["atr"] = ind.atr(d["h"], d["l"], d["c"])
    return d


# ---------------------------------------------------------------- hipotesis altcoin
def h5_breakout_retest(d, ctx, p):
    c, h, l, a = d["c"], d["h"], d["l"], d["atr"]
    N = p["N"]
    n = len(c)
    res = np.full(n, np.nan)
    if n > N:
        res[N:] = np.lib.stride_tricks.sliding_window_view(h, N)[:-1].max(axis=1)
    brk = np.where((c > res + 0.2 * a) & ~np.isnan(a))[0]
    entries, stops, last = [], [], -1
    for i in brk:
        if i <= last:
            continue
        lv = res[i]
        for j in range(i + 1, min(i + 7, n)):          # tunggu retest maksimal 6 candle
            if l[j] <= lv + 0.3 * a[j] and c[j] > lv:
                entries.append(j)
                stops.append(lv - p["k"] * a[j])
                last = j
                break
    return simulate(d, entries, stops, ctx["cost"], trail=p["m"], max_bars=60, delay=ctx.get("delay", 0))


def h6_mtf_trend(d, ctx, p):
    c, a = d["c"], d["atr"]
    f, s = p["ma"]
    ef, es = ind.ema(c, f), ind.ema(c, s)
    ad = ind.adx(d["h"], d["l"], c)[0]
    sig = cross_up(ef, es) & (ctx["daily_up"] == 1) & ((ad > p["adx"]) if p["adx"] else True)
    idx = np.where(sig & ~np.isnan(a))[0]
    return simulate(d, idx, c[idx] - 2 * a[idx], ctx["cost"], exit_sig=c < es, trail=p["m"], delay=ctx.get("delay", 0))


def h7_vwap_rvol(d, ctx, p):
    c, a = d["c"], d["atr"]
    vw = ctx["vwap"]
    sig = cross_up(c, vw) & (ctx["rvol"] >= p["r"])
    if p["ema"]:
        sig &= c > ctx["ema200"]
    idx = np.where(sig & ~np.isnan(a))[0]
    return simulate(d, idx, c[idx] - 1.5 * a[idx], ctx["cost"], exit_sig=c < vw, max_bars=p["H"], delay=ctx.get("delay", 0))


def h8_regime(d, ctx, p):
    c, a = d["c"], d["atr"]
    f, s = p["ma"]
    ef, es = ind.ema(c, f), ind.ema(c, s)
    r = ind.rsi(c)
    reg = ctx["btc_regime"]                     # 1 = bull, 0 = sideways, -1 = bear (BTC harian, sudah selesai)
    trend_idx = np.where(cross_up(ef, es) & (reg == 1) & ~np.isnan(a))[0]
    mr_idx = np.where(cross_down(r, np.full(len(r), p["rsi"])) & (reg == 0) & ~np.isnan(a))[0]
    t1 = simulate(d, trend_idx, c[trend_idx] - 2 * a[trend_idx], ctx["cost"], exit_sig=c < es, trail=3, delay=ctx.get("delay", 0), tag="trend")
    t2 = simulate(d, mr_idx, c[mr_idx] - 2 * a[mr_idx], ctx["cost"], exit_sig=r > 55, max_bars=12, delay=ctx.get("delay", 0), tag="reversi")
    out, busy = [], []
    for tr in sorted(t1 + t2, key=lambda x: x["t_in"]):   # satu posisi per koin: buang yang tumpang tindih
        if all(tr["t_in"] >= b for b in busy[-1:]):
            out.append(tr)
            busy.append(tr["t_out"])
    return out


def grid(**axes):
    keys = list(axes)
    combos = [{}]
    for k in keys:
        combos = [{**c, k: v} for c in combos for v in axes[k]]
    return combos


HYPOTHESES = {
    "H5": {"name": "Breakout + Retest", "tf": "4h", "fn": h5_breakout_retest, "grid": grid(N=[20, 50], k=[1.0, 1.5], m=[2, 3])},
    "H6": {"name": "Trend following multi-timeframe", "tf": "4h", "fn": h6_mtf_trend,
           "grid": grid(ma=[(20, 50), (50, 200)], adx=[0, 20], m=[3, 4])},
    "H7": {"name": "VWAP + Relative Volume", "tf": "1h", "fn": h7_vwap_rvol, "grid": grid(r=[1.5, 2.5], ema=[False, True], H=[12, 24])},
    "H8": {"name": "Adaptif regime BTC", "tf": "4h", "fn": h8_regime, "grid": grid(ma=[(20, 50), (50, 200)], rsi=[25, 30])},
}


# ---------------------------------------------------------------- metrik
def tstat(rets):
    if len(rets) < 3:
        return None
    sd = stx.stdev(rets)
    return stx.mean(rets) / (sd / math.sqrt(len(rets))) if sd else None


def pf(rets):
    gw, gl = sum(r for r in rets if r > 0), -sum(r for r in rets if r < 0)
    return (gw / gl) if gl else (math.inf if gw else None)


def portfolio(trades, t0, t1):
    """Ekuitas portofolio: maks SLOTS posisi bersamaan, tiap posisi 1/SLOTS ekuitas saat masuk. Hasil: return tahunan, max DD."""
    ev = sorted([(tr["t_in"], 1, k) for k, tr in enumerate(trades)] + [(tr["t_out"], 0, k) for k, tr in enumerate(trades)])
    eq, open_, curve = 1.0, {}, [1.0]
    for _, kind, k in ev:
        if kind == 1:
            if len(open_) < SLOTS:
                open_[k] = eq / SLOTS
        elif k in open_:
            eq += open_.pop(k) * trades[k]["ret"]
            curve.append(eq)
    years = max((t1 - t0) / (365 * 86400), 1e-9)
    peak, dd = 1.0, 0.0
    for v in curve:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    return {"total": eq - 1, "annual": (eq ** (1 / years) - 1) if eq > 0 else -1.0, "max_dd": dd}


def summarize(trades, t0, t1):
    rets = [tr["ret"] for tr in trades]
    if not rets:
        return {"n": 0}
    return {"n": len(rets), "win": sum(r > 0 for r in rets) / len(rets), "exp": stx.mean(rets), "pf": pf(rets),
            "t": tstat(rets), **{"port_" + k: v for k, v in portfolio(trades, t0, t1).items()}}


def hodl(data, t0, t1, bases):
    rets = []
    for b in bases:
        d = data.get(b)
        if d is None:
            continue
        i0, i1 = np.searchsorted(d["t"], t0), np.searchsorted(d["t"], t1) - 1
        if i0 < len(d["c"]) and i1 > i0:
            rets.append(d["c"][i1] / d["o"][i0] - 1)
    years = (t1 - t0) / (365 * 86400)
    tot = stx.mean(rets) if rets else None
    return None if tot is None else {"total": tot, "annual": (1 + tot) ** (1 / years) - 1 if tot > -1 else -1.0}


def within(trades, t0, t1):
    return [tr for tr in trades if t0 <= tr["t_in"] < t1]


# ---------------------------------------------------------------- pipeline
def build_context(hdb):
    alt = BASE_DIR / "altcoin.db"
    big = set()
    if alt.exists():
        c = sqlite3.connect(f"file:{alt}?mode=ro", uri=True)
        big = {r[0] for r in c.execute("SELECT base FROM alt_market WHERE rank <= 10")}
        c.close()
    bases = [r[0].split("/")[0] for r in hdb.execute("SELECT DISTINCT symbol FROM ohlcv WHERE tf='4h'")]
    data = {tf: {} for tf in TF_SEC}
    for b in bases:
        for tf in TF_SEC:
            x = load(b, tf, hdb)
            if x is not None and len(x["c"]) > 300:
                data[tf][b] = prep(x)
    btc = data["1d"]["BTC"]
    e50, e200 = ind.ema(btc["c"], 50), ind.ema(btc["c"], 200)
    btc_reg = np.where((btc["c"] > e200) & (e50 > e200), 1, np.where((btc["c"] < e200) & (e50 < e200), -1, 0)).astype(float)
    btc_reg[np.isnan(e200)] = np.nan
    return {"bases": bases, "big": big, "data": data, "btc_reg": btc_reg, "btc_t": btc["t"]}


def coin_ctx(env, b, tf, cost_mult=1.0, delay=0):
    d = env["data"][tf][b]
    cost = (FEE + (SLIP_BIG if b in env["big"] else SLIP_SMALL)) * cost_mult
    ctx = {"cost": cost, "delay": delay}
    if tf == "4h":
        dd = env["data"]["1d"].get(b)
        if dd is not None:
            e50, e200 = ind.ema(dd["c"], 50), ind.ema(dd["c"], 200)
            up = ((dd["c"] > e50) & (e50 > e200)).astype(float)
            up[np.isnan(e200)] = np.nan
            ctx["daily_up"] = align(d["t"], dd["t"], up, 86400, TF_SEC[tf])
        else:
            ctx["daily_up"] = np.full(len(d["c"]), np.nan)
        ctx["btc_regime"] = align(d["t"], env["btc_t"], env["btc_reg"], 86400, TF_SEC[tf])
    if tf == "1h":
        ctx["vwap"] = ind.vwap(d["h"], d["l"], d["c"], d["v"], d["t"])
        ctx["rvol"] = np.nan_to_num(ind.rvol(d["v"]))
        ctx["ema200"] = ind.ema(d["c"], 200)
    return d, ctx


def run_hypothesis(env, key, ctx_cache, grid=None, trials=None):
    """grid: kombinasi parameter yang diuji (bawaan: kisi hipotesis); trials: total percobaan untuk syarat signifikansi
    (bawaan: semua kisi bawaan; Challenger memakai buku catatan percobaan sepanjang masa)."""
    H = {**HYPOTHESES[key], "grid": grid or HYPOTHESES[key]["grid"]}
    tf, fn = H["tf"], H["fn"]
    bases = [b for b in env["bases"] if b in env["data"][tf]]
    t_start = min(env["data"][tf][b]["t"][0] for b in bases)
    t_end = max(env["data"][tf][b]["t"][-1] for b in bases)

    def trades_for(p, cost_mult=1.0, delay=0):
        out = []
        for b in bases:
            ck = (b, tf, cost_mult, delay)
            if ck not in ctx_cache:
                ctx_cache[ck] = coin_ctx(env, b, tf, cost_mult, delay)
            d, ctx = ctx_cache[ck]
            out += [{**tr, "coin": b} for tr in fn(d, ctx, p)]
        return out

    all_trades = {i: trades_for(p) for i, p in enumerate(H["grid"])}
    # 1) in-sample: pilih parameter dengan expectancy terbaik (minimal MIN_IS_TRADES transaksi)
    is_res = {i: summarize(within(tr, t_start, IS_END), t_start, IS_END) for i, tr in all_trades.items()}
    ok = [i for i, r in is_res.items() if r["n"] >= MIN_IS_TRADES]
    best = max(ok, key=lambda i: is_res[i]["exp"]) if ok else max(is_res, key=lambda i: is_res[i]["n"])
    chosen = all_trades[best]
    oos = summarize(within(chosen, IS_END, t_end), IS_END, t_end)
    full = summarize(chosen, t_start, t_end)
    # 2) walk-forward: tiap jendela memilih ulang parameter dari 12 bulan sebelumnya, diuji 3 bulan berikutnya
    folds, stitched = [], []
    t = t_start + WF_TRAIN
    while t + 0.25 * WF_TEST < t_end:
        tr_res = {i: summarize(within(tr, t - WF_TRAIN, t), t - WF_TRAIN, t) for i, tr in all_trades.items()}
        cand = [i for i, r in tr_res.items() if r["n"] >= MIN_WF_TRADES]
        if cand:
            pick = max(cand, key=lambda i: tr_res[i]["exp"])
            test = within(all_trades[pick], t, min(t + WF_TEST, t_end))
            stitched += test
            rets = [x["ret"] for x in test]
            folds.append({"start": t, "params": pick, "n": len(rets), "exp": stx.mean(rets) if rets else None})
        t += WF_TEST
    wf = summarize(stitched, t_start + WF_TRAIN, t_end)
    wf_pos = sum(1 for f in folds if f["exp"] is not None and f["exp"] > 0)
    # 3) stress test pada parameter terpilih (periode out-of-sample)
    p = H["grid"][best]
    cost2 = summarize(within(trades_for(p, cost_mult=2.0), IS_END, t_end), IS_END, t_end)
    late = summarize(within(trades_for(p, delay=1), IS_END, t_end), IS_END, t_end)
    oos_tr = within(chosen, IS_END, t_end)
    rets = sorted(x["ret"] for x in oos_tr)
    cut = rets[:max(1, int(len(rets) * 0.95))] if rets else []
    no_top = {"n": len(cut), "pf": pf(cut), "exp": stx.mean(cut) if cut else None}
    rng = np.random.default_rng(42)
    mc_dd = []
    for _ in range(500):                              # urutan transaksi diacak: seberapa dalam drawdown bisa terjadi?
        eq = peak = 1.0
        worst = 0.0
        for r in rng.permutation(rets):
            eq *= 1 + r / SLOTS
            peak = max(peak, eq)
            worst = min(worst, eq / peak - 1)
        mc_dd.append(worst)
    mc95 = float(np.percentile(mc_dd, 5)) if mc_dd else None
    # 4) signifikansi: syarat t-stat naik sesuai jumlah percobaan (koreksi Bonferroni, satu sisi, alfa 5%)
    trials = trials or sum(len(h["grid"]) for h in HYPOTHESES.values())
    t_need = t_needed(trials)
    btc_oos = hodl(env["data"][tf], IS_END, t_end, ["BTC"])
    stages = [
        ("Backtest penuh untung", full.get("pf") is not None and full["pf"] > 1 and full["n"] >= MIN_IS_TRADES),
        ("In-sample untung", is_res[best]["n"] >= MIN_IS_TRADES and (is_res[best]["pf"] or 0) > 1),
        ("Out-of-sample untung", oos.get("n", 0) >= 30 and (oos.get("pf") or 0) > 1),
        ("Walk-forward stabil", folds and wf_pos / len(folds) >= 0.6 and (wf.get("pf") or 0) > 1),
        ("Lolos stress test", all((x.get("pf") or 0) > 1 for x in (cost2, late, no_top))),
        (f"Signifikan (t ≥ {t_need:.2f})", (oos.get("t") or 0) >= t_need),
        ("Mengalahkan HODL BTC (OOS)", btc_oos is not None and oos.get("port_annual") is not None and oos["port_annual"] > btc_oos["annual"]),
    ]
    passed = 0
    for _, okk in stages:
        if not okk:
            break
        passed += 1
    return {"key": key, "name": H["name"], "tf": tf, "coins": len(bases), "trials": len(H["grid"]), "params": p,
            "is": is_res[best], "is_all": [{"params": H["grid"][i], **r} for i, r in is_res.items()], "oos": oos, "full": full,
            "wf": wf, "wf_folds": len(folds), "wf_pos": wf_pos, "stress": {"cost2": cost2, "late": late, "no_top": no_top, "mc_dd95": mc95},
            "t_need": t_need, "btc_oos": btc_oos, "eqw_oos": hodl(env["data"][tf], IS_END, t_end, bases),
            "stages": [[name, bool(okk)] for name, okk in stages], "passed": passed,
            "verdict": "LOLOS SEMUA" if passed == len(stages) else f"gagal di: {stages[passed][0]}",
            "periods": {"start": t_start, "is_end": IS_END, "end": t_end}}


def meme_status():
    """Status hipotesis meme dari modul masing-masing (tanpa angka basi yang ditulis tangan)."""
    out = []
    lab = BASE_DIR / "wallet_lab.db"
    leaders = 0
    if lab.exists():
        c = sqlite3.connect(f"file:{lab}?mode=ro", uri=True)
        try:
            leaders = c.execute("SELECT COUNT(*) FROM leaders").fetchone()[0]
        except sqlite3.OperationalError:
            pass
        c.close()
    labeled = scored = 0
    fdb = BASE_DIR / "flow.db"
    if fdb.exists():
        c = sqlite3.connect(f"file:{fdb}?mode=ro", uri=True)
        labeled = c.execute("SELECT COUNT(*) FROM token_flow WHERE finalized IS NOT NULL").fetchone()[0]
        scored = c.execute("SELECT COUNT(*) FROM token_flow WHERE finalized IS NOT NULL AND safety60 IS NOT NULL").fetchone()[0]
        c.close()
    out.append({"key": "H1", "name": "Smart money wallet tracking", "status": "forward test berjalan",
                "detail": f"{leaders} leader dikunci; uji ulang: python wallet_lab.py forward"})
    out.append({"key": "H2", "name": "Volume explosion + momentum", "status": "menunggu data aliran",
                "detail": f"{labeled} token berlabel di flow.db; butuh ≥ 1.000 untuk diuji"})
    out.append({"key": "H3", "name": "New token early detection", "status": "ditolak",
                "detail": "butuh kecepatan blok-0; uji pola sniper (python wallet_lab.py backtest) di bawah 1x di semua kombinasi"})
    out.append({"key": "H4", "name": "Anti-rug + liquidity protection", "status": "menunggu data aliran",
                "detail": f"{scored} token punya skor Safety + label; dinilai setelah ≥ 300"})
    return out


def t_needed(trials):
    """Syarat t-stat satu sisi (alfa 5%) yang dinaikkan sesuai jumlah percobaan (koreksi Bonferroni)."""
    return NormalDist().inv_cdf(1 - 0.05 / max(1, trials))


# ---------------------------------------------------------------- Champion vs Challenger (dipakai agent Strategy Research)
# Ruang parameter yang lebih luas dari kisi bawaan. Challenger dipilih HANYA dari hasil in-sample (2022-2024),
# lalu diuji di semua tahap. Setiap konfigurasi yang pernah diuji dicatat di buku percobaan, dan syarat signifikansi
# dihitung dari TOTAL percobaan sepanjang masa: makin sering mencoba, makin sulit lolos (anti "memancing" hasil).
SPACE = {
    "H5": grid(N=[20, 30, 50, 100], k=[1.0, 1.5, 2.0], m=[2, 3, 4]),
    "H6": grid(ma=[(20, 50), (30, 100), (50, 200)], adx=[0, 20, 25], m=[2, 3, 4]),
    "H7": grid(r=[1.5, 2.0, 2.5, 3.0], ema=[False, True], H=[6, 12, 24]),
    "H8": grid(ma=[(10, 30), (20, 50), (30, 100), (50, 200)], rsi=[20, 25, 30, 35]),
}


def ledger(db=None):
    own = db is None
    db = db or sqlite3.connect(RESEARCH_DB)
    db.execute("""CREATE TABLE IF NOT EXISTS trials (ts REAL, key TEXT, params TEXT, is_exp REAL, oos_exp REAL, passed INTEGER,
                  stages INTEGER, PRIMARY KEY (key, params))""")
    db.commit()
    if own:
        return db
    return db


def total_trials(db):
    """Kisi bawaan (selalu dihitung) + semua konfigurasi tambahan yang pernah diuji Challenger."""
    base = {(k, json.dumps(p, sort_keys=True)) for k, h in HYPOTHESES.items() for p in h["grid"]}
    extra = {(r[0], r[1]) for r in db.execute("SELECT key, params FROM trials")} - base
    return len(base) + len(extra)


def challenge(env, key, champion, n=3, ctx_cache=None):
    """Uji n challenger untuk hipotesis `key`. Hasil: {champion, challengers: [...], promoted}."""
    db = ledger()
    tested = {r[0] for r in db.execute("SELECT params FROM trials WHERE key=?", (key,))}
    tested |= {json.dumps(p, sort_keys=True) for p in HYPOTHESES[key]["grid"]}
    fresh = [p for p in SPACE[key] if json.dumps(p, sort_keys=True) not in tested]
    if not fresh:
        db.close()
        return {"champion": champion, "challengers": [], "promoted": False, "note": "ruang parameter sudah habis diuji"}
    cache = ctx_cache if ctx_cache is not None else {}
    # pilih kandidat dari IN-SAMPLE saja (data 2025+ tidak dipakai memilih)
    scout = []
    for p in fresh:
        r = run_hypothesis(env, key, cache, grid=[p], trials=1)
        if r["is"].get("n", 0) >= MIN_IS_TRADES:
            scout.append((r["is"]["exp"], p))
    picks = [p for _, p in sorted(scout, key=lambda x: -x[0])[:n]]
    for p in picks:      # catat dulu: percobaan dihitung walaupun hasilnya jelek
        db.execute("INSERT OR IGNORE INTO trials VALUES (?,?,?,?,?,?,?)", (time.time(), key, json.dumps(p, sort_keys=True),
                   None, None, None, None))
    db.commit()
    trials = total_trials(db)
    results = []
    for p in picks:
        r = run_hypothesis(env, key, cache, grid=[p], trials=trials)
        db.execute("UPDATE trials SET is_exp=?, oos_exp=?, passed=?, stages=? WHERE key=? AND params=?",
                   (r["is"].get("exp"), r["oos"].get("exp"), int(r["passed"] == len(r["stages"])), r["passed"], key,
                    json.dumps(p, sort_keys=True)))
        results.append({"params": p, "passed": r["passed"], "stages": len(r["stages"]), "verdict": r["verdict"],
                        "is_exp": r["is"].get("exp"), "oos_exp": r["oos"].get("exp"), "oos_pf": r["oos"].get("pf"), "t": r["oos"].get("t")})
    db.commit()
    db.close()
    best = max(results, key=lambda x: (x["passed"], x["is_exp"] or -1), default=None)
    promoted = bool(best and best["passed"] == best["stages"] and best["passed"] > champion.get("passed", 0))
    return {"champion": best if promoted else champion, "challengers": results, "promoted": promoted,
            "trials_total": trials, "t_need": t_needed(trials)}


def save(results):
    db = sqlite3.connect(RESEARCH_DB)
    db.execute("CREATE TABLE IF NOT EXISTS runs (ts REAL, key TEXT, result TEXT)")
    ts = time.time()
    db.executemany("INSERT INTO runs VALUES (?,?,?)", [(ts, r["key"], json.dumps(r, default=float)) for r in results])
    db.commit()
    db.close()


def latest(path=RESEARCH_DB):
    if not path.exists():
        return [], None
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    ts = db.execute("SELECT MAX(ts) FROM runs").fetchone()[0]
    rows = [json.loads(r[0]) for r in db.execute("SELECT result FROM runs WHERE ts = ? ORDER BY key", (ts,))] if ts else []
    db.close()
    return rows, ts


def fmt(v, spec=".2f", none="–"):
    if v is None:
        return none
    if isinstance(v, float) and math.isinf(v):
        return "∞"
    return format(v, spec)


def report(rows):
    if not rows:
        print("Belum ada hasil. Jalankan: python research.py")
        return
    for r in rows:
        oos, is_, wf, s = r["oos"], r["is"], r["wf"], r["stress"]
        print(f"\n=== {r['key']} {r['name']} ({r['tf']}, {r['coins']} koin, {r['trials']} kombinasi) · parameter in-sample: {r['params']}")
        print(f"  in-sample 2022-2024 : n={is_['n']:<5} PF {fmt(is_.get('pf'))}  exp {fmt(is_.get('exp') and 100 * is_['exp'], '+.2f')}%  win {fmt(is_.get('win') and 100 * is_['win'], '.0f')}%")
        print(f"  out-of-sample 2025+ : n={oos.get('n', 0):<5} PF {fmt(oos.get('pf'))}  exp {fmt(oos.get('exp') and 100 * oos['exp'], '+.2f')}%  t {fmt(oos.get('t'))}"
              f"  portofolio {fmt(oos.get('port_annual') and 100 * oos['port_annual'], '+.1f')}%/thn  DD {fmt(oos.get('port_max_dd') and 100 * oos['port_max_dd'], '.1f')}%")
        print(f"  walk-forward        : n={wf.get('n', 0):<5} PF {fmt(wf.get('pf'))}  {r['wf_pos']}/{r['wf_folds']} jendela positif")
        print(f"  stress (OOS)        : biaya 2x PF {fmt(s['cost2'].get('pf'))} · telat 1 candle PF {fmt(s['late'].get('pf'))} · tanpa 5% terbaik PF {fmt(s['no_top'].get('pf'))}"
              f" · Monte Carlo DD 95% {fmt(s['mc_dd95'] and 100 * s['mc_dd95'], '.1f')}%")
        b, e = r["btc_oos"] or {}, r["eqw_oos"] or {}
        print(f"  pembanding OOS      : HODL BTC {fmt(b.get('annual') and 100 * b['annual'], '+.1f')}%/thn · HODL semua koin {fmt(e.get('annual') and 100 * e['annual'], '+.1f')}%/thn")
        print("  tahap: " + " → ".join(("✓ " if ok else "✗ ") + n for n, ok in r["stages"][:r["passed"] + 1]) + f"   [{r['verdict']}]")
    print("\nHipotesis meme:")
    for m in meme_status():
        print(f"  {m['key']} {m['name']:<32} {m['status']:<22} {m['detail']}")


def main():
    ap = argparse.ArgumentParser(description="Research Engine: uji hipotesis strategi altcoin dengan validasi bertahap")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--only", help="mis. H6")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.report:
        report(latest()[0])
        return
    hdb = hist_connect()
    log("memuat data & indikator")
    env = build_context(hdb)
    cache, results = {}, []
    for key in HYPOTHESES:
        if args.only and key != args.only.upper():
            continue
        t0 = time.time()
        results.append(run_hypothesis(env, key, cache))
        log(f"{key} selesai ({time.time() - t0:.0f} dtk)")
    save(results)
    report(results)


if __name__ == "__main__":
    main()
