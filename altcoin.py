"""Simulator altcoin & koin besar: paper trading spot + futures dengan data publik (TANPA uang sungguhan).

Jalankan: python altcoin.py              berjalan terus, cek harga tiap menit
          python altcoin.py --backtest   hanya uji strategi di data historis, lalu keluar

Koin yang dipantau: Top N kapitalisasi pasar (CoinGecko, tanpa stablecoin) + watchlist pribadi (ALT_WATCHLIST).
Setiap strategi punya modal simulasi sendiri (ALT_BUDGET_USD) dan maksimal ALT_MAX_POSITIONS posisi sekaligus.
Database terpisah (altcoin.db), jadi tidak mengganggu simulasi meme coin.

Futures (simulasi perpetual USDT, isolated margin): bisa LONG (untung saat naik) dan SHORT (untung saat turun)
dengan leverage. Harga memakai candle spot Binance sebagai pendekatan harga perpetual; koin dibatasi yang punya
kontrak perpetual di OKX; funding rate diambil dari OKX tiap jam dan dibayar tiap 8 jam; posisi dilikuidasi
(margin habis) kalau harga menyentuh harga likuidasi.
"""
import asyncio
import json
import os
import socket
import sqlite3
import sys
import time
from contextlib import nullcontext
from dataclasses import dataclass, fields
from typing import Callable, Optional

import httpx

from alt_ai import AI, AiModel
from config import BASE_DIR, load_env

ALT_DB = BASE_DIR / "altcoin.db"
BINANCE = "https://data-api.binance.vision/api/v3"
COINGECKO = "https://api.coingecko.com/api/v3/coins/markets"
OKX = "https://www.okx.com/api/v5/public"
# stablecoin & token emas tidak menarik untuk ditradingkan
STABLE = {"USDT", "USDC", "USDS", "USDE", "USD1", "RLUSD", "DAI", "FDUSD", "TUSD", "PYUSD", "USDD", "XAUT", "PAXG",
          "U", "BFUSD", "USDF", "EURC", "EURI", "AEUR", "USDP", "GUSD", "USDG", "USDTB", "SUSDE", "BUIDL"}
TF_SEC = {"1h": 3600, "4h": 14400}
HISTORY = {"1h": 3000, "4h": 1000}   # candle untuk backtest & latihan AI (1j ≈ 125 hari, 4j ≈ 166 hari)
LIVE_BARS = 300         # candle yang diambil saat live (cukup untuk EMA200)
WARMUP = 200
LOCK_PORT = 8052
BACKTEST_EVERY = 24 * 3600
FUND_SEC = 8 * 3600     # funding perpetual dibayar tiap 8 jam (00, 08, 16 UTC)
FUND_DEFAULT = 0.0001   # 0,01% per 8 jam: tarif dasar bila data OKX tidak ada
MMR = 0.005             # maintenance margin 0,5%
AI_SPLIT = 0.7          # backtest AI: 70% data awal untuk belajar, 30% terakhir untuk diuji


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


@dataclass
class AltSettings:
    budget_usd: float = 100.0       # modal simulasi per strategi
    max_positions: int = 5          # posisi bersamaan per strategi (modal dibagi rata)
    top_n: int = 30                 # jumlah koin terbesar yang dipantau
    watchlist: str = "MITO,STRK,DYM,IOST,SC"
    fee_pct: float = 0.001          # fee spot Binance/Tokocrypto per sisi
    fut_fee_pct: float = 0.0005     # fee taker futures per sisi (dari nilai kontrak, bukan margin)
    slip_big: float = 0.0005        # slippage koin 10 besar
    slip_small: float = 0.0015      # slippage koin lain
    poll_sec: float = 60.0
    notify: bool = True             # kirim sinyal beli/jual ke Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


def load_alt_settings():
    load_env()
    s = AltSettings()
    for f in fields(s):
        key = f.name.upper() if f.name.startswith("telegram") else "ALT_" + f.name.upper()
        raw = os.environ.get(key)
        if raw is None or raw == "":
            continue
        kind = type(getattr(s, f.name))
        setattr(s, f.name, raw.lower() in ("1", "true", "yes", "ya") if kind is bool else kind(raw))
    return s


# ================= indikator
def ema(xs, n):
    out, e, k = [None] * len(xs), None, 2 / (n + 1)
    for i, x in enumerate(xs):
        if i < n - 1:
            continue
        e = sum(xs[i - n + 1:i + 1]) / n if e is None else x * k + e * (1 - k)
        out[i] = e
    return out


def wilder(values, n):
    out, avg = [None] * len(values), None
    for i in range(1, len(values)):
        if i < n:
            continue
        avg = sum(values[1:n + 1]) / n if avg is None else (avg * (n - 1) + values[i]) / n
        out[i] = avg
    return out


def rsi(close, n=14):
    up = [0.0] + [max(close[i] - close[i - 1], 0) for i in range(1, len(close))]
    dn = [0.0] + [max(close[i - 1] - close[i], 0) for i in range(1, len(close))]
    g, l = wilder(up, n), wilder(dn, n)
    return [None if a is None else 100.0 if b == 0 else 100 - 100 / (1 + a / b) for a, b in zip(g, l)]


def atr(h, l, c, n=14):
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    return wilder(tr, n)


class Bars:
    """Candle tertutup + indikator yang dipakai semua strategi."""

    def __init__(self, rows):
        self.t = [r[0] / 1000 for r in rows]
        self.o, self.h, self.l, self.c, self.v = ([float(r[k]) for r in rows] for k in (1, 2, 3, 4, 5))
        self.e20, self.e50, self.e200 = ema(self.c, 20), ema(self.c, 50), ema(self.c, 200)
        self.e21, self.e55 = ema(self.c, 21), ema(self.c, 55)
        self.rsi, self.atr = rsi(self.c), atr(self.h, self.l, self.c)

    def __len__(self):
        return len(self.c)

    def ready(self, i):
        return i >= WARMUP and self.e200[i] is not None and self.atr[i] is not None


