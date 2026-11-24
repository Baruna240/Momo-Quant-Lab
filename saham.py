"""Simulator saham Indonesia (BEI): paper trading TANPA uang sungguhan, data harian publik Yahoo Finance (kode .JK).

Jalankan: python saham.py               berjalan terus; cek harga tiap 5 menit saat bursa buka
          python saham.py --backtest    uji semua strategi di data 2 tahun, lalu keluar
          python saham.py --once        satu putaran cek lalu keluar
          python saham.py buy BBCA 2    order manual (buku "manual"): beli 2 lot BBCA harga pasar
          python saham.py sell BBCA 1 9500   jual 1 lot dengan limit 9.500
          python saham.py status        ringkasan semua buku

Cara kerja (sama untuk backtest dan live, supaya hasil backtest bisa dibandingkan):
- Sinyal dihitung dari candle HARIAN yang sudah selesai; eksekusi di hari bursa berikutnya (backtest: harga open
  hari berikutnya; live: harga saat itu ketika bursa buka). Satu sinyal hanya dieksekusi sekali.
- Stop loss dicek terus saat bursa buka (backtest: low harian, terisi di harga stop atau open bila gap turun).
- Tiap strategi punya modal sendiri (SAHAM_BUDGET_IDR) dibagi rata ke SAHAM_MAX_POSITIONS saham, dibulatkan ke lot.
- Order lewat saham_broker.Broker; sekarang PaperBroker. Data Yahoo bisa tertunda ~10 menit.
"""
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

import httpx
import numpy as np

import indicators as ind
import saham_ipo
from saham_broker import (LOT, WIB, Order, fmt_idr, get_broker, load_saham_settings, market_open, price_band,
                          round_tick, saham_connect, tick_size)

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/"
UA = {"User-Agent": "Mozilla/5.0"}
BENCH = "^JKSE"     # IHSG
WARMUP = 200
EQUITY_EVERY = 900  # simpan titik equity paling sering tiap 15 menit
FUND_EVERY = 20 * 3600


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ---------------------------------------------------------------- data
@dataclass
class Daily:
    symbol: str
    dates: list           # "YYYY-MM-DD" (WIB)
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    last: float
    prev_close: float
    name: str = ""


def fetch_daily(client, symbol, rng="2y"):
    ticker = symbol if symbol.startswith("^") else symbol + ".JK"
    r = client.get(YAHOO + ticker, params={"range": rng, "interval": "1d"}, headers=UA, timeout=20)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    rows = [(t, o, h, l, c, v or 0) for t, o, h, l, c, v in
            zip(res["timestamp"], q["open"], q["high"], q["low"], q["close"], q["volume"]) if None not in (o, h, l, c)]
    dates = [datetime.fromtimestamp(t, WIB).strftime("%Y-%m-%d") for t, *_ in rows]
    arr = np.array([r[1:] for r in rows], float)
    meta = res["meta"]
    last = float(meta.get("regularMarketPrice") or arr[-1, 3])
    prev = float(meta.get("chartPreviousClose") if rng == "1d" else (arr[-2, 3] if len(arr) > 1 else last))
    # candle hari ini (bila ada) belum selesai: prev_close = close candle sebelumnya
    if dates and dates[-1] != datetime.now(WIB).strftime("%Y-%m-%d") and len(arr):
        prev = float(arr[-1, 3])
    return Daily(symbol, dates, arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3], arr[:, 4], last, prev,
                 meta.get("shortName") or meta.get("longName") or "")


