"""Data altcoin jangka panjang (Fase 4): candle 1 jam / 4 jam / harian sejak 2022 lewat CCXT (data publik, tanpa key).

Jalankan: python alt_data.py                 (unduh / perbarui semua koin yang dipantau simulator altcoin)
          python alt_data.py --symbols SOL,ETH --tf 1d
          python alt_data.py --summary       (cakupan data yang sudah tersimpan)

Sumber utama Binance lewat server data publiknya (data-api.binance.vision: domain utama Binance diblokir dari
jaringan Indonesia); koin yang tidak ada di Binance diambil dari OKX. Pembaruan bertahap: hanya candle baru yang
diunduh. Disimpan di alt_history.db (terpisah dari altcoin.db yang dipakai simulator).
"""
import argparse
import sqlite3
import sys
import time

import numpy as np

from config import BASE_DIR

HIST_DB = BASE_DIR / "alt_history.db"
TFS = {"1h": 3600, "4h": 14400, "1d": 86400}
START = "2022-01-01T00:00:00Z"
DEFAULT = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT", "TRX", "LTC", "NEAR", "SUI", "APT",
           "UNI", "ATOM", "ARB", "OP", "INJ", "TIA", "SEI", "PEPE", "WIF", "ENA"]


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def exchange(name):
    import ccxt
    if name == "binance":
        ex = ccxt.binance({"enableRateLimit": True, "options": {"fetchMarkets": ["spot"], "defaultType": "spot"}})
        ex.urls["api"]["public"] = "https://data-api.binance.vision/api/v3"
    else:
        ex = getattr(ccxt, name)({"enableRateLimit": True})
    ex.load_markets()
    return ex


def connect(path=HIST_DB):
    db = sqlite3.connect(path, timeout=10)
    db.execute("""CREATE TABLE IF NOT EXISTS ohlcv (ex TEXT, symbol TEXT, tf TEXT, ts INTEGER, o REAL, h REAL, l REAL,
                  c REAL, v REAL, PRIMARY KEY (symbol, tf, ts)) WITHOUT ROWID""")
    db.commit()
    return db


def universe():
    """Koin yang dipantau simulator altcoin (top 30 + incaran); kalau belum ada, daftar bawaan."""
    alt = BASE_DIR / "altcoin.db"
    if alt.exists():
        c = sqlite3.connect(f"file:{alt}?mode=ro", uri=True)
        try:
            bases = [r[0] for r in c.execute("SELECT base FROM alt_market ORDER BY rank IS NULL, rank")]
        except sqlite3.OperationalError:
            bases = []
        c.close()
        if bases:
            return list(dict.fromkeys(["BTC", *bases]))
    return DEFAULT


def update(bases, tfs, db=None):
    db = db or connect()
    exs = {}
    total = 0
    for base in bases:
        pair = f"{base}/USDT"
        for name in ("binance", "okx"):
            if name not in exs:
                try:
                    exs[name] = exchange(name)
                except Exception as e:
                    log(f"bursa {name} tidak bisa dipakai: {type(e).__name__}")
                    exs[name] = None
            ex = exs[name]
            if ex and pair in ex.markets:
                break
        else:
            log(f"{pair}: tidak ada di Binance/OKX, dilewati")
            continue
        for tf in tfs:
            step = TFS[tf] * 1000
            last = db.execute("SELECT MAX(ts) FROM ohlcv WHERE symbol=? AND tf=?", (pair, tf)).fetchone()[0]
            since = last + step if last else ex.parse8601(START)
            got = 0
            while since < ex.milliseconds() - step:
                try:
                    bars = ex.fetch_ohlcv(pair, tf, since=since, limit=1000)
                except Exception as e:
                    log(f"{pair} {tf}: gagal ({type(e).__name__}), coba lagi")
                    time.sleep(3)
                    continue
                bars = [b for b in bars if b[0] < ex.milliseconds() - step]   # candle yang belum selesai tidak disimpan
                if not bars:
                    break
                db.executemany("INSERT OR REPLACE INTO ohlcv VALUES (?,?,?,?,?,?,?,?,?)",
                               [(ex.id, pair, tf, *b[:6]) for b in bars])
                db.commit()
                got += len(bars)
                since = bars[-1][0] + step
            total += got
            if got:
                log(f"{pair:<12} {tf:<3} +{got:>6} candle ({ex.id})")
    return total


def load(symbol, tf, db=None, limit=None):
    """Candle tersimpan sebagai array NumPy: t (detik), o, h, l, c, v. symbol boleh 'SOL', 'SOLUSDT' atau 'SOL/USDT'.
    limit: hanya n candle terakhir (jauh lebih cepat untuk analisis harian)."""
    db = db or connect()
    pair = symbol if "/" in symbol else (symbol[:-4] if symbol.endswith("USDT") else symbol) + "/USDT"
    if limit:
        rows = db.execute("SELECT ts, o, h, l, c, v FROM ohlcv WHERE symbol=? AND tf=? ORDER BY ts DESC LIMIT ?",
                          (pair, tf, limit)).fetchall()[::-1]
    else:
        rows = db.execute("SELECT ts, o, h, l, c, v FROM ohlcv WHERE symbol=? AND tf=? ORDER BY ts", (pair, tf)).fetchall()
    if not rows:
        return None
    a = np.array(rows, dtype=float)
    return {"t": a[:, 0] / 1000, "o": a[:, 1], "h": a[:, 2], "l": a[:, 3], "c": a[:, 4], "v": a[:, 5]}


def summary(db=None):
    db = db or connect()
    rows = db.execute("""SELECT symbol, tf, COUNT(*), MIN(ts), MAX(ts), ex FROM ohlcv GROUP BY symbol, tf
                         ORDER BY symbol, tf""").fetchall()
    if not rows:
        print("Belum ada data. Jalankan: python alt_data.py")
        return
    by = {}
    for sym, tf, n, lo, hi, ex in rows:
        by.setdefault(sym, {})[tf] = (n, lo, hi, ex)
    print(f"{'koin':<12}{'1h':>9}{'4h':>8}{'1d':>7}  sejak       sampai      bursa")
    for sym, d in by.items():
        lo = min(v[1] for v in d.values())
        hi = max(v[2] for v in d.values())
        print(f"{sym:<12}" + "".join(f"{d.get(tf, (0,))[0]:>{w}}" for tf, w in (("1h", 9), ("4h", 8), ("1d", 7))) +
              f"  {time.strftime('%Y-%m-%d', time.gmtime(lo / 1000))}  {time.strftime('%Y-%m-%d', time.gmtime(hi / 1000))}  "
              f"{next(iter(d.values()))[3]}")
    n = db.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0]
    print(f"\n{len(by)} koin, {n:,} candle tersimpan")


def main():
    ap = argparse.ArgumentParser(description="Unduh / perbarui candle altcoin jangka panjang lewat CCXT")
    ap.add_argument("--symbols", help="mis. SOL,ETH (kosong = semua koin yang dipantau)")
    ap.add_argument("--tf", default="1h,4h,1d")
    ap.add_argument("--summary", action="store_true")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.summary:
        summary()
        return
    bases = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else universe()
    tfs = [t for t in args.tf.split(",") if t in TFS]
    log(f"memperbarui {len(bases)} koin × {', '.join(tfs)} sejak {START[:10]}")
    n = update(bases, tfs)
    log(f"selesai, {n:,} candle baru")


if __name__ == "__main__":
    main()
