"""Dashboard web simulator. Jalankan: python dashboard.py  lalu buka http://localhost:8050
Hanya MEMBACA simulasi.db, jadi aman dijalankan bersamaan dengan sim.py."""
import json
import sqlite3
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from config import BASE_DIR, DB_PATH, load_settings
from report import kelly
from strategies import build_variants
from altcoin import ALT_DB, STRATEGIES as ALT_STRATEGIES, load_alt_settings, open_value

SETTINGS = load_settings()  # hanya aturan TP/SL yang dikirim ke browser, bukan isi rahasia .env
VARIANTS = {v.name: v for v in build_variants(SETTINGS)}
ALT_SETTINGS = load_alt_settings()
PORT = 8050
PAGE = BASE_DIR / "web" / "index.html"
PEAK_BUCKETS = [(0, 1, "< 1x"), (1, 2, "1-2x"), (2, 5, "2-5x"), (5, 10, "5-10x"), (10, None, ">= 10x")]
WINDOWS = [("1J", 3600), ("6J", 6 * 3600), ("24J", 24 * 3600), ("SEMUA", None)]


def db():
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def has_table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()


def scope_filter(scope):
    return ("", ()) if scope == "all" else (" AND strategy=?", (scope,))


def metrics(conn, scope, now):
    where, args = scope_filter(scope)
    rows = conn.execute("SELECT cost_usd, proceeds_usd, pnl_usd, ret_mult, closed, graduated,"
                        " COALESCE(kelly_stake, cost_usd) AS kstake FROM positions WHERE 1=1" + where, args).fetchall()
    wins = [r["pnl_usd"] for r in rows if r["pnl_usd"] > 0]
    losses = [r["pnl_usd"] for r in rows if r["pnl_usd"] <= 0]
    cost = sum(r["cost_usd"] for r in rows)
    back = sum(r["proceeds_usd"] for r in rows)
    first = conn.execute("SELECT MIN(ts) FROM trades WHERE 1=1" + where, args).fetchone()[0]
    b = conn.execute("SELECT COUNT(*), COALESCE(SUM(pnl > 0),0) FROM batches WHERE 1=1" + where, args).fetchone()
    opened = open_positions(conn, None if scope == "all" else scope)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    k = kelly(len(rows), len(wins), sum(wins), -sum(losses), SETTINGS)
    return {
        "kelly_status": k["status"], "kelly_f": k["f"],
        "kelly_pnl": sum(r["pnl_usd"] * r["kstake"] / r["cost_usd"] for r in rows),
        "kelly_cost": sum(r["kstake"] for r in rows),
        "kelly_min": SETTINGS.kelly_min_sample,
        "closed": len(rows), "cost": cost, "returned": back, "pnl": back - cost,
        "wins": len(wins), "win_rate": len(wins) / len(rows) if rows else None,
        "avg_win": avg_win, "avg_loss": avg_loss, "rr": (avg_win / -avg_loss) if wins and avg_loss < 0 else None,
        "profit_factor": (sum(wins) / -sum(losses)) if wins and sum(losses) < 0 else None,
        "best": max((r["ret_mult"] for r in rows), default=None),
        "worst": min((r["ret_mult"] for r in rows), default=None),
        "graduated": sum(r["graduated"] for r in rows),
        "open": len(opened), "open_cost": sum(p["cost"] - p["sold_usd"] for p in opened),
        "batches": b[0], "batch_wins": b[1],
        "buys": conn.execute("SELECT COUNT(*) FROM trades WHERE side='BELI'" + where, args).fetchone()[0],
        "days": (now - first) / 86400 if first else 0,
    }


def equity(conn, scope, kelly=False):
    """PnL kumulatif per waktu. kelly=True: seandainya ukuran taruhan mengikuti rem Kelly."""
    where, args = scope_filter(scope)
    cum, points = 0.0, []
    for r in conn.execute("SELECT closed, pnl_usd * COALESCE(kelly_stake, cost_usd) / cost_usd AS kp, pnl_usd"
                          " FROM positions WHERE 1=1" + where + " ORDER BY closed", args):
        cum += r["kp"] if kelly else r["pnl_usd"]
        t = int(r["closed"])
        if points and points[-1][0] == t:
            points[-1][1] = cum
        else:
            points.append([t, cum])
    return points