def fetch_fundamentals(client, symbols):
    """Angka fundamental TERBARU dari Yahoo (butuh cookie + crumb). Kembalikan {kode: dict}."""
    client.get("https://fc.yahoo.com", headers=UA, timeout=20)
    crumb = client.get("https://query1.finance.yahoo.com/v1/test/getcrumb", headers=UA, timeout=20).text.strip()
    out = {}
    for sym in symbols:
        try:
            r = client.get(f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{sym}.JK", headers=UA, timeout=20,
                           params={"modules": "defaultKeyStatistics,financialData,summaryDetail", "crumb": crumb})
            r.raise_for_status()
            j = r.json()["quoteSummary"]["result"][0]
        except Exception:
            continue

        def g(m, k):
            v = m.get(k)
            return v.get("raw") if isinstance(v, dict) else None
        fd, sd, ks = j.get("financialData") or {}, j.get("summaryDetail") or {}, j.get("defaultKeyStatistics") or {}
        out[sym] = {"roe": g(fd, "returnOnEquity"), "pe": g(sd, "trailingPE"), "pb": g(ks, "priceToBook"),
                    "earn_growth": g(fd, "earningsGrowth"), "rev_growth": g(fd, "revenueGrowth"),
                    "der": g(fd, "debtToEquity"), "div_yield": g(sd, "dividendYield"), "margin": g(fd, "profitMargins")}
        time.sleep(0.3)
    return out


def completed_bars(d: Daily, now=None):
    """Jumlah candle yang sudah selesai: candle hari ini dianggap selesai setelah 16:15 WIB."""
    now = (now or datetime.now(WIB)).astimezone(WIB)
    today = now.strftime("%Y-%m-%d")
    n = len(d.dates)
    if n and d.dates[-1] == today and now.hour * 60 + now.minute < 16 * 60 + 15:
        n -= 1
    return n


# ---------------------------------------------------------------- strategi
class Ind:
    """Indikator yang dipakai strategi; indeks i hanya memakai data sampai i."""

    def __init__(self, d: Daily, n=None):
        n = n or len(d.c)
        self.o, self.h, self.l, self.c, self.v = d.o[:n], d.h[:n], d.l[:n], d.c[:n], d.v[:n]
        self.e20, self.e50, self.e200 = ind.ema(self.c, 20), ind.ema(self.c, 50), ind.ema(self.c, 200)
        self.rsi = ind.rsi(self.c, 14)
        self.atr = ind.atr(self.h, self.l, self.c, 14)
        self.rvol = ind.rvol(self.v, 20)
        # tertinggi/terendah 20/10 hari SEBELUM hari i
        self.hh20 = np.array([self.h[max(0, i - 20):i].max() if i >= 20 else np.nan for i in range(n)])
        self.ll10 = np.array([self.l[max(0, i - 10):i].min() if i >= 10 else np.nan for i in range(n)])


@dataclass
class Strategy:
    name: str
    desc: str
    entry: Callable        # (Ind, i) -> bool
    exit: Callable         # (Ind, i, bars_held) -> str | None
    stop_atr: float        # stop awal = entry - stop_atr * ATR
    trail_atr: float       # trailing stop dari harga tertinggi sejak masuk (0 = tidak ada)
    regime: bool = False   # True = hanya beli saat IHSG di atas EMA50 (pasar sedang naik)
    fund: bool = False     # True = hanya beli saham yang lolos filter kualitas fundamental (hanya forward test)


def ihsg_up(bench, n=None):
    """Per tanggal: True bila close IHSG > EMA50 IHSG pada hari itu (hanya memakai data sampai hari itu)."""
    n = n or len(bench.c)
    e = ind.ema(bench.c[:n], 50)
    return {dt: bool(bench.c[k] > e[k]) for k, dt in enumerate(bench.dates[:n]) if not np.isnan(e[k])}


def regime_on(up, day):
    """Status IHSG pada `day`, atau hari bursa terakhir sebelumnya bila tanggal itu tidak ada di data IHSG."""
    if day in up:
        return up[day]
    prev = [d for d in up if d < day]
    return up[max(prev)] if prev else False


def quality_ok(f):
    """Filter kualitas sederhana. f = baris tabel fundamentals. Kembalikan (lolos, alasan)."""
    if not f or f["roe"] is None:
        return False, "data fundamental tidak ada"
    if f["roe"] < 0.10:
        return False, f"ROE {f['roe']:.0%} < 10%"
    if f["pe"] is None or not 0 < f["pe"] <= 25:
        return False, "PER rugi/tidak ada" if not f["pe"] or f["pe"] <= 0 else f"PER {f['pe']:.1f} > 25"
    if f["earn_growth"] is not None and f["earn_growth"] < 0:
        return False, f"laba turun {f['earn_growth']:.0%}"
    if f["der"] is not None and f["der"] > 150:        # bank tidak punya DER di Yahoo (None) dan tidak dinilai di sini
        return False, f"utang/ekuitas {f['der']:.0f}% > 150%"
    return True, f"ROE {f['roe']:.0%}, PER {f['pe']:.1f}"


