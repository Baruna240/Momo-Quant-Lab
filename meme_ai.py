"""Latihan & uji AI pemilih meme coin dari data belajar lengkap (token_features ver 2).

Jalankan: python meme_ai.py              (masuk di detik ke-90, aturan jual varian "filter")
          python meme_ai.py --entry 60 --variant momentum_antirug

Cara kerja:
1. Untuk setiap token, hasil trading DISIMULASIKAN ULANG dari jejak harganya (kolom path) dengan aturan jual
   varian yang dipilih (stop loss, TP, trailing, koin mati, batas waktu) dan biaya realistis
   (fee per sisi, slippage dari kecepatan gerak harga, biaya jaringan per transaksi).
2. Model gradient boosting belajar dari ciri koin SAAT MASUK (tidak melihat masa depan).
3. Penilaian walk-forward: belajar dari 70% token terlama, diuji di 30% token terbaru.
Tidak mengubah bot yang sedang berjalan. Hasilnya hanya laporan.
"""
import argparse
import json
import sqlite3
import sys

import numpy as np

from config import DB_PATH, load_settings
from strategies import build_variants

SNAPS = (30, 60, 90, 180)
MIN_ROWS = 1500


def entry_features(r, T):
    snaps = [x for x in SNAPS if x <= T]
    f = []
    for x in snaps:
        f += [r[f"m{x}"], r[f"s{x}"], r[f"d{x}"], r[f"h{x}"], r[f"a{x}"]]
    m0, mT = r[f"m{snaps[0]}"] or 1, r[f"m{T}"] or 1
    f += [mT / m0, (r[f"s{T}"] or 0) - (r[f"s{snaps[0]}"] or 0), mT / max(r[f"h{T}"] or mT, 1e-9),
          r["dev_sol"], r["dev_pct"], r["creator_n24"], r["name_n1h"], r["mayhem"], r["name_len"],
          r["tw"], r["tg"], r["web"], r["desc_len"], (r["created"] // 3600) % 24]
    return [np.nan if v is None else float(v) for v in f]


def simulate(path, T, c, s, tx_frac):
    """Hasil (kelipatan modal) kalau koin dibeli di detik T lalu dijual dengan aturan varian c."""
    pts = [p for p in path if p[0] >= T]
    if len(pts) < 2:
        return None
    before = [p for p in path if p[0] <= T - 8]
    ref = before[-1][1] if before else pts[0][1]

    def slip(m, ref_m):
        return min(s.slippage_max, s.slippage_min + s.slippage_k * (abs(m / ref_m - 1) if ref_m else 0))

    a0, m0, _ = pts[0]
    entry = m0 * (1 + slip(m0, ref))
    qty = (1 - s.fee_pct - tx_frac) / entry
    remaining, proceeds, peak, tp1, tp1_age, tp2 = qty, 0.0, m0, False, None, False
    last_m, last_change, hist = m0, a0, [(a0, m0)]

    def sell(q, m, a):
        nonlocal remaining, proceeds
        prev = [x for t, x in hist if t <= a - 8]
        gross = q * m * (1 - slip(m, prev[-1] if prev else m)) * (1 - s.fee_pct)
        proceeds += max(gross - tx_frac, 0.0)
        remaining -= q

    for a, m, _ in pts[1:]:
        hist.append((a, m))
        if m != last_m:
            last_change, last_m = a, m
        peak = max(peak, m)
        mult = m / entry
        held = (a - a0) / 60
        if not tp1:
            if mult <= 1 - c.stop_loss:
                sell(remaining, m, a); break
            if mult >= c.tp1_mult:
                tp1, tp1_age = True, a
                sell(min(qty * c.tp1_sell, remaining), m, a)
            elif held >= c.max_hold_min:
                sell(remaining, m, a); break
        if tp1 and remaining > 1e-12:
            if not tp2 and mult >= c.tp2_mult:
                tp2 = True
                sell(min(qty * c.tp2_sell, remaining), m, a)
            if remaining > 1e-12 and m <= peak * (1 - c.trailing_stop):
                sell(remaining, m, a); break
        if remaining > 1e-12 and a - last_change >= c.stale_min * 60:
            sell(remaining, m, a); break
        if remaining <= 1e-12:
            break
    if remaining > 1e-12:   # data habis (menit ke-20): jual di harga terakhir
        sell(remaining, pts[-1][1], pts[-1][0])
    return proceeds


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--entry", type=int, default=90, choices=SNAPS)
    ap.add_argument("--variant", default="filter")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    s = load_settings()
    c = next((v for v in build_variants(s) if v.name == args.variant), None)
    if c is None:
        sys.exit(f"Varian {args.variant} tidak ditemukan")
    tx_frac = s.tx_cost_sol * s.sol_usd_fallback / s.per_coin_usd   # biaya jaringan sebagai bagian dari modal
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute("SELECT * FROM token_features WHERE ver=2 AND finalized=1 ORDER BY created")]
    T = args.entry
    data = []
    for r in rows:
        if r[f"m{T}"] is None or not r["path"]:
            continue
        ret = simulate(json.loads(r["path"]), T, c, s, tx_frac)
        if ret is not None:
            data.append((entry_features(r, T), ret))
    print(f"Data lengkap: {len(rows)} token, bisa disimulasikan masuk di detik {T}: {len(data)}")
    if len(data) < MIN_ROWS:
        print(f"Belum cukup untuk melatih AI (butuh minimal {MIN_ROWS}). Coba lagi setelah data bertambah.")
        return
    X = np.array([d[0] for d in data]); ret = np.array([d[1] for d in data])
    y = (ret > 1).astype(int)
    print(f"Kalau membeli SEMUA token di detik {T} dengan aturan jual '{c.label}': rata-rata {ret.mean():.3f}x,"
          f" untung {y.mean():.1%}")
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import roc_auc_score
    cut = int(len(y) * 0.7)
    mdl = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_leaf_nodes=8, min_samples_leaf=40,
                                         l2_regularization=1.0, random_state=1).fit(X[:cut], y[:cut])
    p = mdl.predict_proba(X[cut:])[:, 1]
    rt, yt = ret[cut:], y[cut:]
    auc = roc_auc_score(yt, p) if 0 < yt.sum() < len(yt) else float("nan")
    print(f"\nUji di {len(yt)} token terbaru (tidak ikut dipelajari): AUC {auc:.3f} (0,5 = tebak acak)")
    print(f"  beli semua         : rata-rata {rt.mean():.3f}x, untung {yt.mean():.0%}")
    for q in (0.5, 0.8, 0.9, 0.95):
        k = p >= np.quantile(p, q)
        print(f"  top {100 - q * 100:3.0f}% skor AI : n={k.sum():4d} rata-rata {rt[k].mean():.3f}x, untung {yt[k].mean():.0%},"
              f" median {np.median(rt[k]):.2f}x")
    # top 5% terlalu sedikit untuk dipercaya, jadi kesimpulan hanya dari kelompok yang cukup besar
    best = max((rt[p >= np.quantile(p, q)].mean(), q) for q in (0.5, 0.8, 0.9))
    verdict = "ADA PETUNJUK: layak diuji sebagai varian baru" if best[0] > 1.02 else \
        "BELUM UNTUNG: AI belum menemukan pola yang menutup biaya"
    print(f"\nKesimpulan: {verdict} (terbaik {best[0]:.3f}x di top {100 - best[1] * 100:.0f}%)")


if __name__ == "__main__":
    main()