def summary(conn):
    now = time.time()
    out = {"now": now, "last_update": None, "strategies": [], "variants": {}, "scopes": {}, "equity": {},
           "equity_kelly": {}, "peaks": {},
           "returns": {}, "matrix": {}, "feed": [], "batches": [], "stats": {}}
    if conn is None:
        return out
    if has_table(conn, "stats"):
        out["stats"] = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM stats")}
    if has_table(conn, "auto_log"):
        row = conn.execute("SELECT ts, follow, action, score, n, detail FROM auto_log ORDER BY ts DESC LIMIT 1").fetchone()
        out["auto"] = dict(row) if row else None
        if row and row["follow"] in VARIANTS:
            out["auto"]["follow_label"] = VARIANTS[row["follow"]].label
    if has_table(conn, "token_features"):
        f = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(finalized),0) done, COALESCE(SUM(max_mult>=2),0) x2,"
                         " COALESCE(SUM(max_mult>=5),0) x5, COALESCE(SUM(graduated),0) grad FROM token_features").fetchone()
        out["features"] = dict(f)
    stamps = [conn.execute("SELECT MAX(ts) FROM trades").fetchone()[0], out["stats"].get("heartbeat")]
    out["last_update"] = max(filter(None, stamps), default=None)
    in_db = [r[0] for r in conn.execute("SELECT DISTINCT strategy FROM trades")]
    names = list(VARIANTS) + sorted(n for n in in_db if n not in VARIANTS)
    out["strategies"] = names
    out["variants"] = {n: {"label": v.label, "desc": v.desc, "order": v.order, "mode": v.mode}
                       for n, v in VARIANTS.items()}
    for scope in ["all"] + names:
        where, args = scope_filter(scope)
        out["scopes"][scope] = metrics(conn, scope, now)
        out["equity"][scope] = equity(conn, scope)
        out["equity_kelly"][scope] = equity(conn, scope, kelly=True)
        out["returns"][scope] = [r[0] for r in conn.execute(
            "SELECT ret_mult FROM positions WHERE 1=1" + where + " ORDER BY closed DESC LIMIT 3000", args)]
        out["peaks"][scope] = [
            {"label": label, "count": conn.execute(
                "SELECT COUNT(*) FROM positions WHERE peak_mult>=?" + (" AND peak_mult<?" if hi else "") + where,
                ((lo, hi) if hi else (lo,)) + args).fetchone()[0]}
            for lo, hi, label in PEAK_BUCKETS]
    for name in names:
        out["matrix"][name] = []
        for label, secs in WINDOWS:
            since = now - secs if secs else 0
            n, pnl, wins = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(pnl_usd),0), COALESCE(SUM(pnl_usd>0),0) FROM positions"
                " WHERE strategy=? AND closed>=?", (name, since)).fetchone()
            out["matrix"][name].append({"label": label, "n": n, "pnl": pnl, "win_rate": wins / n if n else None})
    out["feed"] = [dict(r) for r in conn.execute(
        "SELECT t.ts, t.side, t.strategy, t.symbol, t.mint, t.reason, t.usd, t.qty,"
        " t.price_sol / NULLIF(b.price_sol, 0) AS mult"
        " FROM trades t LEFT JOIN trades b ON b.side='BELI' AND b.mint=t.mint AND b.strategy=t.strategy"
        " AND b.batch=t.batch ORDER BY t.ts DESC LIMIT 40")]
    out["batches"] = [dict(r) for r in conn.execute(
        "SELECT strategy, batch, started, ended, invested, returned, pnl FROM batches ORDER BY ended DESC LIMIT 50")]
    # token baru terakhir (sampel data belajar) untuk animasi tahap SCAN
    out["recent_tokens"] = [dict(r) for r in conn.execute(
        "SELECT symbol, created, m30 FROM token_features ORDER BY created DESC LIMIT 12")] \
        if has_table(conn, "token_features") else []
    return out