def cross_up(a, b, i):
    return a[i - 1] is not None and b[i - 1] is not None and a[i] > b[i] and a[i - 1] <= b[i - 1]


def cross_down(a, b, i):
    return cross_up(b, a, i)


# ================= strategi
@dataclass
class AltStrategy:
    name: str
    label: str
    desc: str
    tf: str
    order: int
    entry: Optional[Callable] = None    # True/1 = long, -1 = short (hanya futures), False/0 = tidak masuk
    exit: Optional[Callable] = None
    stop_atr: float = 2.5           # stop awal = harga masuk − stop_atr × ATR
    trail_atr: Optional[float] = None  # trailing = puncak − trail_atr × ATR (None = tanpa trailing)
    max_bars: Optional[int] = None  # keluar paksa setelah sekian candle
    market: str = "spot"            # "spot" atau "futures"
    leverage: float = 1.0           # futures: nilai kontrak = margin × leverage


def _breakout(d, i):
    if i < 48:
        return False
    vol_avg = sum(d.v[i - 20:i]) / 20
    return d.c[i] > max(d.h[i - 48:i]) and d.v[i] > 1.5 * vol_avg and d.c[i] > d.e200[i]


def _breakdown(d, i):
    if i < 48:
        return 0
    vol_avg = sum(d.v[i - 20:i]) / 20
    return -1 if d.c[i] < min(d.l[i - 48:i]) and d.v[i] > 1.5 * vol_avg and d.c[i] < d.e200[i] else 0


def _swing_both(d, i):
    if cross_up(d.e21, d.e55, i) and d.c[i] > d.e200[i]:
        return 1
    if cross_down(d.e21, d.e55, i) and d.c[i] < d.e200[i]:
        return -1
    return 0


STRATEGIES = [
    AltStrategy("trend_ema", "Trend EMA 1j", "Beli saat EMA20 memotong ke atas EMA50 & harga di atas EMA200 (1 jam). "
                "Keluar saat EMA20 turun ke bawah EMA50 atau trailing 3×ATR", "1h", 0,
                entry=lambda d, i: cross_up(d.e20, d.e50, i) and d.c[i] > d.e200[i],
                exit=lambda d, i, p: "tren berbalik" if d.e20[i] < d.e50[i] else None,
                stop_atr=2.5, trail_atr=3.0),
    AltStrategy("breakout", "Breakout 48j", "Beli saat harga menembus titik tertinggi 48 jam dengan volume 1,5× rata-rata. "
                "Keluar jika tembus titik terendah 24 jam atau trailing 3×ATR", "1h", 1,
                entry=_breakout,
                exit=lambda d, i, p: "tembus support 24j" if d.c[i] < min(d.l[i - 24:i]) else None,
                stop_atr=2.0, trail_atr=3.0),
    AltStrategy("rsi_dip", "Beli saat turun (RSI)", "Beli saat RSI(14) < 30 tapi tren besar masih naik (EMA50 > EMA200). "
                "Keluar saat RSI > 55 atau setelah 48 jam", "1h", 2,
                entry=lambda d, i: d.rsi[i] is not None and d.rsi[i] < 30 and d.e50[i] > d.e200[i],
                exit=lambda d, i, p: "RSI pulih" if d.rsi[i] is not None and d.rsi[i] > 55 else None,
                stop_atr=2.5, max_bars=48),
    AltStrategy("swing_4h", "Swing 4j", "Beli saat EMA21 memotong ke atas EMA55 & harga di atas EMA200 (4 jam). "
                "Keluar saat EMA21 turun ke bawah EMA55 atau trailing 3,5×ATR", "4h", 3,
                entry=lambda d, i: cross_up(d.e21, d.e55, i) and d.c[i] > d.e200[i],
                exit=lambda d, i, p: "tren berbalik" if d.e21[i] < d.e55[i] else None,
                stop_atr=3.0, trail_atr=3.5),
    # pembanding: beli BTC sekali di awal lalu diam. Strategi yang bagus harus mengalahkan ini.
    AltStrategy("hodl_btc", "HODL BTC (pembanding)", "Beli BTC dengan seluruh modal di awal lalu tidak pernah dijual", "1h", 4),
    # ---- futures: bisa long & short, pakai leverage
    AltStrategy("fut_swing", "Futures Swing 2 arah", "LONG saat EMA21 memotong ke atas EMA55 di atas EMA200, SHORT saat "
                "memotong ke bawah di bawah EMA200 (4 jam). Tutup saat tren berbalik atau trailing 3,5×ATR. Leverage 3×",
                "4h", 5, entry=_swing_both,
                exit=lambda d, i, p: "tren berbalik" if (d.e21[i] - d.e55[i]) * p.side < 0 else None,
                stop_atr=3.0, trail_atr=3.5, market="futures", leverage=3),
    AltStrategy("fut_breakdown", "Futures Short Breakdown", "SHORT saat harga jebol titik terendah 48 jam dengan volume "
                "1,5× rata-rata & di bawah EMA200. Tutup jika tembus titik tertinggi 24 jam atau trailing 3×ATR. Leverage 3×",
                "1h", 6, entry=_breakdown,
                exit=lambda d, i, p: "tembus resistance 24j" if d.c[i] > max(d.h[i - 24:i]) else None,
                stop_atr=2.0, trail_atr=3.0, market="futures", leverage=3),
    AltStrategy("fut_rsi_short", "Futures Short saat naik (RSI)", "SHORT saat RSI(14) > 70 tapi tren besar turun "
                "(EMA50 < EMA200). Tutup saat RSI < 45 atau setelah 48 jam. Leverage 2×", "1h", 7,
                entry=lambda d, i: -1 if d.rsi[i] is not None and d.rsi[i] > 70 and d.e50[i] < d.e200[i] else 0,
                exit=lambda d, i, p: "RSI turun" if d.rsi[i] is not None and d.rsi[i] < 45 else None,
                stop_atr=2.5, max_bars=48, market="futures", leverage=2),
    AltStrategy("fut_ai", "Futures AI (model belajar)", "Model AI (gradient boosting) menebak arah 12 jam ke depan dari "
                "18 ciri candle & kondisi BTC; hanya masuk pada 10% tebakan paling yakin, dan hanya ke arah (LONG/SHORT) "
                "yang sudah terbukti untung di data uji. Dilatih ulang tiap 24 jam. Tutup setelah 12 jam, tebakan berbalik, atau trailing 2,5×ATR. Leverage 2×", "1h", 8,
                entry=lambda d, i: AI.signal(d, i),
                exit=lambda d, i, p: "AI berbalik arah" if AI.signal(d, i) == -p.side else None,
                stop_atr=2.0, trail_atr=2.5, max_bars=12, market="futures", leverage=2),
]
BY_NAME = {s.name: s for s in STRATEGIES}


