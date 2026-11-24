"""Deteksi saham yang akan/baru IPO di BEI + strategi entry hari-hari pertama listing (PAPER).

Sumber:
- Google News RSS (publik): judul berita berisi "IPO" + kode saham 4 huruf dalam kurung, mis. "Bumi Atsiri Prima (BUAT)".
  Situs e-IPO dan IDX menolak akses otomatis, jadi tidak dipakai.
- ipo_manual.txt (opsional): satu kode saham per baris, untuk IPO yang tidak terdeteksi berita.

Alur status: pipeline (ada berita, belum listing) -> listed (sudah ada harga di Yahoo) -> entered (sudah dibeli)
             atau terlewat (3 hari pertama tidak bisa dibeli, mis. terkunci ARA) / sudah_listing (ternyata saham lama)
             / kadaluarsa (60 hari tidak listing).

Kenapa beli di pasar setelah listing, bukan ikut e-IPO di harga penawaran: jatah e-IPO ritel ditentukan penjatahan
(sering oversubscribe puluhan kali sehingga hanya dapat beberapa lot) dan hasilnya tidak bisa disimulasikan jujur.
"""
import re
import time
from datetime import datetime
from email.utils import parsedate_to_datetime

from config import BASE_DIR
from saham_broker import LOT, WIB, Order, fmt_idr

NEWS = "https://news.google.com/rss/search"
QUERIES = ["IPO saham BEI when:14d", "bookbuilding IPO saham when:14d", "pencatatan perdana saham BEI when:14d"]
MANUAL = BASE_DIR / "ipo_manual.txt"
SCAN_EVERY = 6 * 3600
CHECK_EVERY = 3 * 3600
ENTRY_DAYS = 3          # boleh membeli selama 3 hari bursa pertama
HOLD_DAYS = 20          # jual paling lambat setelah 20 hari bursa
STOP_PCT = 0.15         # stop loss 15% di bawah harga beli
TRAIL_START = 0.20      # setelah naik 20%, pakai trailing stop
TRAIL_PCT = 0.15        # 15% di bawah harga tertinggi
BOOK = "ipo_listing"
DESC = "beli di 3 hari pertama listing (bukan saat terkunci ARA), stop -15%, trailing 15% setelah +20%, maks 20 hari"
NOT_TICKER = {"IPO", "BEI", "OJK", "IHSG", "LQ45", "IDX", "KSEI", "BUMN"}
TICKER_IN_PARENS = re.compile(r"\(([A-Z]{4})\)")
_ITEM = re.compile(r"<item>.*?<title>(.*?)</title>.*?<link>(.*?)</link>.*?<pubDate>(.*?)</pubDate>", re.S)


def parse_news(xml):
    """Kembalikan [(kode, judul, url, waktu)] dari RSS: hanya judul yang menyebut IPO/bookbuilding/listing."""
    out = []
    for title, url, pub in _ITEM.findall(xml):
        title = title.replace("&amp;", "&")
        low = title.lower()
        if not any(w in low for w in ("ipo", "bookbuilding", "book building", "pencatatan perdana", "melantai")):
            continue
        for code in TICKER_IN_PARENS.findall(title):
            if code not in NOT_TICKER:
                try:
                    ts = parsedate_to_datetime(pub).timestamp()
                except (TypeError, ValueError):
                    ts = time.time()
                out.append((code, title, url, ts))
    return out


