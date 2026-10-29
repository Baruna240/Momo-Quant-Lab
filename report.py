"""Laporan hasil simulasi. Jalankan: python report.py"""
import sqlite3
import sys

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades(
    id INTEGER PRIMARY KEY, ts REAL, strategy TEXT, batch INT, mint TEXT, symbol TEXT,
    side TEXT, reason TEXT, price_sol REAL, qty REAL, sol REAL, usd REAL);
CREATE TABLE IF NOT EXISTS positions(
    id INTEGER PRIMARY KEY, strategy TEXT, batch INT, mint TEXT, symbol TEXT,
    opened REAL, closed REAL, cost_usd REAL, proceeds_usd REAL, pnl_usd REAL,
    ret_mult REAL, peak_mult REAL, reason TEXT, graduated INT);
CREATE TABLE IF NOT EXISTS batches(
    id INTEGER PRIMARY KEY, strategy TEXT, batch INT, started REAL, ended REAL,
    invested REAL, returned REAL, pnl REAL);
CREATE TABLE IF NOT EXISTS prices(mint TEXT, ts REAL, price REAL);
CREATE INDEX IF NOT EXISTS prices_mint ON prices(mint, ts);
CREATE TABLE IF NOT EXISTS stats(key TEXT PRIMARY KEY, value REAL);
CREATE TABLE IF NOT EXISTS position_meta(
    strategy TEXT, batch INT, mint TEXT, curve TEXT, launch REAL, creator TEXT, dev_initial REAL, created REAL,
    PRIMARY KEY(strategy, batch, mint));
CREATE TABLE IF NOT EXISTS auto_log(ts REAL, follow TEXT, action TEXT, score REAL, n INT, detail TEXT);
CREATE TABLE IF NOT EXISTS token_features(
    mint TEXT PRIMARY KEY, symbol TEXT, created REAL, sampled INT, bought INT,
    m30 REAL, s30 REAL, d30 REAL, m60 REAL, s60 REAL, d60 REAL,
    m90 REAL, s90 REAL, d90 REAL, m180 REAL, s180 REAL, d180 REAL,
    max_mult REAL, min_mult REAL, t2x REAL, graduated INT, final_mult REAL, finalized INT);
"""


def connect(path=DB_PATH):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    # kolom yang ditambahkan belakangan (database lama ditambah otomatis)
    for table, col in (("trades", "kelly_stake"), ("positions", "kelly_stake"), ("trades", "slip")):
        cols = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} REAL")
    db.commit()
    return db


def kelly(n, wins, sum_win, sum_loss, settings):
    """Kelly f* = p - (1-p)/b, dengan b = rata-rata untung / rata-rata rugi (dalam USD)."""
    if n < settings.kelly_min_sample:
        return {"status": "belajar", "f": None, "p": wins / n if n else None, "b": None}
    losses = n - wins
    p = wins / n
    if wins == 0:
        return {"status": "stop", "f": -1.0, "p": 0.0, "b": 0.0}
    if losses == 0 or sum_loss <= 0:
        return {"status": "aktif", "f": p, "p": p, "b": None}
    b = (sum_win / wins) / (sum_loss / losses)
    f = p - (1 - p) / b
    return {"status": "aktif" if f > 0 else "stop", "f": f, "p": p, "b": b}


def build_report(db):
    lines = ["LAPORAN SIMULASI (bukan uang sungguhan)"]
    strategies = [r[0] for r in db.execute("SELECT DISTINCT strategy FROM positions ORDER BY strategy")]
    if not strategies:
        return lines[0] + "\nBelum ada posisi yang ditutup."
    for strat in strategies:
        n, cost, back, wins, best, grad = db.execute(
            "SELECT COUNT(*), SUM(cost_usd), SUM(proceeds_usd), SUM(pnl_usd > 0), MAX(ret_mult), SUM(graduated)"
            " FROM positions WHERE strategy=?", (strat,)).fetchone()
        net = back - cost
        peaks = {m: db.execute("SELECT COUNT(*) FROM positions WHERE strategy=? AND peak_mult>=?",
                               (strat, m)).fetchone()[0] for m in (2, 5, 10, 20)}
        b_n, b_win, b_pnl = db.execute(
            "SELECT COUNT(*), SUM(pnl > 0), SUM(pnl) FROM batches WHERE strategy=?", (strat,)).fetchone()
        lines += [
            "",
            f"== Strategi: {strat.upper()} ==",
            f"Posisi selesai : {n}  (untung {wins}, win rate {wins / n:.0%})",
            f"Modal terpakai : ${cost:.2f}  ->  kembali ${back:.2f}",
            f"Hasil bersih   : {'+' if net >= 0 else '-'}${abs(net):.2f}  ({net / cost:+.0%})",
            f"Hasil terbaik  : {best:.2f}x modal",
            f"Lulus ke DEX   : {grad or 0} koin",
            "Sempat menyentuh (puncak, belum tentu terjual di sana): "
            + ", ".join(f">={m}x: {c}" for m, c in peaks.items()),
        ]
        if b_n:
            lines.append(f"Putaran selesai: {b_n} (untung {b_win or 0}, rugi {b_n - (b_win or 0)}),"
                         f" total {b_pnl:+.2f} USD")
    return "\n".join(lines)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(build_report(connect()))