# ================= pembukuan
@dataclass
class Pos:
    strategy: str
    symbol: str
    opened: float
    entry: float
    qty: float
    cost: float
    stop: float
    peak: float             # harga terbaik sejak masuk (tertinggi untuk long, terendah untuk short)
    side: int = 1           # 1 = long, -1 = short
    lev: float = 1.0
    liq: float = 0.0        # harga likuidasi (0 = spot, tidak bisa dilikuidasi)
    funding: float = 0.0    # total funding yang sudah dibayar (negatif = diterima)

    @property
    def is_fut(self):
        return self.liq > 0


def pos_value(pos, price):
    """Nilai posisi di harga tertentu (sebelum fee keluar). Futures: margin + untung/rugi − funding, minimal 0."""
    if not pos.is_fut:
        return pos.qty * price
    margin = pos.qty * pos.entry / pos.lev
    return max(0.0, margin + pos.side * pos.qty * (price - pos.entry) - pos.funding)


def open_value(row, price):
    """Sama seperti pos_value, untuk baris tabel alt_open (dipakai dashboard & Telegram)."""
    keys = row.keys()
    return pos_value(Pos(**{k: row[k] for k in POS_COLS if k in keys}), price)


def side_name(pos):
    return ("LONG" if pos.side > 0 else "SHORT") + f" {pos.lev:g}×" if pos.is_fut else "SPOT"


class Book:
    def __init__(self, strat, cash, started):
        self.strat, self.cash, self.started = strat, cash, started
        self.pos = {}
        self.closed = []    # (pnl, ret) untuk statistik backtest

    def equity(self, prices):
        return self.cash + sum(pos_value(p, prices.get(p.symbol, p.entry)) for p in self.pos.values())


SCHEMA = """
CREATE TABLE IF NOT EXISTS alt_trades(id INTEGER PRIMARY KEY, ts REAL, strategy TEXT, symbol TEXT, side TEXT,
    reason TEXT, price REAL, qty REAL, usd REAL);
CREATE TABLE IF NOT EXISTS alt_positions(id INTEGER PRIMARY KEY, strategy TEXT, symbol TEXT, opened REAL, closed REAL,
    entry REAL, exit REAL, qty REAL, cost REAL, proceeds REAL, pnl REAL, ret REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS alt_open(strategy TEXT, symbol TEXT, opened REAL, entry REAL, qty REAL, cost REAL,
    stop REAL, peak REAL, PRIMARY KEY(strategy, symbol));
CREATE TABLE IF NOT EXISTS alt_books(strategy TEXT PRIMARY KEY, cash REAL, started REAL);
CREATE TABLE IF NOT EXISTS alt_equity(ts REAL, strategy TEXT, equity REAL);
CREATE INDEX IF NOT EXISTS alt_equity_ts ON alt_equity(strategy, ts);
CREATE TABLE IF NOT EXISTS alt_market(symbol TEXT PRIMARY KEY, base TEXT, rank INT, watch INT, price REAL,
    chg24 REAL, rsi1h REAL, trend1h TEXT, trend4h TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS alt_backtest(strategy TEXT PRIMARY KEY, tf TEXT, days REAL, ret_pct REAL, trades INT,
    wins INT, max_dd REAL, pf REAL, hodl_pct REAL, ran_at REAL);
CREATE TABLE IF NOT EXISTS alt_stats(key TEXT PRIMARY KEY, value REAL);
CREATE TABLE IF NOT EXISTS alt_ai_runs(ts REAL PRIMARY KEY, kind TEXT, meta TEXT);
"""
POS_COLS = ("strategy", "symbol", "opened", "entry", "qty", "cost", "stop", "peak", "side", "lev", "liq", "funding")
MIGRATE = {"alt_open": [("side", "INT DEFAULT 1"), ("lev", "REAL DEFAULT 1"), ("liq", "REAL DEFAULT 0"),
                        ("funding", "REAL DEFAULT 0")],
           "alt_positions": [("side", "INT DEFAULT 1"), ("lev", "REAL DEFAULT 1"), ("funding", "REAL DEFAULT 0")],
           "alt_trades": [("dir", "INT DEFAULT 1"), ("lev", "REAL DEFAULT 1")]}


def alt_connect(path=ALT_DB):
    db = sqlite3.connect(path, timeout=10)
    db.executescript(SCHEMA)
    for table, cols in MIGRATE.items():   # database lama (sebelum futures) ditambah kolom baru
        have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        for name, kind in cols:
            if name not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
    db.commit()
    return db


def fmt_price(p):
    if p is None:
        return "–"
    return f"{p:,.2f}" if p >= 100 else f"{p:.4f}" if p >= 1 else f"{p:.4g}"