def scan(conn, client, log, force=False):
    last = conn.execute("SELECT MAX(first_seen) FROM ipo WHERE source='scan-mark'").fetchone()[0] or 0
    if not force and time.time() - last < SCAN_EVERY:
        return
    found = 0
    for q in QUERIES:
        try:
            r = client.get(NEWS, params={"q": q, "hl": "id", "gl": "ID", "ceid": "ID:id"},
                           headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            r.raise_for_status()
        except Exception as e:
            log(f"[ipo] gagal ambil berita: {type(e).__name__}: {e}")
            continue
        for code, title, url, ts in parse_news(r.text):
            cur = conn.execute("INSERT OR IGNORE INTO ipo (symbol, title, url, source, first_seen) VALUES (?,?,?,?,?)",
                               (code, title, url, "berita", ts))
            if cur.rowcount:
                found += 1
                log(f"[ipo] kandidat baru dari berita: {code}  — {title[:90]}")
    if MANUAL.exists():
        for line in MANUAL.read_text(encoding="utf-8-sig").splitlines():
            code = line.strip().upper()
            if re.fullmatch(r"[A-Z]{4}", code):
                conn.execute("INSERT OR IGNORE INTO ipo (symbol, title, source, first_seen) VALUES (?,?,?,?)",
                             (code, "ditambahkan manual (ipo_manual.txt)", "manual", time.time()))
    # penanda waktu scan terakhir (baris khusus, tidak ditampilkan)
    conn.execute("INSERT OR REPLACE INTO ipo (symbol, source, first_seen, status) VALUES ('_SCAN','scan-mark',?, 'mark')",
                 (time.time(),))
    conn.commit()
    if found:
        log(f"[ipo] {found} kandidat IPO baru")


def check_listing(conn, client, fetch_daily, log):
    """Cek di Yahoo apakah kandidat sudah mulai diperdagangkan."""
    now = time.time()
    for r in conn.execute("SELECT * FROM ipo WHERE status='pipeline' AND checked < ?", (now - CHECK_EVERY,)).fetchall():
        sym = r["symbol"]
        conn.execute("UPDATE ipo SET checked=? WHERE symbol=?", (now, sym))
        try:
            d = fetch_daily(client, sym, "3mo")
        except Exception:
            d = None                                   # belum ada di Yahoo = belum listing
        if d is None or not d.dates:
            if now - r["first_seen"] > 60 * 86400:
                conn.execute("UPDATE ipo SET status='kadaluarsa', note='60 hari tanpa listing' WHERE symbol=?", (sym,))
            continue
        seen = datetime.fromtimestamp(r["first_seen"], WIB).strftime("%Y-%m-%d")
        first = d.dates[0]
        if len(d.dates) >= 55 or first < _minus_days(seen, 7):
            conn.execute("UPDATE ipo SET status='sudah_listing', listing_date=?, note='saham lama, bukan IPO baru'"
                         " WHERE symbol=?", (first, sym))
        else:
            conn.execute("UPDATE ipo SET status='listed', listing_date=? WHERE symbol=?", (first, sym))
            log(f"[ipo] {sym} sudah listing sejak {first} (harga {d.last:,.0f})")
    conn.commit()


def _minus_days(date, n):
    return datetime.fromtimestamp(datetime.strptime(date, "%Y-%m-%d").timestamp() - n * 86400).strftime("%Y-%m-%d")


def active_symbols(conn):
    rows = conn.execute("SELECT symbol FROM ipo WHERE status IN ('listed', 'entered')").fetchall()
    held = conn.execute("SELECT symbol FROM positions WHERE book=?", (BOOK,)).fetchall()
    return sorted({r[0] for r in rows} | {r[0] for r in held})


def manage(conn, broker, data, is_open, s, log):
    """Exit posisi IPO lalu entry kandidat yang baru listing. data = {kode: Daily} (sudah termasuk saham IPO)."""
    today = datetime.now(WIB).strftime("%Y-%m-%d")
    for sym, p in broker.positions().items():
        d = data.get(sym)
        if not d:
            continue
        peak = max(p["peak"] or 0, d.last)
        stop = p["avg_price"] * (1 - STOP_PCT)
        if peak >= p["avg_price"] * (1 + TRAIL_START):
            stop = max(stop, peak * (1 - TRAIL_PCT))
        opened = datetime.fromtimestamp(p["opened"], WIB).strftime("%Y-%m-%d")
        held = sum(1 for dt in d.dates if dt > opened)
        conn.execute("UPDATE positions SET stop=?, peak=?, bars=? WHERE book=? AND symbol=?", (stop, peak, held, BOOK, sym))
        conn.commit()
        if not is_open:
            continue
        why = "stop loss / trailing" if d.last <= stop else (f"{HOLD_DAYS} hari bursa" if held >= HOLD_DAYS else None)
        if why:
            f = broker.place(Order(sym, "SELL", p["lots"], reason=why), {"last": d.last, "prev_close": d.prev_close})
            log(f"[{BOOK}] {f.msg} ({why}) PnL {fmt_idr(f.pnl)}")
    for r in conn.execute("SELECT * FROM ipo WHERE status='listed'").fetchall():
        sym, d = r["symbol"], data.get(r["symbol"])
        if not d:
            continue
        days = sum(1 for dt in d.dates if dt >= r["listing_date"])
        if days > ENTRY_DAYS or (days == ENTRY_DAYS and d.dates[-1] < today):
            conn.execute("UPDATE ipo SET status='terlewat', note='tidak bisa dibeli di 3 hari pertama' WHERE symbol=?", (sym,))
            conn.commit()
            continue
        if not is_open or sym in broker.positions() or len(broker.positions()) >= s.max_positions:
            continue
        if d.dates[-1] == today and d.h[-1] == d.l[-1] == d.last:
            continue        # satu harga sepanjang hari = terkunci (biasanya ARA); antrean beli tidak terisi
        cash = broker.cash()
        equity = cash + sum(x["lots"] * LOT * (data[x["symbol"]].last if x["symbol"] in data else x["avg_price"])
                            for x in broker.positions().values())
        lots = int(min(equity / s.max_positions, cash) // (d.last * LOT * (1 + s.fee_buy)))
        if lots < 1:
            continue
        f = broker.place(Order(sym, "BUY", lots, reason="IPO: " + DESC), {"last": d.last, "prev_close": d.prev_close})
        log(f"[{BOOK}] {f.msg}")
        if f.ok:
            conn.execute("UPDATE ipo SET status='entered' WHERE symbol=?", (sym,))
            conn.commit()