def open_positions(conn, strategy=None):
    sql = ("SELECT t.strategy, t.batch, t.mint, t.symbol, t.ts opened, t.price_sol entry, t.usd cost,"
           " t.usd / t.sol sol_usd FROM trades t LEFT JOIN positions p"
           " ON p.mint=t.mint AND p.strategy=t.strategy AND p.batch=t.batch"
           " WHERE t.side='BELI' AND p.id IS NULL")
    args = ()
    if strategy:
        sql += " AND t.strategy=?"
        args = (strategy,)
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY t.ts DESC", args)]
    prices = has_table(conn, "prices")
    for r in rows:
        last = peak = None
        if prices:
            last, peak = conn.execute("SELECT (SELECT price FROM prices WHERE mint=? ORDER BY ts DESC LIMIT 1),"
                                      " MAX(price) FROM prices WHERE mint=? AND ts>=?",
                                      (r["mint"], r["mint"], r["opened"])).fetchone()
        r["sold_usd"] = conn.execute("SELECT COALESCE(SUM(usd),0) FROM trades WHERE side='JUAL' AND mint=?"
                                     " AND strategy=? AND batch=?",
                                     (r["mint"], r["strategy"], r["batch"])).fetchone()[0]
        r["mult"] = (last / r["entry"]) if last else None
        r["peak_mult"] = (peak / r["entry"]) if peak else None
    return rows


def positions(conn, status):
    if conn is None:
        return []
    if status == "open":
        return open_positions(conn)
    return [dict(r) for r in conn.execute(
        "SELECT strategy, batch, mint, symbol, opened, closed, cost_usd, proceeds_usd, pnl_usd, ret_mult,"
        " peak_mult, reason, graduated FROM positions ORDER BY closed DESC LIMIT 200")]


def coin(conn, mint, strategy):
    if conn is None:
        return {}
    trades = [dict(r) for r in conn.execute(
        "SELECT ts, side, reason, price_sol, qty, sol, usd, symbol, batch FROM trades"
        " WHERE mint=? AND strategy=? ORDER BY ts", (mint, strategy))]
    if not trades:
        return {}
    buy = next(t for t in trades if t["side"] == "BELI")
    end = trades[-1]["ts"] if trades[-1]["side"] == "JUAL" else time.time()
    prices = []
    if has_table(conn, "prices"):
        prices = [[r[0], r[1]] for r in conn.execute(
            "SELECT ts, price FROM prices WHERE mint=? AND ts BETWEEN ? AND ? ORDER BY ts",
            (mint, buy["ts"] - 1, end + 1))]
    pos = conn.execute("SELECT * FROM positions WHERE mint=? AND strategy=? AND batch=?",
                       (mint, strategy, buy["batch"])).fetchone()
    s = next((v for v in build_variants(SETTINGS) if v.name == strategy), SETTINGS)  # aturan milik varian ini
    return {"mint": mint, "strategy": strategy, "symbol": buy["symbol"], "entry": buy["price_sol"],
            "sol_usd": buy["usd"] / buy["sol"] if buy["sol"] else None, "trades": trades, "prices": prices,
            "position": dict(pos) if pos else None,
            "rules": {"stop_loss": 1 - s.stop_loss, "tp1": s.tp1_mult, "tp2": s.tp2_mult if s.tp2_mult < 1000 else None}}


ALT_PAGE = BASE_DIR / "web" / "alt.html"