class Engine:
    """Logika beli/jual yang sama untuk live dan backtest."""

    def __init__(self, s, books, slip_of, on_trade=None, fut_ok=None):
        self.s, self.books, self.slip_of, self.on_trade = s, books, slip_of, on_trade
        self.fut_ok = fut_ok    # simbol yang punya kontrak perpetual (None = semua boleh)

    def buy(self, book, symbol, price, ts, stop, size, side=1):
        st = book.strat
        fill = price * (1 + side * self.slip_of(symbol))
        if st.market == "futures":
            margin = size / (1 + st.leverage * self.s.fut_fee_pct)   # sisanya untuk fee buka posisi
            qty = margin * st.leverage / fill
            liq = fill * (1 - side * (1 / st.leverage - MMR))
            # stop selalu sebelum harga likuidasi
            stop = max(stop, fill - 0.9 * (fill - liq)) if side > 0 else min(stop, fill + 0.9 * (liq - fill))
            pos = Pos(st.name, symbol, ts, fill, qty, size, stop, fill, side, float(st.leverage), liq)
        else:
            pos = Pos(st.name, symbol, ts, fill, size / (fill * (1 + self.s.fee_pct)), size, stop, fill)
        book.cash -= size
        book.pos[symbol] = pos
        if self.on_trade:
            self.on_trade("BELI", book, pos, fill, size, "sinyal masuk")
        return pos

    def sell(self, book, pos, price, ts, reason, liquidated=False):
        fill = price if liquidated else price * (1 - pos.side * self.slip_of(pos.symbol))
        if liquidated:
            proceeds = 0.0
        elif pos.is_fut:
            proceeds = max(0.0, pos_value(pos, fill) - pos.qty * fill * self.s.fut_fee_pct)
        else:
            proceeds = pos.qty * fill * (1 - self.s.fee_pct)
        book.cash += proceeds
        del book.pos[pos.symbol]
        book.closed.append((proceeds - pos.cost, proceeds / pos.cost))
        if self.on_trade:
            self.on_trade("JUAL", book, pos, fill, proceeds, reason, ts=ts)

    def check_stop(self, book, pos, low, high, ts, gap_open=None):
        """Harga menyentuh stop -> tutup di stop (atau di harga buka kalau langsung gap melewatinya).
        Futures yang gap melewati harga likuidasi kehilangan seluruh margin."""
        s = pos.side
        if (low > pos.stop) if s > 0 else (high < pos.stop):
            return False
        if gap_open is not None:
            fill = min(gap_open, pos.stop) if s > 0 else max(gap_open, pos.stop)
        else:
            fill = low if s > 0 else high
        if pos.is_fut and (fill - pos.liq) * s <= 0:
            self.sell(book, pos, pos.liq, ts, "LIKUIDASI", liquidated=True)
            return True
        self.sell(book, pos, fill, ts, "trailing stop" if (pos.stop - pos.entry) * s > 0 else "stop loss")
        return True

    def apply_funding(self, book, prices, rates):
        """Funding perpetual: rate positif = long membayar short; rate negatif sebaliknya."""
        for pos in book.pos.values():
            if pos.is_fut and pos.symbol in prices:
                pos.funding += pos.side * pos.qty * prices[pos.symbol] * rates.get(pos.symbol, FUND_DEFAULT)

    def on_bar(self, book, symbol, d, i, fill_price, ts, prices):
        st = book.strat
        pos = book.pos.get(symbol)
        if pos:
            if pos.side > 0:
                pos.peak = max(pos.peak, d.h[i])
                if st.trail_atr:
                    pos.stop = max(pos.stop, pos.peak - st.trail_atr * d.atr[i])
            else:
                pos.peak = min(pos.peak, d.l[i])
                if st.trail_atr:
                    pos.stop = min(pos.stop, pos.peak + st.trail_atr * d.atr[i])
            reason = st.exit(d, i, pos) if st.exit else None
            if not reason and st.max_bars and ts - pos.opened >= st.max_bars * TF_SEC[st.tf]:
                reason = f"batas waktu {st.max_bars} candle"
            if not reason:
                return
            self.sell(book, pos, fill_price, ts, reason)
            if st.market != "futures":
                return      # futures boleh langsung balik arah di candle yang sama
        if len(book.pos) >= self.s.max_positions:
            return
        if st.market == "futures" and self.fut_ok is not None and symbol not in self.fut_ok:
            return
        sig = st.entry(d, i)
        if not sig:
            return
        side = -1 if sig < 0 else 1
        if side < 0 and st.market != "futures":
            return
        size = min(book.cash, book.equity(prices) / self.s.max_positions)
        if size < 1:
            return
        self.buy(book, symbol, fill_price, ts, fill_price - side * st.stop_atr * d.atr[i], size, side)


# ================= data pasar
async def get_json(http, url, params=None, tries=3):
    for k in range(tries):
        try:
            r = await http.get(url, params=params)
            r.raise_for_status()
            return r.json()
        except Exception:
            if k == tries - 1:
                raise
            await asyncio.sleep(2 + 3 * k)


async def load_universe(http, s):
    info = await get_json(http, BINANCE + "/exchangeInfo", {"permissions": "SPOT"})
    tradable = {x["baseAsset"] for x in info["symbols"] if x["quoteAsset"] == "USDT" and x["status"] == "TRADING"}
    try:
        cg = await get_json(http, COINGECKO, {"vs_currency": "usd", "order": "market_cap_desc", "per_page": 120, "page": 1})
        ranked = [c["symbol"].upper() for c in cg]
    except Exception as e:
        log(f"CoinGecko gagal ({e!r}), pakai urutan volume Binance")
        t24 = await get_json(http, BINANCE + "/ticker/24hr")
        t24 = [t for t in t24 if t["symbol"].endswith("USDT")]
        ranked = [t["symbol"][:-4] for t in sorted(t24, key=lambda t: -float(t["quoteVolume"]))]
    top, seen = [], set()
    for base in ranked:
        if base in STABLE or base not in tradable or base in seen:
            continue
        seen.add(base)
        top.append(base)
        if len(top) >= s.top_n:
            break
    coins = [{"base": b, "symbol": b + "USDT", "rank": k + 1, "watch": 0} for k, b in enumerate(top)]
    for w in [w.strip().upper() for w in s.watchlist.split(",") if w.strip()]:
        if w in seen:
            next(c for c in coins if c["base"] == w)["watch"] = 1
        elif w in tradable:
            coins.append({"base": w, "symbol": w + "USDT", "rank": None, "watch": 1})
            seen.add(w)
        else:
            log(f"Watchlist {w} tidak ada di Binance spot USDT, dilewati")
    return coins


