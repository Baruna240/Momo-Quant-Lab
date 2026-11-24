"""Aturan bursa saham Indonesia (BEI/IDX) + lapisan broker untuk paper trading saham.

Semua order dari strategi maupun manual lewat kelas Broker. Saat ini yang aktif HANYA PaperBroker (uang simulasi
di saham.db). Nanti, setelah strategi lolos uji, LiveBroker bisa diisi dengan API broker sungguhan (lihat
docs/saham_live.md) tanpa mengubah strategi: cukup ganti broker yang dipakai.

Aturan BEI yang ditiru:
- 1 lot = 100 lembar; order selalu dalam lot.
- Fraksi harga (tick): <200 = 1, 200-<500 = 2, 500-<2.000 = 5, 2.000-<5.000 = 10, >=5.000 = 25 rupiah.
- Auto rejection (ARA/ARB) dari harga penutupan kemarin: 50-200 = 35%, 200-5.000 = 25%, >5.000 = 20%.
  Batas bawah (ARB) bisa diubah lewat SAHAM_ARB_PCT kalau BEI mengubah aturannya (0 = simetris dengan ARA).
- Fee broker: beli 0,15%, jual 0,25% (sudah termasuk PPh final 0,1%). Bisa diubah di .env.
- Jam bursa (WIB): Senin-Kamis 09:00-12:00 & 13:30-15:50; Jumat 09:00-11:30 & 14:00-15:50. Libur bursa tidak dicek
  (pada hari libur harga dari Yahoo tidak bergerak, jadi tidak ada sinyal baru).
"""
import math
import os
import sqlite3
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone

from config import BASE_DIR, load_env

SAHAM_DB = BASE_DIR / "saham.db"
LOT = 100
WIB = timezone(timedelta(hours=7))


@dataclass
class SahamSettings:
    budget_idr: float = 500_000_000.0  # modal simulasi per buku (tiap strategi + buku manual)
    max_positions: int = 5             # saham bersamaan per strategi (modal dibagi rata)
    watchlist: str = ("BBCA,BBRI,BMRI,BBNI,TLKM,ASII,UNVR,ICBP,INDF,KLBF,ANTM,MDKA,ADRO,PTBA,ITMG,UNTR,"
                      "CPIN,AMRT,BRPT,MEDC,INCO,PGAS,SMGR,EXCL,ISAT,AKRA,GOTO,BBTN,JPFA,TPIA")
    fee_buy: float = 0.0015
    fee_sell: float = 0.0025
    slip_ticks: int = 1                # order market terisi 1 tick lebih buruk dari harga terakhir
    arb_pct: float = 0.0               # 0 = ARB simetris dengan ARA
    poll_sec: float = 300.0            # cek harga tiap 5 menit saat bursa buka
    manual_anytime: bool = False       # True = order manual boleh di luar jam bursa (hanya untuk mencoba)
    broker: str = "paper"              # paper | live (live BELUM tersedia, lihat docs/saham_live.md)


def load_saham_settings():
    load_env()
    s = SahamSettings()
    for f in fields(s):
        raw = os.environ.get("SAHAM_" + f.name.upper())
        if raw is None or raw == "":
            continue
        kind = type(getattr(s, f.name))
        setattr(s, f.name, raw.lower() in ("1", "true", "yes", "ya") if kind is bool else kind(raw))
    return s


# ---------------------------------------------------------------- aturan harga BEI
def tick_size(price):
    if price < 200:
        return 1
    if price < 500:
        return 2
    if price < 2000:
        return 5
    if price < 5000:
        return 10
    return 25


def round_tick(price, up=False):
    """Bulatkan ke fraksi harga yang sah (ke bawah, atau ke atas bila up=True)."""
    t = tick_size(price)
    f = math.ceil if up else math.floor
    p = f(price / t - 1e-9 if up else price / t + 1e-9) * t
    return max(1, int(p))


def ara_pct(prev_close):
    if prev_close <= 200:
        return 0.35
    if prev_close <= 5000:
        return 0.25
    return 0.20


def price_band(prev_close, arb_pct=0.0):
    """(batas bawah ARB, batas atas ARA) dari harga penutupan kemarin."""
    up = ara_pct(prev_close)
    dn = arb_pct or up
    return round_tick(prev_close * (1 - dn), up=True), round_tick(prev_close * (1 + up))


def market_open(now=None):
    now = (now or datetime.now(WIB)).astimezone(WIB)
    wd = now.weekday()
    if wd >= 5:
        return False
    hm = now.hour * 60 + now.minute
    s1_end = 11 * 60 + 30 if wd == 4 else 12 * 60
    s2_start = 14 * 60 if wd == 4 else 13 * 60 + 30
    return 9 * 60 <= hm < s1_end or s2_start <= hm < 15 * 60 + 50


