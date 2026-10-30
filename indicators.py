"""Agent Altcoin Scanner + Technical Analysis (Fase 4): indikator teknikal & analisis multi-timeframe.

Semua fungsi menerima array NumPy dan mengembalikan array sepanjang input (nilai awal yang belum cukup data = NaN),
sehingga bisa dipakai bersama di research engine, backtest, dan dashboard tanpa melihat masa depan:
nilai di indeks i hanya memakai data sampai i.

Jalankan: python indicators.py SOL      (analisis lengkap satu koin dari alt_history.db)
"""
import math
import sys

import numpy as np

BARS_PER_YEAR = {"1h": 24 * 365, "4h": 6 * 365, "1d": 365}
ANALYZE_BARS = {"1h": 2000, "4h": 1500, "1d": 1000}   # analisis memakai potongan terakhir (EMA200 tetap punya pemanasan cukup)
VOL_WINDOW = {"1h": 720, "4h": 180, "1d": 30}            # volatilitas: 30 hari terakhir
CORR_WINDOW = {"1h": 24 * 30, "4h": 6 * 30, "1d": 90}    # korelasi BTC: 30 hari (1h/4h), 90 hari (1d)


# ---------------------------------------------------------------- dasar
def sma(x, n):
    x = np.asarray(x, float)
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def ema(x, n):
    x = np.asarray(x, float)
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    k = 2 / (n + 1)
    out[n - 1] = x[:n].mean()
    for i in range(n, len(x)):
        out[i] = x[i] * k + out[i - 1] * (1 - k)
    return out


def wilder(x, n):
    """Rata-rata Wilder (dipakai RSI, ATR, ADX)."""
    x = np.asarray(x, float)
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    out[n - 1] = x[:n].mean()
    for i in range(n, len(x)):
        out[i] = (out[i - 1] * (n - 1) + x[i]) / n
    return out


# ---------------------------------------------------------------- indikator
def rsi(c, n=14):
    c = np.asarray(c, float)
    d = np.diff(c, prepend=c[0])
    up, dn = wilder(np.clip(d, 0, None)[1:], n), wilder(np.clip(-d, 0, None)[1:], n)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(dn == 0, np.where(up == 0, 50.0, 100.0), 100 - 100 / (1 + up / dn))
    return np.insert(r, 0, np.nan)


def macd(c, fast=12, slow=26, signal=9):
    line = ema(c, fast) - ema(c, slow)
    sig = np.full(len(line), np.nan)
    ok = ~np.isnan(line)
    if ok.sum() >= signal:
        sig[ok] = ema(line[ok], signal)
    return line, sig, line - sig


def true_range(h, l, c):
    h, l, c = (np.asarray(a, float) for a in (h, l, c))
    pc = np.insert(c[:-1], 0, c[0])
    return np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)])


def atr(h, l, c, n=14):
    return wilder(true_range(h, l, c), n)