async def load_fut_symbols(http):
    """Koin yang punya kontrak perpetual USDT (OKX). None kalau gagal (semua koin boleh)."""
    try:
        r = await get_json(http, OKX + "/instruments", {"instType": "SWAP"})
        return {x["instId"].split("-")[0] + "USDT" for x in r["data"] if x.get("settleCcy") == "USDT"}
    except Exception as e:
        log(f"Daftar kontrak futures OKX gagal diambil ({e!r}), semua koin dianggap ada futures-nya")
        return None


async def load_funding(http):
    r = await get_json(http, OKX + "/funding-rate", {"instId": "ANY"})
    return {x["instId"].split("-")[0] + "USDT": float(x["fundingRate"]) for x in r["data"]
            if x["instId"].endswith("-USDT-SWAP") and x.get("fundingRate") not in (None, "")}


async def fetch_bars(http, symbol, tf, limit):
    rows, end = [], None
    while len(rows) < limit:   # Binance memberi maksimal 1000 candle per permintaan
        params = {"symbol": symbol, "interval": tf, "limit": min(1000, limit - len(rows))}
        if end:
            params["endTime"] = end
        chunk = await get_json(http, BINANCE + "/klines", params)
        rows = chunk + rows
        if len(chunk) < params["limit"]:
            break
        end = chunk[0][0] - 1
    now_ms = time.time() * 1000
    return Bars([r for r in rows if r[6] < now_ms])   # hanya candle yang sudah tertutup


async def fetch_all(http, symbols, tf, limit):
    sem = asyncio.Semaphore(8)

    async def one(sym):
        async with sem:
            try:
                return sym, await fetch_bars(http, sym, tf, limit)
            except Exception as e:
                log(f"Gagal ambil candle {sym} {tf} ({e!r})")
                return sym, None
    data = {sym: b for sym, b in await asyncio.gather(*(one(x) for x in symbols)) if b and len(b) > WARMUP}
    for d in data.values():
        d.btc = data.get("BTCUSDT")    # konteks pasar untuk model AI
    return data


def make_slip(s, coins):
    big = {c["symbol"] for c in coins if c["rank"] and c["rank"] <= 10}
    return lambda sym: s.slip_big if sym in big else s.slip_small


# ================= backtest
def backtest(s, coins, data_by_tf, fut_ok=None):
    """Jalankan semua strategi di data historis. Hasil: dict nama -> statistik.
    Strategi AI diuji walk-forward: model belajar dari 70% data awal, lalu diuji di 30% sisanya."""
    slip_of = make_slip(s, coins)
    out = {}
    for st in STRATEGIES:
        data = data_by_tf.get(st.tf, {})
        btc = data.get("BTCUSDT")
        if not btc:
            continue
        times = sorted({t for d in data.values() for t in d.t[WARMUP:]})
        bt_ai = None
        if st.name == "fut_ai":
            split = times[int(len(times) * AI_SPLIT)]
            bt_ai = AiModel()
            try:
                out["_ai"] = bt_ai.fit(data, t_end=split)
            except ValueError as e:
                log(f"Backtest AI dilewati ({e})")
                continue
            times = [t for t in times if t >= split]
        with AI.use(bt_ai) if bt_ai else nullcontext():
            out[st.name] = _run_one(s, st, data, btc, times, slip_of, fut_ok)
    return out


def _run_one(s, st, data, btc, times, slip_of, fut_ok):
    index = {sym: {t: i for i, t in enumerate(d.t)} for sym, d in data.items()}
    book = Book(st, s.budget_usd, times[0])
    eng = Engine(s, {st.name: book}, slip_of, fut_ok=fut_ok)
    prices, curve = {}, []
    bi = index["BTCUSDT"]
    btc_start = btc.c[bi[times[0]]] if times[0] in bi else btc.c[WARMUP]
    if st.name == "hodl_btc":
        eng.buy(book, "BTCUSDT", btc_start, times[0], 0, book.cash)
    for t in times:
        if t % FUND_SEC == 0:
            eng.apply_funding(book, prices, {})
        for sym, d in data.items():
            i = index[sym].get(t)
            if i is None:
                continue
            prices[sym] = d.c[i]
            if st.name == "hodl_btc" or not d.ready(i):
                continue
            pos = book.pos.get(sym)
            if pos and eng.check_stop(book, pos, d.l[i], d.h[i], t, gap_open=d.o[i]):
                continue
            eng.on_bar(book, sym, d, i, d.c[i], t, prices)
        curve.append(book.equity(prices))
    for pos in list(book.pos.values()):   # tutup di harga terakhir supaya statistik lengkap
        eng.sell(book, pos, prices[pos.symbol], times[-1], "akhir backtest")
    peak, max_dd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        max_dd = min(max_dd, v / peak - 1)
    wins = [p for p, _ in book.closed if p > 0]
    losses = [-p for p, _ in book.closed if p <= 0]
    btc_end = btc.c[-1]
    return {"tf": st.tf, "days": (times[-1] - times[0]) / 86400,
            "ret_pct": (book.cash / s.budget_usd - 1) * 100, "trades": len(book.closed),
            "wins": len(wins), "max_dd": max_dd * 100,
            "pf": (sum(wins) / sum(losses)) if losses and sum(losses) > 0 else None,
            "hodl_pct": (btc_end / btc_start - 1) * 100,
            "liq": sum(1 for _, r in book.closed if r == 0)}


async def run_backtest(s, http, coins, fut_ok=None):
    """Ambil data historis lalu backtest. Data dikembalikan juga untuk melatih model AI live."""
    symbols = [c["symbol"] for c in coins]
    data_by_tf = {tf: await fetch_all(http, symbols, tf, HISTORY[tf]) for tf in TF_SEC}
    return backtest(s, coins, data_by_tf, fut_ok), data_by_tf