def _ok(*xs):
    return all(not np.isnan(x) for x in xs)


def _trend_entry(d, i):
    return _ok(d.e200[i], d.e50[i - 1]) and d.c[i] > d.e200[i] and d.e20[i - 1] <= d.e50[i - 1] and d.e20[i] > d.e50[i]


def _trend_exit(d, i, held):
    return "EMA20 turun di bawah EMA50" if d.e20[i] < d.e50[i] else None


def _breakout_entry(d, i):
    return _ok(d.hh20[i], d.e200[i], d.rvol[i]) and d.c[i] > d.hh20[i] and d.c[i] > d.e200[i] and d.rvol[i] >= 1.5


def _breakout_exit(d, i, held):
    return "tembus low 10 hari" if _ok(d.ll10[i]) and d.c[i] < d.ll10[i] else None


def _dip_entry(d, i):
    return _ok(d.e200[i], d.rsi[i]) and d.c[i] > d.e200[i] and d.rsi[i] < 35


def _dip_exit(d, i, held):
    if d.rsi[i] > 55:
        return "RSI pulih > 55"
    return "10 hari tidak pulih" if held >= 10 else None


STRATEGIES = [
    Strategy("trend_ema", "EMA20 memotong ke atas EMA50 saat harga di atas EMA200", _trend_entry, _trend_exit, 2.5, 3.0),
    Strategy("breakout_20", "tutup di atas high 20 hari + volume >= 1,5x rata-rata, di atas EMA200",
             _breakout_entry, _breakout_exit, 2.0, 3.0),
    Strategy("rsi_dip", "RSI14 < 35 dalam uptrend (di atas EMA200), jual saat RSI > 55", _dip_entry, _dip_exit, 2.5, 0),
]
# varian dengan filter IHSG (bisa di-backtest) dan filter kualitas fundamental (hanya forward test: data fundamental
# gratis hanya angka hari ini, jadi memakainya di backtest berarti mengintip masa depan)
STRATEGIES += [Strategy(s.name + "_ihsg", s.desc + " · hanya saat IHSG > EMA50", s.entry, s.exit, s.stop_atr,
                        s.trail_atr, regime=True) for s in STRATEGIES[:3]]
STRATEGIES += [Strategy(s.name + "_fund", s.desc + " · hanya saham lolos filter fundamental", s.entry, s.exit,
                        s.stop_atr, s.trail_atr, regime=True, fund=True) for s in STRATEGIES[3:6]]
BY_NAME = {s.name: s for s in STRATEGIES}


