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


# ================= tema PRISM: konsol riset (leaderboard lengkap, biaya, risiko, 8 agent)
PRISM_PAGE = BASE_DIR / "web" / "prism.html"
LAB_DB = BASE_DIR / "wallet_lab.db"
LB_KEYS = ("strategy", "n", "win_rate", "avg_rr", "profit_factor", "net", "expectancy", "expectancy_pct", "avg_win",
           "avg_loss", "max_dd", "sharpe", "sortino", "fees", "slippage", "oos_pf", "oos_exp", "oos_n", "is_pf",
           "wf_exp", "wf_pos", "wf_folds", "t_stat", "verdict", "risk_net", "risk_blocked", "volume")


def _clean(v):
    """JSON aman untuk browser: Infinity/NaN -> None (PF tak terhingga = belum pernah rugi)."""
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        return None
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


def _curve(trades, limit=160):
    cum, pts = 0.0, []
    for t in sorted(trades, key=lambda t: t["closed"]):
        cum += t["pnl"]
        pts.append([int(t["closed"]), round(cum, 4)])
    step = max(1, len(pts) // limit)
    return pts[::step] + ([pts[-1]] if pts and len(pts) % step else [])


def _ro(path):
    if not path.exists():
        return None
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    return c


# pemindai altcoin multi-timeframe (Fase 4): dihitung di latar belakang supaya halaman tetap cepat
SCAN = {"at": 0, "rows": [], "err": None}


def scan_loop():
    import threading  # noqa: F401  (dipakai oleh pemanggil)
    from alt_data import HIST_DB
    from indicators import analyze
    while True:
        try:
            if HIST_DB.exists():
                hdb = sqlite3.connect(f"file:{HIST_DB}?mode=ro", uri=True, timeout=10)
                ranks = {}
                alt = _ro(ALT_DB)
                if alt is not None:
                    ranks = {r["base"]: r["rank"] for r in alt.execute("SELECT base, rank FROM alt_market")}
                    alt.close()
                bases = [r[0].split("/")[0] for r in hdb.execute("SELECT DISTINCT symbol FROM ohlcv WHERE tf='1d'")]
                rows = []
                for b in bases:
                    a = analyze(b, hdb)
                    if not a:
                        continue
                    t = a["tf"]
                    h1, h4, d1 = t.get("1h", {}), t.get("4h", {}), t.get("1d", {})
                    px = h1.get("close") or d1.get("close")
                    rows.append({"base": b, "symbol": b + "USDT", "rank": ranks.get(b), "mtf": a["mtf_align"], "close": px,
                                 "trend": {tf: t[tf]["trend"] for tf in t}, "regime": {tf: t[tf]["regime"] for tf in t},
                                 "vol": d1.get("volatility"), "rsi1h": h1.get("rsi"), "rsi1d": d1.get("rsi"),
                                 "adx4h": h4.get("adx"), "rvol1h": h1.get("rvol"), "vwap1h": h1.get("vwap_dist"),
                                 "atr1d": d1.get("atr_pct"), "corr": d1.get("btc_corr"), "brk4h": h4.get("breakout"),
                                 "sup": (px / d1["support"] - 1) if px and d1.get("support") else None,
                                 "res": (d1["resistance"] / px - 1) if px and d1.get("resistance") else None})
                hdb.close()
                SCAN.update(at=time.time(), rows=sorted(rows, key=lambda r: (r["rank"] is None, r["rank"] or 0)), err=None)
        except Exception as e:  # pemindai gagal tidak boleh menjatuhkan dashboard
            SCAN["err"] = f"{type(e).__name__}: {e}"
        time.sleep(300)


def quant_summary(conn):
    from metrics import alt_trades, leaderboard, meme_trades
    from risk import KILL_FILE, RiskLimits
    now = time.time()
    limits = RiskLimits()
    out = {"now": now, "meme": [], "alt": [], "alt_bt": [], "calib": [], "risk": {}, "agents": [], "costs": {},
           "curves": {}, "alt_curves": {}, "labels": {n: v.label for n, v in VARIANTS.items()},
           "alt_labels": {st.name: st.label for st in ALT_STRATEGIES},
           "limits": {k: getattr(limits, k) for k in ("max_daily_loss", "max_drawdown", "max_exposure", "max_slippage")},
           "risk_mode": SETTINGS.risk_mode, "kill": KILL_FILE.exists()}
    stats = {}
    if conn is not None:
        groups = meme_trades(conn, SETTINGS)
        out["meme"] = [{k: r.get(k) for k in LB_KEYS} for r in leaderboard(groups, limits)]
        out["curves"] = {n: _curve(tr) for n, tr in groups.items()}
        allt = [t for tr in groups.values() for t in tr]
        out["curve_all"] = _curve(allt, 120)
        net = sum(t["pnl"] for t in allt)
        fees = sum(t["fees"] for t in allt)
        slip = sum(t["slip"] for t in allt)
        out["costs"] = {"net": net, "fees": fees, "slip": slip, "gross": net + fees + slip,
                        "volume": sum(t["cost"] for t in allt), "n": len(allt),
                        "risk_net": sum(r.get("risk_net") or 0 for r in out["meme"])}
        stats = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM stats")}
        if has_table(conn, "risk_events"):
            ev = conn.execute("SELECT action, reasons FROM risk_events WHERE ts >= ?", (now - 86400,)).fetchall()
            kinds = {}
            for e in ev:
                for part in (e["reasons"] or "").split("; "):
                    if part:
                        k = part.split(" ")[0] + (" " + part.split(" ")[1] if part.startswith(("rugi", "order", "kill")) else "")
                        kinds[k] = kinds.get(k, 0) + 1
            out["risk"] = {"events24": len(ev), "blocked24": sum(e["action"] == "blokir" for e in ev), "kinds": kinds}
        feats = conn.execute("SELECT COUNT(*), COALESCE(SUM(created >= ?),0) FROM token_features", (now - 3600,)).fetchone()
        out["features"] = {"total": feats[0], "last_hour": feats[1]}
    alt = _ro(ALT_DB)
    regime, ai = {}, {}
    alt_hb = None
    if alt is not None:
        try:
            gr = alt_trades(alt, ALT_SETTINGS, {r["symbol"] for r in alt.execute("SELECT symbol FROM alt_market WHERE rank <= 10")})
            out["alt"] = [{k: r.get(k) for k in LB_KEYS} for r in leaderboard(gr, None)]
            have = {r["strategy"] for r in out["alt"]}
            out["alt"] += [{"strategy": st.name, "n": 0, "verdict": "belum ada transaksi"} for st in ALT_STRATEGIES
                           if st.name not in have]
            out["alt_curves"] = {n: _curve(tr) for n, tr in gr.items()}
            altt = [t for tr in gr.values() for t in tr]
            anet, afee, aslip = sum(t["pnl"] for t in altt), sum(t["fees"] for t in altt), sum(t["slip"] for t in altt)
            out["costs_alt"] = {"net": anet, "fees": afee, "slip": aslip, "gross": anet + afee + aslip,
                                "volume": sum(t["cost"] for t in altt), "n": len(altt)}
            out["curve_alt_all"] = _curve(altt, 120)
            best = max((r for r in out["alt"] if r.get("n") and r["strategy"] != "hodl_btc"),
                       key=lambda r: r.get("expectancy") or -1e9, default=None)
            out["alt_best"] = best and best["strategy"]
            out["alt_bt"] = [dict(r) for r in alt.execute(
                "SELECT strategy, tf, days, ret_pct, trades, wins, max_dd, pf, hodl_pct FROM alt_backtest"
                " WHERE ran_at = (SELECT MAX(ran_at) FROM alt_backtest) ORDER BY ret_pct DESC")]
            b = alt.execute("SELECT price, chg24, rsi1h, trend1h, trend4h FROM alt_market WHERE symbol='BTCUSDT'").fetchone()
            regime = dict(b) if b else {}
            m = alt.execute("SELECT meta FROM alt_ai_runs WHERE kind='live' ORDER BY ts DESC LIMIT 1").fetchone()
            ai = json.loads(m["meta"]) if m else {}
            hb = alt.execute("SELECT value FROM alt_stats WHERE key='heartbeat'").fetchone()
            alt_hb = hb[0] if hb else None
            out["alt_market"] = alt.execute("SELECT COUNT(*) FROM alt_market").fetchone()[0]
        finally:
            alt.close()
    lab = _ro(LAB_DB)
    leaders = []
    if lab is not None:
        try:
            if lab.execute("SELECT 1 FROM sqlite_master WHERE name='calibration'").fetchone():
                run = lab.execute("SELECT MAX(run) FROM calibration").fetchone()[0]
                out["calib"] = [dict(r) for r in lab.execute("SELECT * FROM calibration WHERE run = ? ORDER BY side, latency", (run,))]
                out["calib_at"] = run
            if lab.execute("SELECT 1 FROM sqlite_master WHERE name='leaders'").fetchone():
                leaders = [dict(r) for r in lab.execute("SELECT wallet, picked FROM leaders")]
        finally:
            lab.close()
    # aliran transaksi per token + skor Safety (Fase 3, flow.db)
    fl = _ro(BASE_DIR / "flow.db")
    out["flow"] = {"tracking": int(stats.get("flow_tracking", 0)), "trades": int(stats.get("flow_trades", 0)),
                   "mode": int(stats.get("flow_on", 0))}
    out["scan"], out["scan_at"] = SCAN["rows"], SCAN["at"]
    if fl is not None:
        try:
            rows = [dict(r) for r in fl.execute("SELECT mint, symbol, created, missed_sol, n_trades, n_traders, label, safety60,"
                                                " safety60_reasons, peak_mult, dev_sold_frac FROM token_flow WHERE finalized IS NOT NULL")]
            labels = {}
            for r in rows:
                labels[r["label"]] = labels.get(r["label"], 0) + 1
            buckets = []
            for lo, hi in ((0, 20), (20, 40), (40, 60), (60, 101)):
                b = [r for r in rows if r["safety60"] is not None and lo <= r["safety60"] < hi]
                buckets.append({"lo": lo, "hi": min(hi, 100), "n": len(b),
                                "bad": sum(r["label"] in ("rug", "dump") for r in b)})
            missed = sorted(r["missed_sol"] for r in rows if r["missed_sol"] is not None)
            recent = sorted(rows, key=lambda r: r["created"], reverse=True)[:8]
            out["flow"].update({"done": len(rows), "labels": labels, "buckets": buckets,
                                "missed_med": missed[len(missed) // 2] if missed else None,
                                "missed_1": sum(m > 1 for m in missed) / len(missed) if missed else None,
                                "raw": fl.execute("SELECT COUNT(*) FROM token_trades").fetchone()[0],
                                "recent": [{**r, "safety60_reasons": json.loads(r["safety60_reasons"] or "[]")} for r in recent]})
        finally:
            fl.close()
    sim_hb = stats.get("heartbeat")
    # varian terbaik hanya dari yang masih aktif (varian yang dipensiunkan tetap tampil di tabel, tapi tidak dinilai juara)
    meme_best = max((r for r in out["meme"] if r.get("n") and r["strategy"] in VARIANTS), key=lambda r: r.get("expectancy") or -1e9,
                    default=None)
    cand = [r["strategy"] for r in out["meme"] + out["alt"] if r.get("verdict") == "kandidat"]
    alive = lambda hb, sec: bool(hb and now - hb < sec)
    out["agents"] = [
        {"id": "supervisor", "name": "Supervisor", "kind": "kode", "ok": alive(sim_hb, 60) and alive(alt_hb, 300),
         "metric": f"meme {'aktif' if alive(sim_hb, 60) else 'MATI'} · altcoin {'aktif' if alive(alt_hb, 300) else 'MATI'}"},
        {"id": "meme", "name": "Meme Scanner", "kind": "kode", "ok": alive(sim_hb, 60),
         "metric": f"{int(stats.get('tokens_seen', 0))} token sesi ini · {out['flow']['tracking']} diikuti live"},
        {"id": "alt", "name": "Altcoin Scanner", "kind": "kode", "ok": alive(alt_hb, 300),
         "metric": f"{out.get('alt_market', 0)} koin · EMA/RSI/ATR/trend"},
        {"id": "safety", "name": "Safety", "kind": "kode", "ok": True,
         "metric": f"skor 4 lapis · {out['flow'].get('done', 0)} token berlabel"},
        {"id": "smart", "name": "Smart Money", "kind": "kode", "ok": bool(leaders),
         "metric": f"{len(leaders)} leader dipantau (forward test)"},
        {"id": "regime", "name": "Market Regime", "kind": "kode", "ok": bool(regime),
         "metric": f"BTC 1j {regime.get('trend1h', '?')} · 4j {regime.get('trend4h', '?')} · RSI {regime.get('rsi1h') or 0:.0f}"},
        {"id": "risk", "name": "Risk & Portfolio", "kind": "veto", "ok": not out["kill"],
         "metric": ("KILL SWITCH AKTIF" if out["kill"] else f"mode {SETTINGS.risk_mode} · {out['risk'].get('blocked24', 0)} blokir / 24j")},
        {"id": "research", "name": "Strategy Research", "kind": "kode + LLM", "ok": True,
         "metric": f"{len(cand)} kandidat lolos · AI futures {'menunggu bukti' if not ai.get('sides') else ', '.join(ai['sides'])}"},
    ]
    out["regime"], out["ai"] = regime, {k: ai.get(k) for k in ("acc", "base_acc", "long_n", "long_hit", "long_ret",
                                                                "short_n", "short_hit", "short_ret", "sides", "trained_at")}
    out["meme_best"] = meme_best and meme_best["strategy"]
    return _clean(out)


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
            if url.path in ("/prism", "/prism.html", "/prism-alt"):
                return self.send(PRISM_PAGE.read_bytes(), "text/html; charset=utf-8")
            conn = db()
            try:
                if url.path == "/api/summary":
                    return self.send(summary(conn))
                if url.path == "/api/lab":
                    return self.send(lab_summary(conn))
                if url.path == "/api/quant":
                    return self.send(quant_summary(conn))
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
        import threading
        threading.Thread(target=scan_loop, daemon=True).start()
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