def print_backtest(res):
    print("\nBACKTEST (simulasi di data historis, sudah dipotong fee & slippage)")
    ai = res.get("_ai")
    for name, r in res.items():
        if name == "_ai":
            continue
        st = BY_NAME[name]
        pf = f"{r['pf']:.2f}" if r["pf"] else "–"
        wr = f"{r['wins'] / r['trades']:.0%}" if r["trades"] else "–"
        print(f"  {st.label:<30} {r['days']:5.0f} hari | hasil {r['ret_pct']:+6.1f}% | {r['trades']:3d} trade,"
              f" menang {wr:>4} | PF {pf:>5} | max DD {r['max_dd']:5.1f}% | HODL BTC {r['hodl_pct']:+.1f}%"
              + (f" | likuidasi {r['liq']}" if r.get("liq") else ""))
    if ai:
        print("\n" + ai_report(ai, "Model AI backtest (belajar dari 70% data awal)"))


def ai_report(m, title):
    def side(k, word):
        if not m.get(k + "_n"):
            return f"  sinyal {word}: tidak ada"
        return (f"  sinyal {word}: {m[k + '_n']} kali, benar arah {m[k + '_hit']:.0%},"
                f" rata-rata {m[k + '_ret']:+.2f}% dalam {m['horizon']} jam")
    return "\n".join([
        f"{title}: {m['samples']:,} contoh dari {m['coins']} koin ({m['days']:.0f} hari)",
        f"  akurasi di data uji {m['acc']:.1%} (tebak kelas terbanyak saja: {m['base_acc']:.1%})",
        side("long", "LONG"), side("short", "SHORT"),
        "  arah yang boleh dipakai: " + (" & ".join("LONG" if x > 0 else "SHORT" for x in m.get("sides", []))
                                          or "TIDAK ADA (AI tidak membuka posisi sampai ada yang terbukti)")])