def next_open(now=None):
    """Waktu (datetime WIB) sesi bursa berikutnya dimulai; hari libur bursa tidak diperhitungkan."""
    now = (now or datetime.now(WIB)).astimezone(WIB).replace(second=0, microsecond=0)
    for add in range(0, 8):
        day = (now + timedelta(days=add)).replace(hour=0, minute=0)
        if day.weekday() >= 5:
            continue
        s2 = (14, 0) if day.weekday() == 4 else (13, 30)
        for h, m in ((9, 0), s2):
            t = day.replace(hour=h, minute=m)
            if t > now:
                return t
    return now


def fmt_idr(x):
    return f"Rp{x:,.0f}".replace(",", ".")


# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (book TEXT PRIMARY KEY, cash REAL, start REAL, created REAL);
CREATE TABLE IF NOT EXISTS positions (book TEXT, symbol TEXT, lots INT, avg_price REAL, cost REAL, opened REAL,
    stop REAL DEFAULT 0, peak REAL DEFAULT 0, bars INT DEFAULT 0, PRIMARY KEY (book, symbol));
CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, book TEXT, symbol TEXT, side TEXT,
    lots INT, price REAL, value REAL, fee REAL, pnl REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS equity (ts REAL, book TEXT, equity REAL);
CREATE TABLE IF NOT EXISTS quotes (symbol TEXT PRIMARY KEY, last REAL, prev_close REAL, ts REAL, name TEXT);
CREATE TABLE IF NOT EXISTS signals (book TEXT, symbol TEXT, bar_date TEXT, kind TEXT, ts REAL,
    PRIMARY KEY (book, symbol, bar_date, kind));
CREATE TABLE IF NOT EXISTS backtests (ts REAL, strategy TEXT, start TEXT, end TEXT, trades INT, wins INT,
    ret_pct REAL, max_dd REAL, pf REAL, bench_pct REAL);
CREATE TABLE IF NOT EXISTS fundamentals (symbol TEXT PRIMARY KEY, ts REAL, roe REAL, pe REAL, pb REAL,
    earn_growth REAL, rev_growth REAL, der REAL, div_yield REAL, margin REAL);
CREATE TABLE IF NOT EXISTS ipo (symbol TEXT PRIMARY KEY, title TEXT, url TEXT, source TEXT, first_seen REAL,
    status TEXT DEFAULT 'pipeline', listing_date TEXT, checked REAL DEFAULT 0, note TEXT);