def alt_summary():
    """Data halaman altcoin (dibaca dari altcoin.db, read-only)."""
    out = {"strategies": [], "equity": {}, "open": [], "feed": [], "market": [], "stats": {}, "now": time.time()}
    if not ALT_DB.exists():
        return out
    conn = sqlite3.connect(f"file:{ALT_DB}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        if not has_table(conn, "alt_books"):
            return out
        out["stats"] = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM alt_stats")}
        market = [dict(r) for r in conn.execute("SELECT * FROM alt_market ORDER BY rank IS NULL, rank, base")]
        prices = {m["symbol"]: m["price"] for m in market}
        holders = {}
        for r in conn.execute("SELECT * FROM alt_open ORDER BY opened DESC"):
            o = dict(r)
            o["price"] = prices.get(o["symbol"])
            o["value"] = open_value(o, o["price"] or o["entry"])
            # spot: perubahan harga; futures: hasil terhadap margin (sudah termasuk leverage & funding)
            o["ret_pct"] = (o["value"] / o["cost"] - 1) * 100 if o.get("liq") else                 ((o["price"] or o["entry"]) / o["entry"] - 1) * 100
            out["open"].append(o)
            holders.setdefault(o["symbol"], []).append(o["strategy"])
        for m in market:
            m["held_by"] = holders.get(m["symbol"], [])
        out["market"] = market
        bt = {r["strategy"]: dict(r) for r in conn.execute("SELECT * FROM alt_backtest")}
        budget = ALT_SETTINGS.budget_usd
        for st in ALT_STRATEGIES:
            book = conn.execute("SELECT cash, started FROM alt_books WHERE strategy=?", (st.name,)).fetchone()
            if not book:
                continue
            opens = [o for o in out["open"] if o["strategy"] == st.name]
            equity = book["cash"] + sum(o["value"] for o in opens)
            n, wins, pnl, best, worst = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(pnl>0),0), COALESCE(SUM(pnl),0), MAX(ret), MIN(ret) FROM alt_positions"
                " WHERE strategy=?", (st.name,)).fetchone()
            curve = [(r[0], r[1]) for r in conn.execute(
                "SELECT ts, equity FROM alt_equity WHERE strategy=? ORDER BY ts", (st.name,))]
            curve.append((time.time(), equity))
            peak, max_dd = budget, 0.0
            for _, v in curve:
                peak = max(peak, v)
                max_dd = min(max_dd, v / peak - 1)
            step = max(1, len(curve) // 400)
            points = {}   # per detik unik (syarat chart), titik terakhir selalu ikut
            for t, v in curve[::step] + [curve[-1]]:
                points[int(t)] = round((v / budget - 1) * 100, 3)
            out["equity"][st.name] = [{"time": t, "value": v} for t, v in sorted(points.items())]
            out["strategies"].append({
                "name": st.name, "label": st.label, "desc": st.desc, "tf": st.tf, "order": st.order,
                "market": st.market, "leverage": st.leverage,
                "cash": book["cash"], "started": book["started"], "equity": equity,
                "ret_pct": (equity / budget - 1) * 100, "trades": n, "wins": wins, "realized": pnl,
                "best": best, "worst": worst, "open": len(opens), "max_dd": max_dd * 100,
                "backtest": bt.get(st.name)})
        out["feed"] = [dict(r) for r in conn.execute(
            "SELECT t.ts, t.strategy, t.symbol, t.side, t.dir, t.lev, t.reason, t.price, t.usd, p.ret FROM alt_trades t"
            " LEFT JOIN alt_positions p ON t.side='JUAL' AND p.strategy=t.strategy AND p.symbol=t.symbol"
            " AND ABS(p.closed - t.ts) < 1 ORDER BY t.ts DESC LIMIT 40")]
        out["ai"] = {"live": None, "backtest": None, "history": []}
        if has_table(conn, "alt_ai_runs"):
            for r in conn.execute("SELECT ts, kind, meta FROM alt_ai_runs ORDER BY ts DESC LIMIT 60"):
                m = {"ts": r["ts"], **json.loads(r["meta"])}
                if r["kind"] == "live":
                    out["ai"]["history"].append(m)
                if not out["ai"][r["kind"]]:
                    out["ai"][r["kind"]] = m
        out["closed"] = [dict(r) for r in conn.execute(
            "SELECT strategy, symbol, opened, closed, entry, exit, cost, proceeds, pnl, ret, reason, side, lev, funding"
            " FROM alt_positions ORDER BY closed DESC LIMIT 300")]
        out["budget"] = budget
        out["max_positions"] = ALT_SETTINGS.max_positions
        return out
    finally:
        conn.close()


LAB_PAGE = BASE_DIR / "web" / "lab.html"


def alt_coin(symbol):
    """Semua transaksi & posisi terbuka untuk satu koin altcoin (penanda di chart candlestick)."""
    out = {"symbol": symbol, "trades": [], "open": []}
    if not ALT_DB.exists():
        return out
    conn = sqlite3.connect(f"file:{ALT_DB}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        out["trades"] = [dict(r) for r in conn.execute(
            "SELECT ts, strategy, side, dir, lev, price, usd, reason FROM alt_trades WHERE symbol=? ORDER BY ts", (symbol,))]
        out["open"] = [dict(r) for r in conn.execute("SELECT * FROM alt_open WHERE symbol=?", (symbol,))]
        return out
    finally:
        conn.close()
ALT_ABYSS_PAGE = BASE_DIR / "web" / "alt_abyss.html"   # tema altcoin laut dalam (gurita)