# ================= live
class AltSim:
    def __init__(self, s):
        self.s = s
        self.db = alt_connect()
        self.coins, self.prices, self.chg24 = [], {}, {}
        self.fut_ok, self.fund_rates = None, {}
        self.books = {}
        self.last_bar = {tf: self.stat(f"last_bar_{tf}") or 0 for tf in TF_SEC}
        self.outbox = []
        self.slip_of = lambda sym: s.slip_small
        now = time.time()
        for st in STRATEGIES:
            row = self.db.execute("SELECT cash, started FROM alt_books WHERE strategy=?", (st.name,)).fetchone()
            if not row:
                row = (s.budget_usd, now)
                self.db.execute("INSERT INTO alt_books VALUES(?,?,?)", (st.name, *row))
            self.books[st.name] = Book(st, row[0], row[1])
        for r in self.db.execute(f"SELECT {','.join(POS_COLS)} FROM alt_open"):
            if r[0] in self.books:
                self.books[r[0]].pos[r[1]] = Pos(*r)
        self.db.commit()
        self.engine = Engine(s, self.books, lambda sym: self.slip_of(sym), self.on_trade)
        n_open = sum(len(b.pos) for b in self.books.values())
        log(f"Simulator altcoin siap: {len(STRATEGIES)} strategi, modal ${s.budget_usd:g} per strategi,"
            f" {n_open} posisi dilanjutkan")
        if AI.load():
            log(f"Model AI dimuat (dilatih {time.strftime('%d/%m %H:%M', time.localtime(AI.meta['trained_at']))})")

    def stat(self, key):
        r = self.db.execute("SELECT value FROM alt_stats WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def set_stat(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO alt_stats VALUES(?,?)", (key, value))

    def notify(self, text):
        if self.s.notify and self.s.telegram_bot_token and self.s.telegram_chat_id:
            self.outbox.append(text)

    def save_pos(self, pos):
        self.db.execute(f"INSERT OR REPLACE INTO alt_open({','.join(POS_COLS)}) VALUES({','.join('?' * len(POS_COLS))})",
                        tuple(getattr(pos, k) for k in POS_COLS))

    def on_trade(self, side, book, pos, fill, usd, reason, ts=None):
        ts = ts or time.time()
        self.db.execute("INSERT INTO alt_trades(ts,strategy,symbol,side,reason,price,qty,usd,dir,lev)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (ts, book.strat.name, pos.symbol, side, reason, fill, pos.qty, usd, pos.side, pos.lev))
        base, kind = pos.symbol[:-4], side_name(pos)
        if side == "BELI":
            self.save_pos(pos)
            extra = f", likuidasi {fmt_price(pos.liq)}" if pos.is_fut else ""
            log(f"[{book.strat.name}] {'BUKA ' + kind if pos.is_fut else 'BELI'} {base} ${usd:.2f} @ {fmt_price(fill)}"
                f" (stop {fmt_price(pos.stop)}{extra})")
            if book.strat.name != "hodl_btc":
                head = (f"{'🟢' if pos.side > 0 else '🟣'} FUT {kind} {base}" if pos.is_fut else f"🔵 ALT BELI {base}")
                self.notify(f"{head} · {book.strat.label}\nHarga {fmt_price(fill)} · {'margin ' if pos.is_fut else ''}"
                            f"${usd:.2f} · stop {fmt_price(pos.stop)}{extra}\n(simulasi, bukan saran investasi)")
        else:
            pnl, ret = usd - pos.cost, usd / pos.cost
            self.db.execute("DELETE FROM alt_open WHERE strategy=? AND symbol=?", (pos.strategy, pos.symbol))
            self.db.execute("INSERT INTO alt_positions(strategy,symbol,opened,closed,entry,exit,qty,cost,proceeds,pnl,"
                            "ret,reason,side,lev,funding) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (pos.strategy, pos.symbol, pos.opened, ts, pos.entry, fill, pos.qty, pos.cost, usd, pnl,
                             ret, reason, pos.side, pos.lev, pos.funding))
            log(f"[{book.strat.name}] {'TUTUP ' + kind if pos.is_fut else 'JUAL'} {base} @ {fmt_price(fill)}"
                f" {(ret - 1) * 100:+.1f}% ({pnl:+.2f} USD) · {reason}")
            head = f"FUT TUTUP {kind} {base}" if pos.is_fut else f"ALT JUAL {base}"
            self.notify(f"{'💥' if reason == 'LIKUIDASI' else '✅' if pnl >= 0 else '🔻'} {head} · {book.strat.label}\n"
                        f"{(ret - 1) * 100:+.1f}% ({'+' if pnl >= 0 else '-'}${abs(pnl):.2f}) · {reason}")
        self.db.execute("UPDATE alt_books SET cash=? WHERE strategy=?", (book.cash, book.strat.name))
        self.db.commit()

    async def refresh_universe(self, http):
        self.coins = await load_universe(http, self.s)
        self.fut_ok = self.engine.fut_ok = await load_fut_symbols(http)
        held = {sym for b in self.books.values() for sym in b.pos}
        known = {c["symbol"] for c in self.coins}
        for sym in held - known:   # koin yang masih dipegang tetap dipantau walau keluar dari daftar
            self.coins.append({"base": sym[:-4], "symbol": sym, "rank": None, "watch": 0})
        self.slip_of = make_slip(self.s, self.coins)
        self.db.execute("DELETE FROM alt_market WHERE symbol NOT IN (%s)" % ",".join("?" * len(self.coins)),
                        [c["symbol"] for c in self.coins])
        for c in self.coins:
            self.db.execute("INSERT INTO alt_market(symbol,base,rank,watch) VALUES(?,?,?,?) ON CONFLICT(symbol) DO "
                            "UPDATE SET base=excluded.base, rank=excluded.rank, watch=excluded.watch",
                            (c["symbol"], c["base"], c["rank"], c["watch"]))
        self.set_stat("universe_at", time.time())
        self.db.commit()
        log(f"Memantau {len(self.coins)} koin: " + ", ".join(c["base"] for c in self.coins))

    async def maybe_backtest(self, http):
        last = self.stat("backtest_at") or 0
        if time.time() - last < BACKTEST_EVERY and AI.model is not None:
            return
        log("Menjalankan backtest harian & melatih ulang AI…")
        res, data_by_tf = await run_backtest(self.s, http, self.coins, self.fut_ok)
        if "_ai" in res:
            self.db.execute("INSERT OR REPLACE INTO alt_ai_runs VALUES(?,?,?)",
                            (time.time(), "backtest", json.dumps(res.pop("_ai"))))
        try:
            meta = AI.fit(data_by_tf["1h"])
            AI.save()
            self.db.execute("INSERT OR REPLACE INTO alt_ai_runs VALUES(?,?,?)", (time.time(), "live", json.dumps(meta)))
            log(ai_report(meta, "Model AI live dilatih ulang"))
        except Exception as e:
            log(f"Latihan AI gagal ({e!r})")
        for name, r in res.items():
            self.db.execute("INSERT OR REPLACE INTO alt_backtest VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (name, r["tf"], r["days"], r["ret_pct"], r["trades"], r["wins"], r["max_dd"], r["pf"],
                             r["hodl_pct"], time.time()))
        self.set_stat("backtest_at", time.time())
        self.db.commit()
        print_backtest(res)

    async def refresh_funding(self, http):
        if time.time() - (self.stat("funding_rates_at") or 0) < 3600 and self.fund_rates:
            return
        try:
            self.fund_rates = await load_funding(http)
            self.set_stat("funding_rates_at", time.time())
        except Exception as e:
            log(f"Funding rate OKX gagal diambil ({e!r}), pakai 0,01%")

    def maybe_pay_funding(self):
        due = (time.time() // FUND_SEC) * FUND_SEC
        last = self.stat("funding_paid_at")
        if last is not None and due <= last:
            return
        if last is not None:   # saat pertama kali jalan hanya mencatat jadwal, tidak menagih
            for book in self.books.values():
                self.engine.apply_funding(book, self.prices, self.fund_rates)
                for pos in book.pos.values():
                    if pos.is_fut:
                        self.save_pos(pos)
            log("Funding futures 8 jam dibayar/diterima")
        self.set_stat("funding_paid_at", due)

    async def poll_prices(self, http):
        rows = await get_json(http, BINANCE + "/ticker/24hr", {"type": "MINI"})
        want = {c["symbol"] for c in self.coins}
        for r in rows:
            if r["symbol"] in want:
                price, open_ = float(r["lastPrice"]), float(r["openPrice"])
                self.prices[r["symbol"]] = price
                self.chg24[r["symbol"]] = (price / open_ - 1) * 100 if open_ else None
        now = time.time()
        for sym, price in self.prices.items():
            self.db.execute("UPDATE alt_market SET price=?, chg24=?, updated=? WHERE symbol=?",
                            (price, self.chg24.get(sym), now, sym))
        for book in self.books.values():   # stop loss / trailing dicek setiap menit
            for pos in list(book.pos.values()):
                price = self.prices.get(pos.symbol)
                if price is not None and book.strat.name != "hodl_btc":
                    self.engine.check_stop(book, pos, price, price, now)
        self.db.commit()

    async def on_candles(self, http, tf):
        closed_open = (time.time() // TF_SEC[tf]) * TF_SEC[tf] - TF_SEC[tf]
        if closed_open <= self.last_bar[tf]:
            return
        data = await fetch_all(http, [c["symbol"] for c in self.coins], tf, LIVE_BARS)
        now = time.time()
        for sym, d in data.items():
            i = len(d) - 1
            if d.t[i] != closed_open:
                continue   # data koin ini belum memuat candle terbaru, coba lagi di siklus berikutnya
            if tf == "1h":
                trend = "NAIK" if d.e50[i] and d.e200[i] and d.c[i] > d.e50[i] > d.e200[i] else \
                        "TURUN" if d.e50[i] and d.e200[i] and d.c[i] < d.e50[i] < d.e200[i] else "DATAR"
                self.db.execute("UPDATE alt_market SET rsi1h=?, trend1h=? WHERE symbol=?", (d.rsi[i], trend, sym))
            else:
                trend = "NAIK" if d.e21[i] and d.e55[i] and d.e21[i] > d.e55[i] else "TURUN"
                self.db.execute("UPDATE alt_market SET trend4h=? WHERE symbol=?", (trend, sym))
            if not d.ready(i):
                continue
            price = self.prices.get(sym, d.c[i])
            for book in self.books.values():
                if book.strat.tf == tf and book.strat.name != "hodl_btc":
                    self.engine.on_bar(book, sym, d, i, price, now, self.prices)
                    pos = book.pos.get(sym)
                    if pos:
                        self.save_pos(pos)
        self.last_bar[tf] = closed_open
        self.set_stat(f"last_bar_{tf}", closed_open)
        self.db.commit()
        log(f"Candle {tf} {time.strftime('%d/%m %H:%M', time.localtime(closed_open))} diproses ({len(data)} koin)")

    def ensure_hodl(self):
        book = self.books["hodl_btc"]
        if not book.pos and book.cash >= 1 and "BTCUSDT" in self.prices:
            self.engine.buy(book, "BTCUSDT", self.prices["BTCUSDT"], time.time(), 0, book.cash)

    def record_equity(self):
        now = time.time()
        for name, book in self.books.items():
            self.db.execute("INSERT INTO alt_equity VALUES(?,?,?)", (now, name, book.equity(self.prices)))
        self.db.commit()

    async def telegram_sender(self, http):
        url = f"https://api.telegram.org/bot{self.s.telegram_bot_token}/sendMessage"
        while self.outbox:
            try:
                r = await http.post(url, json={"chat_id": self.s.telegram_chat_id, "text": self.outbox[0][:4000]})
                if r.status_code == 429:
                    await asyncio.sleep(r.json().get("parameters", {}).get("retry_after", 5))
                    continue
            except Exception as e:
                log(f"Gagal kirim Telegram ({e!r})")
                return
            self.outbox.pop(0)

    async def run(self):
        async with httpx.AsyncClient(timeout=30) as http:
            last_universe = last_equity = 0
            while True:
                try:
                    if time.time() - last_universe > 24 * 3600 or not self.coins:
                        await self.refresh_universe(http)
                        last_universe = time.time()
                    await self.poll_prices(http)
                    await self.refresh_funding(http)
                    self.maybe_pay_funding()
                    self.ensure_hodl()
                    for tf in TF_SEC:
                        await self.on_candles(http, tf)
                    if time.time() - last_equity >= 300:
                        self.record_equity()
                        last_equity = time.time()
                    await self.maybe_backtest(http)
                    self.set_stat("heartbeat", time.time())
                    self.db.commit()
                    await self.telegram_sender(http)
                except Exception as e:
                    log(f"Siklus gagal ({e!r}), dicoba lagi")
                await asyncio.sleep(self.s.poll_sec)


def alt_status_text():
    """Ringkasan untuk perintah Telegram /alt (dipanggil dari sim.py)."""
    if not ALT_DB.exists():
        return "Simulator altcoin belum pernah dijalankan."
    db = sqlite3.connect(f"file:{ALT_DB}?mode=ro", uri=True, timeout=5)
    try:
        db.row_factory = sqlite3.Row
        prices = {r["symbol"]: r["price"] for r in db.execute("SELECT symbol, price FROM alt_market")}
        hb = db.execute("SELECT value FROM alt_stats WHERE key='heartbeat'").fetchone()
        lines = ["ALTCOIN (simulasi, bukan uang sungguhan)"]
        if hb and time.time() - hb[0] > 300:
            lines.append(f"⚠️ simulator altcoin tidak aktif sejak {time.strftime('%d/%m %H:%M', time.localtime(hb[0]))}")
        rows, budget = [], load_alt_settings().budget_usd
        for st in STRATEGIES:
            cash = db.execute("SELECT cash FROM alt_books WHERE strategy=?", (st.name,)).fetchone()
            if not cash:
                continue
            opens = db.execute("SELECT * FROM alt_open WHERE strategy=?", (st.name,)).fetchall()
            eq = cash[0] + sum(open_value(o, prices.get(o["symbol"]) or o["entry"]) for o in opens)
            n, w = db.execute("SELECT COUNT(*), COALESCE(SUM(pnl>0),0) FROM alt_positions WHERE strategy=?",
                              (st.name,)).fetchone()
            rows.append((eq / budget - 1, f"{st.label}: ${eq:.2f} ({(eq / budget - 1) * 100:+.1f}%) | {n} trade selesai,"
                         f" {w} untung | dipegang: "
                         + (", ".join(o["symbol"][:-4] + ("" if o["liq"] == 0 else " L" if o["side"] > 0 else " S")
                                      for o in opens) or "-")))
        lines += [f"{k}. {t}" for k, (_, t) in enumerate(sorted(rows, key=lambda r: -r[0]), 1)]
        return "\n".join(lines)
    finally:
        db.close()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    s = load_alt_settings()
    if "--backtest" in sys.argv:
        async def bt():
            async with httpx.AsyncClient(timeout=30) as http:
                coins = await load_universe(http, s)
                fut_ok = await load_fut_symbols(http)
                log(f"Backtest {len(coins)} koin…")
                print_backtest((await run_backtest(s, http, coins, fut_ok))[0])
        asyncio.run(bt())
        return
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        print("Simulator altcoin sudah berjalan di jendela lain.")
        time.sleep(8)
        return
    try:
        asyncio.run(AltSim(s).run())
    except KeyboardInterrupt:
        log("Simulator altcoin dihentikan. Posisi terbuka disimpan dan dilanjutkan saat dijalankan lagi.")


if __name__ == "__main__":
    main()
