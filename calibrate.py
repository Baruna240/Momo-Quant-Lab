"""Kalibrasi biaya simulator meme dengan harga on-chain sungguhan (hanya MEMBACA data publik lewat Helius).

Jalankan: python calibrate.py --n 120          (120 transaksi beli + 120 jual, dari 36 jam terakhir)
          python calibrate.py --report         (tampilkan hasil kalibrasi terakhir)

Pertanyaan yang dijawab: kalau bot sungguhan memutuskan beli/jual pada saat simulator memutuskan, harga berapa
yang benar-benar didapat 1/2/3/5 detik kemudian (waktu kirim + konfirmasi transaksi)?
- harga keputusan = harga pasar yang dilihat simulator (tanpa slippage model)
- harga nyata     = harga transaksi PERTAMA di koin itu pada detik ke-L setelah keputusan (dari bonding curve)
- slippage nyata  = seberapa jauh harga nyata lebih buruk dari harga keputusan (positif = lebih buruk)
Dampak harga order $1 sendiri di bonding curve ~0,03% sehingga diabaikan.
Hasil dibandingkan dengan slippage yang dipakai model simulator untuk transaksi yang sama.
"""
import argparse
import asyncio
import json
import random
import sqlite3
import statistics as stx
import sys
import time

from config import DB_PATH, load_settings
from wallet_lab import MAX_SIG_PAGES, Prices, Rpc, log, open_lab

LATENCIES = (1, 2, 3, 5)     # detik dari keputusan sampai transaksi masuk blok
WINDOW_H = 36                # transaksi lebih tua dari ini: riwayat on-chain-nya terlalu jauh untuk ditelusuri murah


async def sigs_around(rpc, mint, ts):
    """Tanda tangan transaksi koin di sekitar waktu ts (urut dari terlama). None = koin terlalu ramai."""
    out, before = [], None
    for _ in range(MAX_SIG_PAGES // 2):
        page = await rpc("getSignaturesForAddress", [mint, {"limit": 1000, **({"before": before} if before else {})}])
        out += page
        if len(page) < 1000 or (page and (page[-1].get("blockTime") or 0) < ts - 15):
            return [x for x in reversed(out) if x.get("err") is None and x.get("blockTime")]
        before = page[-1]["signature"]
    return None


async def price_near(prices, sigs, mint, t_from, after=True):
    """Harga transaksi pertama sejak t_from (after) atau terakhir sebelum t_from (not after)."""
    pick = [x for x in sigs if x["blockTime"] >= t_from] if after else [x for x in reversed(sigs) if x["blockTime"] <= t_from]
    for x in pick[:4]:
        pr = await prices.price(x["signature"], mint)
        if pr:
            return pr, x["blockTime"]
    return None, None


async def run(rpc, db, n):
    lab = open_lab()
    prices = Prices(rpc, lab)
    lab.execute("""CREATE TABLE IF NOT EXISTS calibration (run REAL, side TEXT, latency INTEGER, n INTEGER,
                   median REAL, mean REAL, p25 REAL, p75 REAL, model_mean REAL, mark_err REAL)""")
    since = time.time() - WINDOW_H * 3600
    rows = [dict(r) for r in db.execute("""SELECT ts, side, mint, price_sol, slip FROM trades
                                           WHERE ts >= ? AND slip IS NOT NULL AND price_sol > 0""", (since,))]
    buys = [r for r in rows if r["side"] == "BELI"]
    sells = [r for r in rows if r["side"] == "JUAL"]
    sample = random.sample(buys, min(n, len(buys))) + random.sample(sells, min(n, len(sells)))
    log(f"menguji {len(sample)} transaksi ({min(n, len(buys))} beli, {min(n, len(sells))} jual) dari {WINDOW_H} jam terakhir")
    res = {(side, L): [] for side in ("BELI", "JUAL") for L in LATENCIES}
    model = {"BELI": [], "JUAL": []}
    mark_err = {"BELI": [], "JUAL": []}
    sig_cache, skipped = {}, 0
    for i, r in enumerate(sample, 1):
        mint = r["mint"]
        if mint not in sig_cache:
            sig_cache[mint] = await sigs_around(rpc, mint, r["ts"])
        sigs = sig_cache[mint]
        if not sigs:
            skipped += 1
            continue
        buy = r["side"] == "BELI"
        mark = r["price_sol"] / (1 + r["slip"]) if buy else r["price_sol"]   # harga pasar yang dilihat simulator
        p_now, _ = await price_near(prices, sigs, mint, r["ts"], after=False)
        if p_now:
            mark_err[r["side"]].append(mark / p_now - 1)
        got = False
        for L in LATENCIES:
            p, _ = await price_near(prices, sigs, mint, r["ts"] + L)
            if p:
                res[(r["side"], L)].append(p / mark - 1 if buy else 1 - p / mark)
                got = True
        if got:
            model[r["side"]].append(r["slip"])
        if i % 20 == 0:
            lab.commit()
            log(f"  {i}/{len(sample)}")
    lab.commit()
    run_ts = time.time()
    for (side, L), xs in res.items():
        if len(xs) >= 5:
            q = stx.quantiles(xs, n=4)
            me = mark_err[side]
            lab.execute("INSERT INTO calibration VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (run_ts, side, L, len(xs), stx.median(xs), stx.mean(xs), q[0], q[2],
                         stx.mean(model[side]) if model[side] else None, stx.median(me) if me else None))
    lab.commit()
    log(f"{skipped} transaksi dilewati (koin terlalu ramai / tidak ada transaksi di sekitarnya)")
    report(lab)


def report(lab):
    run_ts = lab.execute("SELECT MAX(run) FROM calibration").fetchone()[0] if lab.execute(
        "SELECT 1 FROM sqlite_master WHERE name='calibration'").fetchone() else None
    if not run_ts:
        print("Belum ada hasil kalibrasi. Jalankan: python calibrate.py --n 120")
        return
    rows = lab.execute("SELECT * FROM calibration WHERE run = ? ORDER BY side, latency", (run_ts,)).fetchall()
    print(f"\nKALIBRASI {time.strftime('%Y-%m-%d %H:%M', time.localtime(run_ts))}  (positif = harga lebih buruk dari keputusan)")
    print(f"{'sisi':<6}{'telat':>7}{'n':>5}{'median':>9}{'rata2':>9}{'p25':>8}{'p75':>8}{'model':>9}")
    for r in rows:
        print(f"{r['side']:<6}{r['latency']:>6}s{r['n']:>5}{r['median'] * 100:>8.2f}%{r['mean'] * 100:>8.2f}%"
              f"{r['p25'] * 100:>7.2f}%{r['p75'] * 100:>7.2f}%{r['model_mean'] * 100:>8.2f}%")
    for side in ("BELI", "JUAL"):
        e = next((r["mark_err"] for r in rows if r["side"] == side and r["mark_err"] is not None), None)
        if e is not None:
            print(f"Akurasi harga simulator ({side}): median selisih {e * 100:+.2f}% dari harga on-chain saat itu")


async def main_async(args):
    s = load_settings()
    if not s.helius_api_key:
        sys.exit("HELIUS_API_KEY kosong di .env")
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rpc = Rpc(s.rpc_url)
    try:
        await run(rpc, db, args.n)
        log(f"selesai, {rpc.calls} panggilan RPC")
    finally:
        await rpc.close()


def main():
    ap = argparse.ArgumentParser(description="Kalibrasi slippage simulator meme dengan harga on-chain")
    ap.add_argument("--n", type=int, default=120, help="jumlah transaksi beli DAN jual yang diuji")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.report:
        report(open_lab())
    else:
        asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