def lab_summary(conn):
    """Data ringkas untuk Command Center (/lab): meme + altcoin + AI dalam satu panggilan."""
    out = {"now": time.time(), "meme": {}, "alt": alt_summary()}
    if conn is None or not has_table(conn, "positions"):
        return out
    m = out["meme"]
    m["stats"] = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM stats")}         if has_table(conn, "stats") else {}
    active = list(VARIANTS)
    marks = ",".join("?" * len(active))
    m["variants"] = []
    for name, v in VARIANTS.items():
        n, pnl, wins = conn.execute("SELECT COUNT(*), COALESCE(SUM(pnl_usd),0), COALESCE(SUM(pnl_usd>0),0)"
                                    " FROM positions WHERE strategy=?", (name,)).fetchone()
        m["variants"].append({"name": name, "label": v.label, "order": v.order, "n": n, "pnl": pnl, "wins": wins})
    n, pnl, wins = conn.execute(f"SELECT COUNT(*), COALESCE(SUM(pnl_usd),0), COALESCE(SUM(pnl_usd>0),0)"
                                f" FROM positions WHERE strategy IN ({marks})", active).fetchone()
    m["total"] = {"n": n, "pnl": pnl, "wins": wins}
    # pasang-surut: PnL kumulatif varian aktif (maks ±300 titik)
    rows = conn.execute(f"SELECT closed, pnl_usd FROM positions WHERE strategy IN ({marks}) ORDER BY closed",
                        active).fetchall()
    step, cum, tide = max(1, len(rows) // 300), 0.0, []
    for k, (t, p) in enumerate(rows):
        cum += p
        if k % step == 0 or k == len(rows) - 1:
            tide.append([t, round(cum, 3)])
    m["tide"] = tide
    m["open"] = [o for o in open_positions(conn) if o["strategy"] in VARIANTS]
    m["feed"] = [dict(r) for r in conn.execute(
        "SELECT t.ts, t.side, t.strategy, t.symbol, t.reason, t.usd, t.price_sol / NULLIF(b.price_sol, 0) AS mult"
        " FROM trades t LEFT JOIN trades b ON b.side='BELI' AND b.mint=t.mint AND b.strategy=t.strategy"
        " AND b.batch=t.batch ORDER BY t.ts DESC LIMIT 30")]
    if has_table(conn, "token_features"):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(token_features)")}
        v2 = "COALESCE(SUM(ver=2),0)" if "ver" in cols else "0"
        f = conn.execute(f"SELECT COUNT(*), {v2}, COALESCE(SUM(max_mult>=2),0) FROM token_features"
                         " WHERE finalized=1").fetchone()
        m["learn"] = {"total": f[0], "v2": f[1], "x2": f[2]}
        m["recent_tokens"] = [dict(r) for r in conn.execute(
            "SELECT symbol, created, max_mult, final_mult FROM token_features WHERE finalized=1"
            " ORDER BY created DESC LIMIT 24")]
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, body, ctype="application/json", code=200):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path in ("/", "/index.html"):
                return self.send(PAGE.read_bytes(), "text/html; charset=utf-8")
            if url.path in ("/alt", "/alt.html"):
                return self.send(ALT_PAGE.read_bytes(), "text/html; charset=utf-8")
            if url.path == "/api/alt/summary":
                return self.send(alt_summary())
            if url.path == "/api/alt/coin":
                return self.send(alt_coin(q.get("symbol", "").upper()))
            if url.path in ("/lab", "/lab.html"):
                return self.send(LAB_PAGE.read_bytes(), "text/html; charset=utf-8")
            if url.path in ("/alt-abyss", "/alt_abyss.html"):
                return self.send(ALT_ABYSS_PAGE.read_bytes(), "text/html; charset=utf-8")
            conn = db()
            try:
                if url.path == "/api/summary":
                    return self.send(summary(conn))
                if url.path == "/api/lab":
                    return self.send(lab_summary(conn))
                if url.path == "/api/positions":
                    return self.send(positions(conn, q.get("status", "closed")))
                if url.path == "/api/coin":
                    return self.send(coin(conn, q.get("mint", ""), q.get("strategy", "")))
            finally:
                if conn:
                    conn.close()
            self.send({"error": "not found"}, code=404)
        except sqlite3.OperationalError as e:
            self.send({"error": f"database sibuk: {e}"}, code=503)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    url = f"http://localhost:{PORT}"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"Dashboard sudah berjalan di jendela lain. Buka {url} di browser.")
        if "--no-browser" not in sys.argv:
            webbrowser.open(url)
        time.sleep(8)
        return
    print(f"Dashboard berjalan di {url}  (Ctrl+C untuk berhenti)")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