CREATE TABLE IF NOT EXISTS agents (name TEXT PRIMARY KEY, status TEXT, detail TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS agent_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, agent TEXT, msg TEXT);
CREATE INDEX IF NOT EXISTS trades_book ON trades(book, ts);
CREATE INDEX IF NOT EXISTS equity_book ON equity(book, ts);
"""


def saham_connect(path=SAHAM_DB):
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------- broker
@dataclass
class Order:
    symbol: str
    side: str            # "BUY" | "SELL"
    lots: int
    limit: float = 0.0   # 0 = market (isi di harga terakhir +/- slippage)
    reason: str = ""


@dataclass
class Fill:
    ok: bool
    msg: str
    price: float = 0.0
    lots: int = 0
    fee: float = 0.0
    pnl: float = 0.0


class OrderRejected(Exception):
    pass


class Broker(ABC):
    """Antarmuka yang sama untuk simulasi dan (nanti) broker sungguhan."""
    live = False

    @abstractmethod
    def cash(self): ...

    @abstractmethod
    def positions(self): ...

    @abstractmethod
    def place(self, order: Order, quote: dict) -> Fill: ...


class PaperBroker(Broker):
    """Eksekusi simulasi: tidak ada uang sungguhan. quote = {"last": .., "prev_close": ..}."""

    def __init__(self, conn, book, settings: SahamSettings):
        self.conn, self.book, self.s = conn, book, settings
        if not conn.execute("SELECT 1 FROM accounts WHERE book=?", (book,)).fetchone():
            conn.execute("INSERT INTO accounts VALUES (?,?,?,?)", (book, settings.budget_idr, settings.budget_idr, time.time()))
            conn.commit()

    def cash(self):
        return self.conn.execute("SELECT cash FROM accounts WHERE book=?", (self.book,)).fetchone()[0]

    def positions(self):
        return {r["symbol"]: dict(r) for r in self.conn.execute("SELECT * FROM positions WHERE book=?", (self.book,))}

    def _fill_price(self, side, last, limit):
        t = tick_size(last) * self.s.slip_ticks
        px = last + t if side == "BUY" else last - t
        px = round_tick(px, up=side == "BUY")
        if limit:
            if side == "BUY" and px > limit:
                raise OrderRejected(f"harga {px:,.0f} di atas limit {limit:,.0f}")
            if side == "SELL" and px < limit:
                raise OrderRejected(f"harga {px:,.0f} di bawah limit {limit:,.0f}")
        return px

    def place(self, order: Order, quote: dict) -> Fill:
        try:
            return self._place(order, quote)
        except OrderRejected as e:
            return Fill(False, f"DITOLAK {order.side} {order.symbol}: {e}")

    def _place(self, o: Order, q: dict) -> Fill:
        if o.side not in ("BUY", "SELL"):
            raise OrderRejected("side harus BUY atau SELL")
        if int(o.lots) != o.lots or o.lots < 1:
            raise OrderRejected("jumlah harus minimal 1 lot (1 lot = 100 lembar)")
        last, prev = float(q.get("last") or 0), float(q.get("prev_close") or q.get("last") or 0)
        if last <= 0:
            raise OrderRejected("harga belum tersedia")
        if o.limit and o.limit != round_tick(o.limit):
            raise OrderRejected(f"limit {o.limit:,.0f} tidak sesuai fraksi harga (kelipatan {tick_size(o.limit)})")
        lo, hi = price_band(prev, self.s.arb_pct)
        # saham yang terkunci di ARA hanya ada antrean beli (tidak ada penjual), di ARB sebaliknya: order tidak terisi
        if o.side == "BUY" and last >= hi:
            raise OrderRejected(f"terkunci ARA ({hi:,.0f}): antre beli, tidak ada penjual")
        if o.side == "SELL" and last <= lo:
            raise OrderRejected(f"terkunci ARB ({lo:,.0f}): antre jual, tidak ada pembeli")
        px = self._fill_price(o.side, last, o.limit)
        px = min(max(px, lo), hi)          # di BEI harga tidak bisa melewati ARA/ARB
        shares = o.lots * LOT
        value = px * shares
        now = time.time()
        pos = self.positions().get(o.symbol)
        if o.side == "BUY":
            fee = value * self.s.fee_buy
            if value + fee > self.cash() + 1e-6:
                raise OrderRejected(f"kas kurang: butuh {fmt_idr(value + fee)}, ada {fmt_idr(self.cash())}")
            if pos:
                lots = pos["lots"] + o.lots
                cost = pos["cost"] + value + fee
                self.conn.execute("UPDATE positions SET lots=?, cost=?, avg_price=? WHERE book=? AND symbol=?",
                                  (lots, cost, cost / (lots * LOT), self.book, o.symbol))
            else:
                self.conn.execute("INSERT INTO positions (book, symbol, lots, avg_price, cost, opened, peak) VALUES (?,?,?,?,?,?,?)",
                                  (self.book, o.symbol, o.lots, (value + fee) / shares, value + fee, now, px))
            self.conn.execute("UPDATE accounts SET cash = cash - ? WHERE book=?", (value + fee, self.book))
            pnl = 0.0
        else:
            if not pos or pos["lots"] < o.lots:
                raise OrderRejected(f"tidak punya {o.lots} lot {o.symbol} (dimiliki {pos['lots'] if pos else 0})")
            fee = value * self.s.fee_sell
            part_cost = pos["cost"] * o.lots / pos["lots"]
            pnl = value - fee - part_cost
            if pos["lots"] == o.lots:
                self.conn.execute("DELETE FROM positions WHERE book=? AND symbol=?", (self.book, o.symbol))
            else:
                self.conn.execute("UPDATE positions SET lots=?, cost=? WHERE book=? AND symbol=?",
                                  (pos["lots"] - o.lots, pos["cost"] - part_cost, self.book, o.symbol))
            self.conn.execute("UPDATE accounts SET cash = cash + ? WHERE book=?", (value - fee, self.book))
        self.conn.execute("INSERT INTO trades (ts, book, symbol, side, lots, price, value, fee, pnl, reason)"
                          " VALUES (?,?,?,?,?,?,?,?,?,?)", (now, self.book, o.symbol, o.side, o.lots, px, value, fee, pnl, o.reason))
        self.conn.commit()
        return Fill(True, f"{o.side} {o.lots} lot {o.symbol} @ {px:,.0f}", px, o.lots, fee, pnl)


class LiveBroker(Broker):
    """Tempat koneksi ke broker/sekuritas sungguhan. SENGAJA belum diisi.

    Broker ritel Indonesia umumnya belum membuka API order publik; dompet digital (DANA/OVO/GoPay) tidak bisa
    menyimpan saham karena saham wajib berada di rekening efek + RDN atas nama Anda. Lihat docs/saham_live.md.
    """
    live = True

    def __init__(self, *args, **kwargs):
        raise NotImplementedError("Mode live belum tersedia: isi LiveBroker dengan API resmi broker Anda "
                                  "(lihat docs/saham_live.md). Simulasi tetap memakai SAHAM_BROKER=paper.")

    def cash(self): ...

    def positions(self): ...

    def place(self, order, quote): ...


def get_broker(conn, book, settings: SahamSettings) -> Broker:
    if settings.broker.lower() == "live":
        return LiveBroker(conn, book, settings)
    return PaperBroker(conn, book, settings)