def lots_for(alloc, price, fee):
    return int(alloc // (price * LOT * (1 + fee)))


# ---------------------------------------------------------------- backtest (portofolio, semua saham sekaligus)
def backtest(s, data, bench=None):
    """data = {symbol: Daily}. Sinyal di close hari i, eksekusi open hari i+1. Kembalikan dict per strategi."""
    days = sorted({dt for d in data.values() for dt in d.dates})
    idx = {sym: {dt: k for k, dt in enumerate(d.dates)} for sym, d in data.items()}
    inds = {sym: Ind(d) for sym, d in data.items()}
    up = ihsg_up(bench) if bench is not None else {}
    out = {}
    for st in STRATEGIES:
        if st.fund:
            continue
        cash, pos, trades, curve = s.budget_idr, {}, [], []
        pending = []                     # (symbol, kind, reason) dieksekusi di open hari ini
        for day in days:
            # 1) eksekusi sinyal kemarin di harga open
            todo, pending = pending, []
            for sym, kind, why in todo:
                k = idx[sym].get(day)
                if k is None:
                    continue
                d = data[sym]
                lo, hi = price_band(d.c[k - 1], s.arb_pct)
                if kind == "SELL" and sym in pos and d.h[k] <= lo:
                    pending.append((sym, kind, why))      # terkunci ARB sepanjang hari: coba lagi besok
                    continue
                if kind == "BUY" and d.o[k] >= hi:
                    continue                              # dibuka di ARA: antrean beli tidak terisi
                if kind == "SELL" and sym in pos:
                    p = pos.pop(sym)
                    px = round_tick(d.o[k] - tick_size(d.o[k]) * s.slip_ticks)
                    val = px * p["lots"] * LOT
                    cash += val * (1 - s.fee_sell)
                    trades.append(val * (1 - s.fee_sell) - p["cost"])
                elif kind == "BUY" and sym not in pos and len(pos) < s.max_positions:
                    px = round_tick(d.o[k] + tick_size(d.o[k]) * s.slip_ticks, up=True)
                    equity = cash + sum(pp["lots"] * LOT * data[q].c[idx[q][pp["last_day"]]] for q, pp in pos.items())
                    lots = lots_for(min(equity / s.max_positions, cash), px, s.fee_buy)
                    if lots < 1:
                        continue
                    cost = px * lots * LOT * (1 + s.fee_buy)
                    cash -= cost
                    a = inds[sym].atr[k - 1]
                    pos[sym] = {"lots": lots, "cost": cost, "entry": px, "stop": px - st.stop_atr * a,
                                "peak": px, "held": 0, "last_day": day}
            # 2) stop loss intraday, lalu sinyal di close
            for sym, p in list(pos.items()):
                k = idx[sym].get(day)
                if k is None:
                    continue
                d, di = data[sym], inds[sym]
                p["last_day"] = day
                if d.l[k] <= p["stop"] and d.h[k] > price_band(d.c[k - 1], s.arb_pct)[0]:
                    px = round_tick(min(d.o[k], p["stop"]))
                    val = px * p["lots"] * LOT
                    cash += val * (1 - s.fee_sell)
                    trades.append(val * (1 - s.fee_sell) - p["cost"])
                    del pos[sym]
                    continue
                p["held"] += 1
                p["peak"] = max(p["peak"], d.c[k])
                if st.trail_atr and _ok(di.atr[k]):
                    p["stop"] = max(p["stop"], p["peak"] - st.trail_atr * di.atr[k])
                if st.exit(di, k, p["held"]) and not any(x[0] == sym for x in pending):
                    pending.append((sym, "SELL", ""))
            for sym, d in data.items():
                k = idx[sym].get(day)
                if k is None or k < WARMUP or sym in pos or k + 1 >= len(d.c):
                    continue
                if st.entry(inds[sym], k) and (not st.regime or regime_on(up, day)):
                    pending.append((sym, "BUY", ""))
            eq = cash + sum(p["lots"] * LOT * data[q].c[idx[q][p["last_day"]]] for q, p in pos.items())
            curve.append(eq)
        curve = np.array(curve)
        start_i = next((k for k, x in enumerate(curve) if x != s.budget_idr), 0)
        peak = np.maximum.accumulate(curve)
        wins = [t for t in trades if t > 0]
        loss = -sum(t for t in trades if t <= 0)
        bench_pct = None
        if bench is not None and start_i < len(days):
            bi = {dt: k for k, dt in enumerate(bench.dates)}
            b0 = next((bench.c[bi[dt]] for dt in days[start_i:] if dt in bi), None)
            bench_pct = (bench.c[-1] / b0 - 1) * 100 if b0 else None
        out[st.name] = {"trades": len(trades), "wins": len(wins), "ret_pct": (curve[-1] / s.budget_idr - 1) * 100,
                        "max_dd": float(((curve - peak) / peak).min() * 100), "pf": sum(wins) / loss if loss else None,
                        "bench_pct": bench_pct, "start": days[start_i] if days else "", "end": days[-1] if days else ""}
    return out


# ---------------------------------------------------------------- live (paper)
class SahamSim:
    def __init__(self, settings=None):
        self.s = settings or load_saham_settings()
        self.conn = saham_connect()
        self.symbols = [x.strip().upper() for x in self.s.watchlist.split(",") if x.strip()]
        self.client = httpx.Client()
        self.brokers = {st.name: get_broker(self.conn, st.name, self.s) for st in STRATEGIES}
        self.brokers[saham_ipo.BOOK] = get_broker(self.conn, saham_ipo.BOOK, self.s)
        get_broker(self.conn, "manual", self.s)
        self.ihsg_ok = False
        self.last_equity = 0.0

    def fetch_all(self, rng="2y", extra=()):
        data = {}
        for sym in list(self.symbols) + [x for x in extra if x not in self.symbols]:
            try:
                data[sym] = fetch_daily(self.client, sym, rng)
            except Exception as e:
                log(f"gagal ambil {sym}: {type(e).__name__}: {e}")
            time.sleep(0.3)
        now = time.time()
        for sym, d in data.items():
            self.conn.execute("INSERT OR REPLACE INTO quotes VALUES (?,?,?,?,?)", (sym, d.last, d.prev_close, now, d.name))
        self.conn.commit()
        return data

    def was_acted(self, book, sym, bar_date, kind):
        return self.conn.execute("SELECT 1 FROM signals WHERE book=? AND symbol=? AND bar_date=? AND kind=?",
                                 (book, sym, bar_date, kind)).fetchone() is not None

    def mark_acted(self, book, sym, bar_date, kind):
        self.conn.execute("INSERT OR IGNORE INTO signals VALUES (?,?,?,?,?)", (book, sym, bar_date, kind, time.time()))
        self.conn.commit()

    def refresh_fundamentals(self, force=False):
        last = self.conn.execute("SELECT MIN(ts) FROM fundamentals").fetchone()[0] or 0
        have = self.conn.execute("SELECT COUNT(*) FROM fundamentals").fetchone()[0]
        if not force and have >= len(self.symbols) * 0.8 and time.time() - last < FUND_EVERY:
            return
        try:
            rows = fetch_fundamentals(self.client, self.symbols)
        except Exception as e:
            log(f"gagal ambil fundamental: {type(e).__name__}: {e}")
            return
        now = time.time()
        for sym, f in rows.items():
            self.conn.execute("INSERT OR REPLACE INTO fundamentals VALUES (?,?,?,?,?,?,?,?,?,?)",
                              (sym, now, f["roe"], f["pe"], f["pb"], f["earn_growth"], f["rev_growth"], f["der"],
                               f["div_yield"], f["margin"]))
        self.conn.commit()
        log(f"fundamental diperbarui untuk {len(rows)} saham")

    def step(self):
        saham_ipo.scan(self.conn, self.client, log)
        saham_ipo.check_listing(self.conn, self.client, fetch_daily, log)
        self.refresh_fundamentals()
        data = self.fetch_all(extra=saham_ipo.active_symbols(self.conn))
        is_open = market_open()
        try:
            bench = fetch_daily(self.client, BENCH, "1y")
            up = ihsg_up(bench, completed_bars(bench))
            self.ihsg_ok = bool(up) and up[max(up)]
            self.conn.execute("INSERT OR REPLACE INTO quotes VALUES (?,?,?,?,?)",
                              ("IHSG", bench.last, bench.prev_close, time.time(), "naik" if self.ihsg_ok else "turun"))
            self.conn.commit()
        except Exception as e:
            log(f"gagal ambil IHSG: {e}; strategi _ihsg tidak membeli dulu")
            self.ihsg_ok = False
        funds = {r["symbol"]: dict(r) for r in self.conn.execute("SELECT * FROM fundamentals")}
        for st in STRATEGIES:
            br = self.brokers[st.name]
            for sym, p in br.positions().items():
                d = data.get(sym)
                if not d:
                    continue
                n = completed_bars(d)
                di = Ind(d, n)
                k = n - 1
                peak = max(p["peak"] or 0, d.last)
                stop = p["stop"] or (p["avg_price"] - st.stop_atr * di.atr[k])
                if st.trail_atr and _ok(di.atr[k]):
                    stop = max(stop, peak - st.trail_atr * di.atr[k])
                held = sum(1 for dt in d.dates[:n] if dt > datetime.fromtimestamp(p["opened"], WIB).strftime("%Y-%m-%d"))
                self.conn.execute("UPDATE positions SET stop=?, peak=?, bars=? WHERE book=? AND symbol=?",
                                  (stop, peak, held, st.name, sym))
                self.conn.commit()
                if not is_open:
                    continue
                why = "stop loss" if d.last <= stop else st.exit(di, k, held)
                if not why or (why != "stop loss" and self.was_acted(st.name, sym, d.dates[k], "SELL")):
                    continue
                f = br.place(Order(sym, "SELL", p["lots"], reason=why), {"last": d.last, "prev_close": d.prev_close})
                if f.ok:
                    self.mark_acted(st.name, sym, d.dates[k], "SELL")
                log(f"[{st.name}] {f.msg}  ({why})  PnL {fmt_idr(f.pnl)}")
            if not is_open or (st.regime and not self.ihsg_ok):
                continue
            held_syms = br.positions()
            for sym in self.symbols:
                d = data.get(sym)
                if not d or sym in held_syms or len(held_syms) >= self.s.max_positions:
                    continue
                n = completed_bars(d)
                if n <= WARMUP:
                    continue
                di = Ind(d, n)
                k = n - 1
                if not st.entry(di, k) or self.was_acted(st.name, sym, d.dates[k], "BUY"):
                    continue
                if st.fund:
                    ok, why = quality_ok(funds.get(sym))
                    if not ok:
                        self.mark_acted(st.name, sym, d.dates[k], "BUY")
                        log(f"[{st.name}] sinyal {sym} ditolak filter fundamental: {why}")
                        continue
                equity = self.equity(st.name, data)
                lots = lots_for(min(equity / self.s.max_positions, br.cash()), d.last, self.s.fee_buy)
                if lots < 1:
                    self.mark_acted(st.name, sym, d.dates[k], "BUY")
                    log(f"[{st.name}] sinyal {sym} dilewati: modal tidak cukup untuk 1 lot")
                    continue
                f = br.place(Order(sym, "BUY", lots, reason=st.desc), {"last": d.last, "prev_close": d.prev_close})
                if f.ok:      # order ditolak (mis. terkunci ARA) dicoba lagi di putaran berikutnya
                    self.mark_acted(st.name, sym, d.dates[k], "BUY")
                    self.conn.execute("UPDATE positions SET stop=? WHERE book=? AND symbol=?",
                                      (f.price - st.stop_atr * di.atr[k], st.name, sym))
                    self.conn.commit()
                    held_syms = br.positions()
                log(f"[{st.name}] {f.msg}")
        saham_ipo.manage(self.conn, self.brokers[saham_ipo.BOOK], data, is_open, self.s, log)
        self.snapshot(data)
        return is_open

    def equity(self, book, data=None):
        cash = self.conn.execute("SELECT cash FROM accounts WHERE book=?", (book,)).fetchone()[0]
        val = 0.0
        for r in self.conn.execute("SELECT p.lots, p.avg_price, q.last FROM positions p LEFT JOIN quotes q"
                                   " ON q.symbol=p.symbol WHERE p.book=?", (book,)):
            val += r["lots"] * LOT * (r["last"] or r["avg_price"])
        return cash + val

    def snapshot(self, data=None):
        now = time.time()
        if now - self.last_equity < EQUITY_EVERY:
            return
        self.last_equity = now
        for (book,) in self.conn.execute("SELECT book FROM accounts").fetchall():
            self.conn.execute("INSERT INTO equity VALUES (?,?,?)", (now, book, self.equity(book)))
        self.conn.commit()

    def run_backtest(self):
        log(f"mengambil data 5 tahun untuk {len(self.symbols)} saham + IHSG ...")
        data = self.fetch_all("5y")
        bench = fetch_daily(self.client, BENCH, "5y")
        res = backtest(self.s, data, bench)
        now = time.time()
        for name, r in res.items():
            self.conn.execute("INSERT INTO backtests VALUES (?,?,?,?,?,?,?,?,?,?)",
                              (now, name, r["start"], r["end"], r["trades"], r["wins"], r["ret_pct"], r["max_dd"],
                               r["pf"], r["bench_pct"]))
        self.conn.commit()
        print_backtest(res, self.s)
        return res

    def loop(self):
        log(f"Simulator saham BEI (PAPER) - {len(self.symbols)} saham, {len(STRATEGIES)} strategi, "
            f"modal {fmt_idr(self.s.budget_idr)}/strategi")
        while True:
            try:
                is_open = self.step()
            except Exception as e:
                log(f"error putaran: {type(e).__name__}: {e}")
                is_open = False
            time.sleep(self.s.poll_sec if is_open else 1800)


def print_backtest(res, s):
    print(f"\nBacktest {next(iter(res.values()))['start']} s/d {next(iter(res.values()))['end']}  "
          f"(modal {fmt_idr(s.budget_idr)}, maks {s.max_positions} saham, fee beli {s.fee_buy:.2%} / jual {s.fee_sell:.2%})")
    print(f"{'strategi':<18} {'trade':>5} {'win%':>6} {'return':>8} {'maxDD':>7} {'PF':>5} {'IHSG':>7}")
    for name, r in res.items():
        wr = r["wins"] / r["trades"] * 100 if r["trades"] else 0
        pf = f"{r['pf']:.2f}" if r["pf"] is not None else "-"
        b = f"{r['bench_pct']:+.1f}%" if r["bench_pct"] is not None else "-"
        print(f"{name:<18} {r['trades']:>5} {wr:>5.0f}% {r['ret_pct']:>+7.1f}% {r['max_dd']:>6.1f}% {pf:>5} {b:>7}")
    print("Catatan: ini data yang sama dengan yang dipakai merancang strategi (in-sample) dan sampel trade kecil;"
          " jangan dianggap bukti strategi akan untung.\n")


def status_text(conn=None):
    conn = conn or saham_connect()
    lines = []
    for a in conn.execute("SELECT * FROM accounts ORDER BY book"):
        pos = conn.execute("SELECT p.symbol, p.lots, p.avg_price, q.last FROM positions p LEFT JOIN quotes q"
                           " ON q.symbol=p.symbol WHERE p.book=?", (a["book"],)).fetchall()
        val = sum(r["lots"] * LOT * (r["last"] or r["avg_price"]) for r in pos)
        eq = a["cash"] + val
        lines.append(f"{a['book']:<12} equity {fmt_idr(eq):>16} ({(eq / a['start'] - 1) * 100:+.2f}%)  kas {fmt_idr(a['cash'])}")
        for r in pos:
            last = r["last"] or r["avg_price"]
            lines.append(f"    {r['symbol']:<5} {r['lots']:>4} lot  avg {r['avg_price']:,.0f}  last {last:,.0f}"
                         f"  ({(last / r['avg_price'] - 1) * 100:+.1f}%)")
    return "\n".join(lines) or "Belum ada data. Jalankan: python saham.py --once"


def manual_order(args, s=None):
    s = s or load_saham_settings()
    if len(args) < 3:
        return "Format: python saham.py buy|sell KODE LOT [LIMIT]"
    side, sym, lots = args[0].upper(), args[1].upper(), int(args[2])
    side = {"BUY": "BUY", "BELI": "BUY", "SELL": "SELL", "JUAL": "SELL"}.get(side, side)
    limit = float(args[3]) if len(args) > 3 else 0.0
    if not (market_open() or s.manual_anytime):
        return "Bursa sedang tutup (jam bursa WIB). Set SAHAM_MANUAL_ANYTIME=1 di .env untuk mencoba di luar jam bursa."
    conn = saham_connect()
    with httpx.Client() as c:
        d = fetch_daily(c, sym, "5d")
    conn.execute("INSERT OR REPLACE INTO quotes VALUES (?,?,?,?,?)", (sym, d.last, d.prev_close, time.time(), d.name))
    br = get_broker(conn, "manual", s)
    return br.place(Order(sym, side, lots, limit, "manual"), {"last": d.last, "prev_close": d.prev_close}).msg


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    a = sys.argv[1:]
    if a and a[0].lower() in ("buy", "sell", "beli", "jual"):
        print(manual_order(a))
    elif a and a[0] == "status":
        print(status_text())
    elif "--backtest" in a:
        SahamSim().run_backtest()
    elif "--once" in a:
        sim = SahamSim()
        log(f"bursa {'BUKA' if sim.step() else 'TUTUP'}")
        print(status_text(sim.conn))
    else:
        SahamSim().loop()


if __name__ == "__main__":
    main()