def adx(h, l, c, n=14):
    """ADX + DI+ / DI-. ADX > 25 = trend kuat, < 20 = tidak ada trend (sideways)."""
    h, l, c = (np.asarray(a, float) for a in (h, l, c))
    up, dn = np.diff(h, prepend=h[0]), -np.diff(l, prepend=l[0])
    plus = np.where((up > dn) & (up > 0), up, 0.0)
    minus = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = wilder(true_range(h, l, c), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = 100 * wilder(plus, n) / tr
        mdi = 100 * wilder(minus, n) / tr
        dx = 100 * np.abs(pdi - mdi) / (pdi + mdi)
    out = np.full(len(c), np.nan)
    ok = ~np.isnan(dx)
    if ok.sum() >= n:
        out[ok] = wilder(np.nan_to_num(dx[ok]), n)
    return out, pdi, mdi


def vwap(h, l, c, v, t, period=86400):
    """VWAP yang direset tiap periode (bawaan: harian UTC). t dalam detik."""
    tp = (np.asarray(h, float) + np.asarray(l, float) + np.asarray(c, float)) / 3
    v = np.asarray(v, float)
    day = (np.asarray(t, float) // period).astype(np.int64)
    out = np.empty(len(tp))
    pv = vv = 0.0
    for i in range(len(tp)):
        if i == 0 or day[i] != day[i - 1]:
            pv = vv = 0.0
        pv += tp[i] * v[i]
        vv += v[i]
        out[i] = pv / vv if vv else tp[i]
    return out


def rvol(v, n=20):
    """Volume relatif: volume candle ini dibanding rata-rata n candle SEBELUMNYA (tanpa candle ini)."""
    v = np.asarray(v, float)
    base = np.insert(sma(v, n)[:-1], 0, np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(base > 0, v / base, np.nan)


def realized_vol(c, tf="1h", n=None):
    """Volatilitas tahunan dari log return n candle terakhir (bawaan: 30 hari)."""
    c = np.asarray(c, float)
    n = n or {"1h": 720, "4h": 180, "1d": 30}[tf]
    r = np.diff(np.log(c), prepend=np.nan)
    out = np.full(len(c), np.nan)
    for i in range(n, len(c)):
        out[i] = np.nanstd(r[i - n + 1:i + 1], ddof=1) * math.sqrt(BARS_PER_YEAR[tf])
    return out


def rolling_corr(a, b, n):
    """Korelasi Pearson bergulir dari return dua koin (array return sejajar)."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    out = np.full(len(a), np.nan)
    for i in range(n - 1, len(a)):
        x, y = a[i - n + 1:i + 1], b[i - n + 1:i + 1]
        ok = ~(np.isnan(x) | np.isnan(y))
        if ok.sum() > 5 and np.std(x[ok]) > 0 and np.std(y[ok]) > 0:
            out[i] = np.corrcoef(x[ok], y[ok])[0, 1]
    return out


# ---------------------------------------------------------------- struktur harga
def pivots(h, l, k=5):
    """Titik balik: high/low yang tertinggi/terendah di k candle kiri & kanannya. Hasil: [(indeks, harga, 'R'/'S')].
    Pivot di indeks i baru 'diketahui' di i + k (dipakai oleh fungsi lain supaya tidak melihat masa depan)."""
    h, l = np.asarray(h, float), np.asarray(l, float)
    out = []
    for i in range(k, len(h) - k):
        if h[i] == h[i - k:i + k + 1].max():
            out.append((i, h[i], "R"))
        if l[i] == l[i - k:i + k + 1].min():
            out.append((i, l[i], "S"))
    return out


def levels(h, l, c, atr_now, k=5, lookback=300, tol_atr=0.6, upto=None):
    """Level support/resistance: pivot dalam `lookback` candle terakhir yang dikelompokkan bila jaraknya < tol x ATR.
    Hasil diurutkan dari yang paling sering disentuh: [{"price", "touches", "kind"}]."""
    n = len(c) if upto is None else upto + 1
    lo = max(0, n - lookback)
    ps = [(i, p, kd) for i, p, kd in pivots(h[lo:n], l[lo:n], k) if i + lo + k < n]
    tol = max(atr_now * tol_atr, 1e-12)
    groups = []
    for _, p, kd in sorted(ps, key=lambda x: x[1]):
        if groups and p - groups[-1]["hi"] <= tol:
            g = groups[-1]
            g["sum"] += p
            g["touches"] += 1
            g["hi"] = p
            g["kinds"].add(kd)
        else:
            groups.append({"sum": p, "touches": 1, "hi": p, "kinds": {kd}})
    out = [{"price": g["sum"] / g["touches"], "touches": g["touches"],
            "kind": "S/R" if len(g["kinds"]) > 1 else next(iter(g["kinds"]))} for g in groups]
    return sorted(out, key=lambda g: -g["touches"])


def breakout_retest(h, l, c, a, lvls, lookback=48, brk_atr=0.2, retest_atr=0.35):
    """Breakout: close menembus level resistance > brk_atr x ATR. Retest: sesudahnya low kembali menyentuh level
    (dalam retest_atr x ATR) tapi close tetap di atas -> level lama berubah jadi support.
    Hasil: {"breakout": harga level atau None, "bars_ago", "retest": bool} untuk breakout terbaru."""
    n = len(c)
    best = {"breakout": None, "bars_ago": None, "retest": False, "dir": None}
    for lv in lvls:
        p = lv["price"]
        for i in range(max(1, n - lookback), n):
            if np.isnan(a[i]):
                continue
            up = c[i - 1] <= p < c[i] and c[i] - p > brk_atr * a[i]
            dn = c[i - 1] >= p > c[i] and p - c[i] > brk_atr * a[i]
            if up or dn:
                ago = n - 1 - i
                if best["bars_ago"] is None or ago < best["bars_ago"]:
                    after = range(i + 1, n)
                    if up:
                        rt = any(l[j] - p <= retest_atr * a[j] and c[j] > p for j in after)
                    else:
                        rt = any(p - h[j] <= retest_atr * a[j] and c[j] < p for j in after)
                    best = {"breakout": p, "bars_ago": ago, "retest": rt, "dir": "naik" if up else "turun"}
    return best


# ---------------------------------------------------------------- regime & multi-timeframe
def trend_label(c, e50, e200, i=-1):
    if np.isnan(e200[i]) or np.isnan(e50[i]):
        return "data kurang"
    if c[i] > e50[i] > e200[i]:
        return "naik"
    if c[i] < e50[i] < e200[i]:
        return "turun"
    return "campuran"


def regime(c, h, l, i=-1, tf="1h"):
    """Kondisi pasar: arah (trend naik / trend turun / sideways) + volatilitas (tinggi / normal / rendah).
    Arah: harga & EMA50 terhadap EMA200 dan kekuatan trend ADX. Volatilitas: ATR% dibanding sejarahnya sendiri."""
    c, h, l = (np.asarray(a, float) for a in (c, h, l))
    e50, e200 = ema(c, 50), ema(c, 200)
    ad, _, _ = adx(h, l, c)
    a = atr(h, l, c)
    atr_pct = a / c
    hist = atr_pct[~np.isnan(atr_pct)][-500:]
    pct = float((hist <= atr_pct[i]).mean()) if len(hist) and not np.isnan(atr_pct[i]) else None
    slope = (e200[i] - e200[i - 20]) / e200[i - 20] if len(c) > 220 and not np.isnan(e200[i - 20]) else None
    t = trend_label(c, e50, e200, i)
    strong = not np.isnan(ad[i]) and ad[i] >= 20
    if t == "naik" and strong:
        direction = "trend naik"
    elif t == "turun" and strong:
        direction = "trend turun"
    else:
        direction = "sideways"
    vol = None if pct is None else "tinggi" if pct >= 0.75 else "rendah" if pct <= 0.25 else "normal"
    return {"direction": direction, "volatility": vol, "adx": None if np.isnan(ad[i]) else float(ad[i]),
            "atr_pct": None if np.isnan(atr_pct[i]) else float(atr_pct[i]), "atr_pctile": pct,
            "ema200_slope": slope, "trend": t}


def analyze(symbol, db=None):
    """Analisis lengkap satu koin untuk agent Altcoin Scanner (keluaran terstruktur). None kalau data belum ada."""
    from alt_data import connect, load
    db = db or connect()
    out = {"symbol": symbol.upper(), "tf": {}}
    btc = {tf: load("BTC", tf, db, ANALYZE_BARS[tf]) for tf in ("1h", "4h", "1d")}
    for tf in ("1h", "4h", "1d"):
        d = load(symbol, tf, db, ANALYZE_BARS[tf])
        if d is None or len(d["c"]) < 60:
            continue
        c, h, l, v, t = d["c"], d["h"], d["l"], d["v"], d["t"]
        e50, e200 = ema(c, 50), ema(c, 200)
        a = atr(h, l, c)
        ml, ms, mh = macd(c)
        ad, pdi, mdi = adx(h, l, c)
        rg = regime(c, h, l, tf=tf)
        lv = levels(h, l, c, a[-1] if not np.isnan(a[-1]) else c[-1] * 0.02)
        px = c[-1]
        sup = max((x["price"] for x in lv if x["price"] < px), default=None)
        res = min((x["price"] for x in lv if x["price"] > px), default=None)
        row = {"close": float(px), "time": float(t[-1]), "bars": len(c), "trend": trend_label(c, e50, e200),
               "ema50": _f(e50[-1]), "ema200": _f(e200[-1]), "rsi": _f(rsi(c)[-1]), "macd_hist": _f(mh[-1]),
               "adx": _f(ad[-1]), "di_plus": _f(pdi[-1]), "di_minus": _f(mdi[-1]), "atr_pct": _f(a[-1] / px),
               "rvol": _f(rvol(v)[-1]), "vol_annual": _f(np.std(np.diff(np.log(c[-VOL_WINDOW[tf] - 1:])), ddof=1)
                                                          * math.sqrt(BARS_PER_YEAR[tf])) if len(c) > 30 else None,
               "regime": rg["direction"], "volatility": rg["volatility"],
               "support": sup, "resistance": res,
               "breakout": breakout_retest(h, l, c, a, lv[:6])}
        if tf != "1d":
            row["vwap_dist"] = _f(px / vwap(h, l, c, v, t)[-1] - 1)
        b = btc.get(tf)
        if b is not None and symbol.upper() not in ("BTC", "BTCUSDT"):
            common, ia, ib = np.intersect1d(t, b["t"], return_indices=True)
            if len(common) > 60:
                ra, rb = np.diff(np.log(c[ia])), np.diff(np.log(b["c"][ib]))
                w = min(CORR_WINDOW[tf], len(ra))
                row["btc_corr"] = _f(np.corrcoef(ra[-w:], rb[-w:])[0, 1])
        out["tf"][tf] = row
    if not out["tf"]:
        return None
    trends = [r["trend"] for r in out["tf"].values()]
    out["mtf_align"] = ("naik" if all(x == "naik" for x in trends) else "turun" if all(x == "turun" for x in trends)
                        else "tidak selaras")
    return out


def _f(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) or (hasattr(x, "dtype") and np.isnan(x)) else float(x)


def _print(a):
    print(f"=== {a['symbol']} · keselarasan multi-timeframe: {a['mtf_align']}")
    for tf, r in a["tf"].items():
        b = r["breakout"]
        print(f"[{tf}] close {r['close']:.6g} · trend {r['trend']} · regime {r['regime']} (volatilitas {r['volatility']})")
        print(f"     EMA50 {r['ema50'] or 0:.6g} / EMA200 {r['ema200'] or 0:.6g} · RSI {r['rsi'] or 0:.1f} · MACD hist {r['macd_hist'] or 0:+.4g}"
              f" · ADX {r['adx'] or 0:.1f} (DI+ {r['di_plus'] or 0:.1f} / DI- {r['di_minus'] or 0:.1f})")
        print(f"     ATR {100 * (r['atr_pct'] or 0):.2f}% · RVOL {r['rvol'] or 0:.2f} · vol tahunan {100 * (r['vol_annual'] or 0):.0f}%"
              + (f" · jarak VWAP {100 * r['vwap_dist']:+.2f}%" if r.get("vwap_dist") is not None else "")
              + (f" · korelasi BTC {r['btc_corr']:.2f}" if r.get("btc_corr") is not None else ""))
        lvl = lambda x: "–" if x is None else f"{x:.6g}"
        print(f"     support {lvl(r['support'])} · resistance {lvl(r['resistance'])}"
              + (f" · breakout {b['dir']} di {b['breakout']:.6g} ({b['bars_ago']} candle lalu, retest {'ya' if b['retest'] else 'belum'})"
                 if b["breakout"] else ""))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    res = analyze(sys.argv[1] if len(sys.argv) > 1 else "BTC")
    print("Data belum ada. Jalankan: python alt_data.py") if res is None else _print(res)
