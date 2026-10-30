"""Wallet Lab: membedah strategi dompet sniper pump.fun dari data on-chain publik (hanya MEMBACA).

Jalankan: python wallet_lab.py discover --tokens 40 --early 25   (cari dompet yang selalu beli paling awal)
          python wallet_lab.py profile --top 15 --txs 400        (bedah riwayat transaksi dompet-dompet itu)
          python wallet_lab.py wallet <ALAMAT> --txs 400         (bedah satu dompet publik pilihanmu)
          python wallet_lab.py report                            (ringkasan + usulan varian simulator)

Cara kerja:
1. discover: untuk token pump.fun yang sudah direkam simulator (token_features), ambil transaksi PALING AWAL
   lewat Helius RPC. Dompet yang berulang kali muncul di antara pembeli pertama = kandidat sniper.
2. profile: baca ratusan transaksi terakhir tiap dompet, hitung perubahan SOL & token si pembayar fee
   (sudah termasuk fee jaringan, priority fee, dan tip), lalu susun jadi putaran beli -> jual per koin.
3. Dari putaran itu diukur: untung/rugi riil, win rate, seberapa cepat masuk setelah koin lahir, kelipatan
   saat jual pertama, lama memegang, seberapa pilih-pilih, dan ciri koin yang dipilih dibanding semua koin.
Tidak ada dompet, private key, atau transaksi yang dikirim. Hasil disimpan di wallet_lab.db.
"""
import argparse
import asyncio
import json
import random
import sqlite3
import statistics as stx
import sys
import time

import httpx

from config import BASE_DIR, DB_PATH, load_settings

LAB_DB = BASE_DIR / "wallet_lab.db"
WSOL = "So11111111111111111111111111111111111111112"
CONCURRENCY = 4          # jangan habiskan batas kecepatan Helius (simulator juga memakainya)
MAX_SIG_PAGES = 6        # token yang terlalu ramai (>6000 transaksi) dilewati: transaksi awalnya terlalu jauh
CLOSED_FRAC = 0.98       # dianggap sudah keluar jika >=98% token yang dibeli sudah dijual
MIN_TRIPS = 20           # dompet baru dinilai setelah punya minimal 20 putaran selesai
DUST_SOL = 0.0005        # token keluar dengan SOL masuk sekecil ini = dipindah, bukan dijual


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def open_lab():
    db = sqlite3.connect(LAB_DB)
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE IF NOT EXISTS tx (sig TEXT PRIMARY KEY, wallet TEXT, ts REAL, slot INTEGER, sol REAL, fee REAL,
                                   tokens TEXT, ok INTEGER);
    CREATE INDEX IF NOT EXISTS tx_wallet ON tx(wallet);
    CREATE TABLE IF NOT EXISTS wallet_sigs (wallet TEXT, sig TEXT, PRIMARY KEY (wallet, sig));
    CREATE TABLE IF NOT EXISTS early_buys (mint TEXT, wallet TEXT, sig TEXT, ts REAL, d_slot INTEGER, d_sec REAL,
                                           sol REAL, rank INTEGER, PRIMARY KEY (mint, wallet));
    CREATE TABLE IF NOT EXISTS scanned (mint TEXT PRIMARY KEY, ts REAL, created REAL, creator TEXT, status TEXT);
    CREATE TABLE IF NOT EXISTS profiles (wallet TEXT PRIMARY KEY, ts REAL, stats TEXT);
    CREATE TABLE IF NOT EXISTS leaders (wallet TEXT PRIMARY KEY, picked REAL, stats TEXT);
    """)
    return db


def parse_tx(tx):
    """Transaksi mentah (encoding json) -> perubahan SOL & token milik si pembayar fee."""
    meta = tx.get("meta")
    if not meta:
        return None
    payer = tx["transaction"]["message"]["accountKeys"][0]
    sol = (meta["postBalances"][0] - meta["preBalances"][0]) / 1e9   # sudah termasuk fee & tip
    deltas = {}
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in meta.get(side) or []:
            if b.get("owner") == payer:
                amt = float(b["uiTokenAmount"].get("uiAmount") or 0)
                deltas[b["mint"]] = deltas.get(b["mint"], 0.0) + sign * amt
    sol += deltas.pop(WSOL, 0.0)       # sebagian bot berdagang lewat akun wSOL
    return {"wallet": payer, "ts": tx.get("blockTime") or 0, "slot": tx.get("slot") or 0, "sol": sol,
            "fee": meta.get("fee", 0) / 1e9, "tokens": {m: d for m, d in deltas.items() if abs(d) > 1e-9},
            "ok": meta.get("err") is None}


def build_trips(rows):
    """Daftar transaksi satu dompet -> putaran per koin (beli ... jual)."""
    by_mint = {}
    for r in sorted(rows, key=lambda r: (r["ts"], r["slot"])):
        if not r["ok"] or len(r["tokens"]) != 1:
            continue      # gagal, atau swap antar-token / transaksi rumit: lewati
        (mint, tok), = r["tokens"].items()
        if tok > 0 and r["sol"] < 0:
            kind = "buy"
        elif tok < 0 and r["sol"] > DUST_SOL:
            kind = "sell"
        else:
            kind = "move"
        by_mint.setdefault(mint, []).append((kind, r["ts"], tok, r["sol"], r["fee"], r.get("sig"), r["slot"]))
    trips = []
    for mint, ev in by_mint.items():
        if ev[0][0] != "buy" or any(k == "move" for k, *_ in ev):
            continue      # pembelian di luar jendela data, atau token dipindah ke dompet lain: tidak bisa dinilai
        buys = [e for e in ev if e[0] == "buy"]
        sells = [e for e in ev if e[0] == "sell"]
        bought, sold = sum(e[2] for e in buys), -sum(e[2] for e in sells)
        cost, back = -sum(e[3] for e in buys), sum(e[3] for e in sells)
        if sold > bought * 1.02:
            continue
        t = {"mint": mint, "t_buy": buys[0][1], "n_buys": len(buys), "n_sells": len(sells), "cost": cost,
             "back": back, "closed": sold >= bought * CLOSED_FRAC, "fees": sum(e[4] for e in ev),
             "hold": (sells[-1][1] - buys[0][1]) if sells else None, "first_sell_mult": None, "first_sell_frac": None,
             "buy_sig": buys[0][5], "buy_slot": buys[0][6],
             "sells": [(e[5], e[6], -e[2] / bought if bought else 0) for e in sells]}   # (sig, slot, porsi dijual)
        if sells and bought > 0 and cost > 0:
            k, ts, tok, sol, *_ = sells[0]
            t["first_sell_mult"] = (sol / -tok) / (cost / bought)
            t["first_sell_frac"] = -tok / bought
        trips.append(t)
    return trips


def med(xs):
    xs = [x for x in xs if x is not None]
    return stx.median(xs) if xs else None


def wallet_stats(trips, feats):
    """Ringkasan perilaku satu dompet. feats = {mint: baris token_features} untuk koin yang kita rekam."""
    done = [t for t in trips if t["closed"]]
    if not done:
        return {"n_closed": 0, "n_open": len(trips)}
    cost, back = sum(t["cost"] for t in done), sum(t["back"] for t in done)
    wins = [t for t in done if t["back"] > t["cost"]]
    losers = [t for t in done if t["back"] <= t["cost"]]
    delays = [t["t_buy"] - feats[t["mint"]]["created"] for t in done if t["mint"] in feats]
    span_h = max((max(t["t_buy"] for t in trips) - min(t["t_buy"] for t in trips)) / 3600, 1 / 60)
    return {
        "n_closed": len(done), "n_open": len(trips) - len(done), "pnl_sol": back - cost, "roi": back / cost if cost else None,
        "win_rate": len(wins) / len(done), "avg_cost": cost / len(done), "trips_per_h": len(trips) / span_h,
        "med_hold": med([t["hold"] for t in done]), "med_delay": med(delays), "n_delay": len(delays),
        "med_first_sell": med([t["first_sell_mult"] for t in done]),
        "med_first_frac": med([t["first_sell_frac"] for t in done]),
        "med_win": med([t["back"] / t["cost"] for t in wins if t["cost"]]),
        "med_loss": med([t["back"] / t["cost"] for t in losers if t["cost"]]),
        "multi_sell": sum(t["n_sells"] > 1 for t in done) / len(done),
        "fee_per_trip": sum(t["fees"] for t in done) / len(done),
        "best": max(t["back"] - t["cost"] for t in done), "worst": min(t["back"] - t["cost"] for t in done),
        "open_cost": sum(t["cost"] for t in trips if not t["closed"]),
    }


def style(s):
    d = s.get("med_delay")
    if d is None:
        return "?"
    if d < 3:
        return "sniper blok-0"
    if d < 30:
        return "sniper cepat"
    if d < 300:
        return "momentum"
    return "pemburu lambat"


class Rpc:
    def __init__(self, url):
        self.url, self.calls = url, 0
        self.sem = asyncio.Semaphore(CONCURRENCY)
        self.http = httpx.AsyncClient(timeout=30)

    async def __call__(self, method, params):
        async with self.sem:
            for attempt in range(6):
                self.calls += 1
                try:
                    r = await self.http.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
                    if r.status_code in (429, 500, 502, 503, 504):
                        raise httpx.HTTPStatusError("sibuk", request=r.request, response=r)
                    r.raise_for_status()
                    body = r.json()
                    if "error" in body:
                        raise RuntimeError(body["error"].get("message", "RPC error"))
                    return body["result"]
                except (httpx.HTTPError, httpx.HTTPStatusError) as e:
                    if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (401, 403):
                        raise RuntimeError("HELIUS_API_KEY ditolak. Cek isi file .env") from None
                    await asyncio.sleep(0.5 * 2 ** attempt + random.random() * 0.3)
            raise RuntimeError(f"{method} gagal berulang kali")

    async def close(self):
        await self.http.aclose()


async def fetch_txs(rpc, db, sigs):
    """Ambil & urai transaksi yang belum ada di cache (hemat kuota: tiap transaksi hanya diambil sekali)."""
    have = {r[0] for r in db.execute(f"SELECT sig FROM tx WHERE sig IN ({','.join('?' * len(sigs))})", sigs)} if sigs else set()
    todo = [s for s in sigs if s not in have]

    async def one(sig):
        try:
            raw = await rpc("getTransaction", [sig, {"encoding": "json", "maxSupportedTransactionVersion": 1,
                                                     "commitment": "confirmed"}])
        except RuntimeError as e:
            if "ditolak" in str(e):
                raise
            return
        p = parse_tx(raw) if raw else None
        if p:
            db.execute("INSERT OR REPLACE INTO tx VALUES (?,?,?,?,?,?,?,?)", (sig, p["wallet"], p["ts"], p["slot"],
                       p["sol"], p["fee"], json.dumps(p["tokens"]), int(p["ok"])))

    for i in range(0, len(todo), 50):
        await asyncio.gather(*(one(s) for s in todo[i:i + 50]))
        db.commit()
    rows = db.execute(f"SELECT * FROM tx WHERE sig IN ({','.join('?' * len(sigs))})", sigs).fetchall() if sigs else []
    return {r["sig"]: {**dict(r), "tokens": json.loads(r["tokens"])} for r in rows}


def load_features(mints=None):
    sim = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    sim.row_factory = sqlite3.Row
    if mints is None:
        rows = sim.execute("SELECT * FROM token_features WHERE finalized = 1").fetchall()
    else:
        mints = list(mints)
        rows = []
        for i in range(0, len(mints), 500):
            part = mints[i:i + 500]
            rows += sim.execute(f"SELECT * FROM token_features WHERE mint IN ({','.join('?' * len(part))})", part).fetchall()
    sim.close()
    return {r["mint"]: dict(r) for r in rows}


# ---------------------------------------------------------------- discover
async def discover(rpc, db, n_tokens, early):
    feats = load_features()
    done = {r[0] for r in db.execute("SELECT mint FROM scanned")}
    # hanya token bonding curve pump.fun (alamatnya berakhiran "pump"); puncak >100x = harga rusak, bukan pemenang
    recent = [f for f in feats.values() if f["mint"] not in done and f["mint"].endswith("pump")
              and f["created"] > time.time() - 5 * 86400 and (f["max_mult"] or 0) <= 100]
    # separuh koin yang terbang (tempat sniper jago pasti ikut), separuh acak (supaya bukan hanya pemenang)
    recent.sort(key=lambda f: f["max_mult"] or 0, reverse=True)
    pick = recent[:n_tokens // 2]
    rest = recent[n_tokens // 2:]
    pick += random.sample(rest, min(len(rest), n_tokens - len(pick)))
    log(f"memindai {len(pick)} token (dari {len(recent)} yang belum dipindai)")
    for i, f in enumerate(pick, 1):
        mint, before, sigs, status = f["mint"], None, [], "ok"
        for _ in range(MAX_SIG_PAGES):
            opts = {"limit": 1000, **({"before": before} if before else {})}
            page = await rpc("getSignaturesForAddress", [mint, opts])
            sigs += page
            if len(page) < 1000:
                break
            before = page[-1]["signature"]
        else:
            status = "terlalu ramai"
        if status == "ok":
            oldest = [s["signature"] for s in reversed(sigs) if s.get("err") is None][:early + 1]
            txs = await fetch_txs(rpc, db, oldest)
            ordered = [txs[s] for s in oldest if s in txs]
            if not ordered:
                status = "kosong"
            else:
                create, creator = ordered[0], ordered[0]["wallet"]
                rank = 0
                for t in ordered[1:]:
                    tok = t["tokens"].get(mint, 0)
                    if t["wallet"] == creator or tok <= 0 or t["sol"] >= 0:
                        continue
                    rank += 1
                    db.execute("INSERT OR REPLACE INTO early_buys VALUES (?,?,?,?,?,?,?,?)",
                               (mint, t["wallet"], t["sig"], t["ts"],
                                t["slot"] - create["slot"], t["ts"] - create["ts"], -t["sol"], rank))
                db.execute("INSERT OR REPLACE INTO scanned VALUES (?,?,?,?,?)", (mint, time.time(), create["ts"], creator, status))
        if status != "ok":
            db.execute("INSERT OR REPLACE INTO scanned VALUES (?,?,?,?,?)", (mint, time.time(), None, None, status))
        db.commit()
        log(f"[{i}/{len(pick)}] {f['symbol'] or mint[:6]:<12} puncak {f['max_mult'] or 0:5.1f}x  {status}")
    top = db.execute("""SELECT wallet, COUNT(*) n, ROUND(AVG(d_slot),1) slot, ROUND(AVG(d_sec),1) sec, ROUND(AVG(sol),3) sol
                        FROM early_buys GROUP BY wallet HAVING n >= 2 ORDER BY n DESC, sec LIMIT 25""").fetchall()
    n_scanned = db.execute("SELECT COUNT(*) FROM scanned WHERE status='ok'").fetchone()[0]
    print(f"\nDompet yang berulang kali masuk paling awal (dari {n_scanned} token):")
    print(f"{'dompet':<46}{'koin':>5}{'+slot':>7}{'+dtk':>7}{'SOL/beli':>10}")
    for r in top:
        print(f"{r['wallet']:<46}{r['n']:>5}{r['slot']:>7}{r['sec']:>7}{r['sol']:>10}")
    if not top:
        print("(belum ada dompet yang muncul di >=2 token; pindai lebih banyak token)")


# ---------------------------------------------------------------- profile
async def profile_wallet(rpc, db, wallet, n_txs, newest_only=False):
    """newest_only: berhenti begitu bertemu transaksi yang sudah tersimpan (hanya ambil yang baru)."""
    old = {r[0] for r in db.execute("SELECT sig FROM wallet_sigs WHERE wallet = ?", (wallet,))} if newest_only else set()
    before, sigs = None, []
    while len(sigs) < n_txs:
        opts = {"limit": min(1000, n_txs - len(sigs)), **({"before": before} if before else {})}
        page = await rpc("getSignaturesForAddress", [wallet, opts])
        sigs += [s["signature"] for s in page if s.get("err") is None]
        if not page or len(page) < opts["limit"] or any(x["signature"] in old for x in page):
            break
        before = page[-1]["signature"]
    db.executemany("INSERT OR IGNORE INTO wallet_sigs VALUES (?,?)", [(wallet, s) for s in sigs])
    db.commit()
    known = [r[0] for r in db.execute("SELECT sig FROM wallet_sigs WHERE wallet = ?", (wallet,))]
    txs = await fetch_txs(rpc, db, known)
    rows = [t for t in txs.values() if t["wallet"] == wallet]
    trips = build_trips(rows)
    feats = load_features({t["mint"] for t in trips})
    s = wallet_stats(trips, feats)
    s["style"] = style(s)
    s["n_tx"] = len(rows)
    s["picks"] = pick_profile(trips, feats)
    db.execute("INSERT OR REPLACE INTO profiles VALUES (?,?,?)", (wallet, time.time(), json.dumps(s)))
    db.commit()
    return s


def pick_profile(trips, feats):
    """Ciri koin yang dibeli dompet ini dibanding SEMUA koin yang kita rekam di jendela waktu yang sama."""
    mine = [feats[t["mint"]] for t in trips if t["mint"] in feats]
    if len(mine) < 5:
        return None
    t0, t1 = min(f["created"] for f in mine), max(f["created"] for f in mine)
    sim = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    sim.row_factory = sqlite3.Row
    allr = [dict(r) for r in sim.execute("SELECT * FROM token_features WHERE created BETWEEN ? AND ?", (t0, t1))]
    sim.close()

    def summary(rows):
        # median untuk angka (harga rusak/spam tidak mendominasi), persentase untuk ciri ya/tidak
        def share(k, cond):
            v = [r[k] for r in rows if r.get(k) is not None]
            return sum(map(cond, v)) / len(v) if v else None
        return {"dev_sol": med([r.get("dev_sol") for r in rows]), "max_mult": med([r.get("max_mult") for r in rows]),
                "creator_n24": share("creator_n24", lambda x: x >= 1), "name_n1h": share("name_n1h", lambda x: x >= 1),
                **{k: share(k, bool) for k in ("tw", "tg", "web", "graduated")}}
    return {"n_mine": len(mine), "n_all": len(allr), "share": len(mine) / max(len(allr), 1),
            "mine": summary(mine), "all": summary(allr)}


def fmt(v, spec=".2f", none="-"):
    return none if v is None else format(v, spec)


def print_profile(wallet, s):
    print(f"\n=== {wallet}")
    if not s.get("n_closed"):
        print(f"  belum ada putaran selesai yang bisa dinilai ({s.get('n_tx', 0)} transaksi, {s.get('n_open', 0)} masih dipegang)")
        return
    print(f"  gaya: {s['style']}   putaran selesai {s['n_closed']} (+{s['n_open']} masih dipegang, modal {s['open_cost']:.2f} SOL)")
    print(f"  PnL {s['pnl_sol']:+.2f} SOL   ROI {fmt(s['roi'])}x   win rate {s['win_rate']:.0%}   modal/putaran {s['avg_cost']:.3f} SOL")
    early = " (lebih cepat dari kabar yang sampai ke simulator kita)" if (s["med_delay"] or 1) <= 0 else ""
    print(f"  masuk: median {fmt(s['med_delay'], '.1f')} dtk setelah koin muncul di feed kita{early} (dari {s['n_delay']} koin)")
    print(f"  jual pertama di {fmt(s['med_first_sell'])}x, menjual {fmt(s['med_first_frac'] and s['med_first_frac'] * 100, '.0f')}% "
          f"| bertahap {s['multi_sell']:.0%} | pegang median {fmt(s['med_hold'] and s['med_hold'] / 60, '.1f')} mnt")
    print(f"  menang median {fmt(s['med_win'])}x, kalah median {fmt(s['med_loss'])}x | terbaik {s['best']:+.2f}, terburuk {s['worst']:+.2f} SOL")
    print(f"  {s['trips_per_h']:.1f} koin/jam, biaya jaringan+priority {s['fee_per_trip'] * 1000:.2f} mSOL/putaran")
    p = s.get("picks")
    if p:
        m, a = p["mine"], p["all"]
        print(f"  pilih-pilih: beli {p['n_mine']} dari {p['n_all']} koin yang kita rekam ({p['share']:.1%})")
        print(f"    {'ciri':<12}{'pilihannya':>11}{'semua':>9}   (median / persen koin)")
        for k, lbl, pct in (("dev_sol", "dev beli SOL", False), ("creator_n24", "dev serial", True), ("name_n1h", "nama tiruan", True),
                            ("tw", "punya X", True), ("tg", "punya TG", True), ("web", "punya web", True),
                            ("max_mult", "puncak x", False), ("graduated", "lulus DEX", True)):
            f = (lambda v: fmt(v and v * 100, ".0f") + "%") if pct else fmt
            print(f"    {lbl:<12}{f(m[k]):>11}{f(a[k]):>9}")


def report(db):
    rows = [(r["wallet"], json.loads(r["stats"])) for r in db.execute("SELECT * FROM profiles")]
    rows = [(w, s) for w, s in rows if s.get("n_closed")]
    if not rows:
        print("Belum ada profil. Jalankan dulu: python wallet_lab.py discover lalu profile")
        return
    rows.sort(key=lambda x: x[1]["pnl_sol"], reverse=True)
    print(f"{'dompet':<46}{'gaya':<15}{'putaran':>8}{'PnL SOL':>9}{'ROI':>6}{'win':>6}{'masuk dtk':>10}{'jual 1':>7}")
    for w, s in rows:
        print(f"{w:<46}{s['style']:<15}{s['n_closed']:>8}{s['pnl_sol']:>+9.2f}{fmt(s['roi']):>6}{s['win_rate']:>6.0%}"
              f"{fmt(s['med_delay'], '.1f'):>10}{fmt(s['med_first_sell']):>7}")
    good = [s for _, s in rows if s["n_closed"] >= MIN_TRIPS and (s["roi"] or 0) > 1.0]
    print(f"\nDompet untung dengan >= {MIN_TRIPS} putaran: {len(good)} dari {len(rows)}")
    if not good:
        print("Belum ada pola yang layak ditiru. Ini juga jawaban: sniper yang terlihat aktif pun banyak yang rugi.")
        return
    delay = med([s["med_delay"] for s in good])
    print("Pola gabungan dompet untung (median):")
    print(f"  masuk {fmt(delay, '.1f')} dtk setelah lahir | jual pertama {fmt(med([s['med_first_sell'] for s in good]))}x "
          f"({fmt(med([s['med_first_frac'] for s in good]) and med([s['med_first_frac'] for s in good]) * 100, '.0f')}%) | "
          f"kalah median {fmt(med([s['med_loss'] for s in good]))}x | pegang {fmt(med([s['med_hold'] for s in good]) and med([s['med_hold'] for s in good]) / 60, '.1f')} mnt")
    if delay is not None and delay < 5:
        print("  Catatan: mereka masuk dalam hitungan detik pertama. Kecepatan seperti itu butuh node khusus + bundle/tip;")
        print("  simulator kita menerima kabar token 1-3 dtk lebih lambat, jadi pola ini TIDAK bisa ditiru secara adil.")
    print("  Ingat: dompet ini dipilih karena terlihat bagus di jendela pendek -> bias penyintas. Uji dulu sebagai varian simulasi.")


# ---------------------------------------------------------------- backtest pola sniper
# filter pemilihan koin yang terlihat pada dompet sniper yang untung (semua cirinya sudah kita rekam saat koin lahir)
PICK_RULES = {
    "semua koin": lambda r: True,
    "dev beli >=0,5 SOL": lambda r: (r["dev_sol"] or 0) >= 0.5,
    "punya X": lambda r: bool(r["tw"]),
    "bukan dev serial": lambda r: (r["creator_n24"] or 0) == 0,
    "POLA SNIPER (ketiganya)": lambda r: (r["dev_sol"] or 0) >= 0.5 and r["tw"] and (r["creator_n24"] or 0) == 0,
    "POLA SNIPER + dev >=1 SOL": lambda r: (r["dev_sol"] or 0) >= 1 and r["tw"] and (r["creator_n24"] or 0) == 0,
}
# cara keluar: scalping cepat ala sniper vs aturan jual standar simulator
EXIT_RULES = {
    "scalp 1,25x / SL 15% / 1 mnt": dict(stop_loss=0.15, tp1_mult=1.25, tp1_sell=1.0, max_hold_min=1, stale_min=0.5),
    "scalp 1,5x / SL 20% / 3 mnt": dict(stop_loss=0.20, tp1_mult=1.5, tp1_sell=1.0, max_hold_min=3, stale_min=1),
    "standar (2x lalu trailing)": {},
}


def backtest(entries):
    """Uji offline: koin dipilih dengan filter ala sniper, dibeli di detik T, dijual dengan aturan scalping.
    Memakai jejak harga yang sudah direkam + biaya realistis (fee, slippage dinamis, biaya jaringan) dari meme_ai."""
    from types import SimpleNamespace
    from meme_ai import simulate
    s = load_settings()
    tx_frac = s.tx_cost_sol * s.sol_usd_fallback / s.per_coin_usd
    base = {k: getattr(s, k) for k in ("stop_loss", "tp1_mult", "tp1_sell", "tp2_mult", "tp2_sell", "trailing_stop",
                                       "max_hold_min", "stale_min")}
    sim = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    sim.row_factory = sqlite3.Row
    rows = [dict(r) for r in sim.execute("""SELECT * FROM token_features WHERE ver = 2 AND finalized = 1 AND path IS NOT NULL
                                            AND meta_ok = 1 ORDER BY created""")]
    sim.close()
    paths = {r["mint"]: json.loads(r["path"]) for r in rows}
    cut = rows[int(len(rows) * 0.7)]["created"] if rows else 0
    print(f"{len(rows)} token dengan data lengkap (termasuk link X). Biaya: fee {s.fee_pct:.1%}/sisi + slippage + jaringan.")
    print("Kolom 'lama' = 70% token terlama, 'baru' = 30% terbaru: pola yang sungguhan harus untung di KEDUANYA.\n")
    best = None
    for T in entries:
        for ename, ex in EXIT_RULES.items():
            c = SimpleNamespace(**{**base, **ex})
            print(f"== masuk detik ke-{T}, jual: {ename}")
            print(f"   {'filter':<27}{'n':>6}{'rata2':>8}{'median':>8}{'untung':>8}{'lama':>8}{'baru':>8}")
            for pname, rule in PICK_RULES.items():
                res = [(r["created"], simulate(paths[r["mint"]], T, c, s, tx_frac)) for r in rows if rule(r)]
                res = [(t, x) for t, x in res if x is not None]
                if len(res) < 30:
                    print(f"   {pname:<27}{len(res):>6}   (terlalu sedikit)")
                    continue
                xs = [x for _, x in res]
                old = [x for t, x in res if t < cut]
                new = [x for t, x in res if t >= cut]
                avg = lambda v: sum(v) / len(v) if v else float("nan")
                print(f"   {pname:<27}{len(xs):>6}{avg(xs):>7.3f}x{stx.median(xs):>7.2f}x{sum(x > 1 for x in xs) / len(xs):>8.0%}"
                      f"{avg(old):>7.3f}x{avg(new):>7.3f}x")
                if pname != "semua koin" and len(new) >= 30 and (best is None or min(avg(old), avg(new)) > best[0]):
                    best = (min(avg(old), avg(new)), T, ename, pname, len(xs))
            print()
    if best:
        verdict = "LAYAK DIUJI sebagai varian simulator" if best[0] > 1.02 else "BELUM UNTUNG setelah biaya"
        print(f"Kombinasi terbaik (dinilai dari periode TERBURUKNYA): {best[3]} + {best[2]}, masuk detik ke-{best[1]}, n={best[4]}")
        print(f"  -> {best[0]:.3f}x di periode terburuk: {verdict}")


# ---------------------------------------------------------------- uji copy-trade
COPY_FEE_SOL = 0.0005    # priority fee + tip per transaksi bot copy-trade (perlu cepat supaya tidak tertinggal jauh)


def trade_price(raw, mint):
    """Harga riil satu transaksi: perubahan SOL / perubahan token milik POOL itu sendiri (bonding curve atau
    pool PumpSwap), sebelum fee. Tetap benar walau satu transaksi berisi beberapa pembelian sekaligus."""
    meta = (raw or {}).get("meta")
    if not meta or meta.get("err") is not None:
        return None
    by_owner = {}
    payer = raw["transaction"]["message"]["accountKeys"][0]
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in meta.get(side) or []:
            if b["mint"] in (mint, WSOL) and b.get("owner") != payer:   # pedagangnya sendiri bukan pool
                d = by_owner.setdefault(b.get("owner"), {})
                d[b["mint"]] = d.get(b["mint"], 0.0) + sign * float(b["uiTokenAmount"].get("uiAmount") or 0)
    # pool = pemilik dengan perubahan token koin ini terbesar (saat orang membeli, token pool berkurang)
    owner, d = max(by_owner.items(), key=lambda kv: abs(kv[1].get(mint, 0)), default=(None, {}))
    tok = d.get(mint, 0)
    if not owner or abs(tok) < 1e-9:
        return None
    sol = d.get(WSOL, 0.0)              # pool PumpSwap memegang wSOL
    if abs(sol) < 1e-12:                # bonding curve memegang SOL biasa di akunnya sendiri
        msg, loaded = raw["transaction"]["message"], meta.get("loadedAddresses") or {}
        keys = msg["accountKeys"] + loaded.get("writable", []) + loaded.get("readonly", [])
        if owner not in keys:
            return None
        i = keys.index(owner)
        sol = (meta["postBalances"][i] - meta["preBalances"][i]) / 1e9
    # SOL dan token pool harus bergerak berlawanan arah; transaksi debu (<0,01 SOL) tidak mewakili harga
    if abs(sol) < 0.01 or (sol > 0) == (tok > 0):
        return None
    return abs(sol) / abs(tok)


class Prices:
    """Harga transaksi PERTAMA di koin itu pada slot >= target (= harga yang didapat bot yang telat d slot)."""
    def __init__(self, rpc, db):
        self.rpc, self.db = rpc, db
        db.execute("CREATE TABLE IF NOT EXISTS px (sig TEXT, mint TEXT, price REAL, PRIMARY KEY (sig, mint))")

    async def price(self, sig, mint):
        r = self.db.execute("SELECT price FROM px WHERE sig = ? AND mint = ?", (sig, mint)).fetchone()
        if r:
            return r[0]
        raw = await self.rpc("getTransaction", [sig, {"encoding": "json", "maxSupportedTransactionVersion": 1,
                                                      "commitment": "confirmed"}])
        pr = trade_price(raw, mint)
        self.db.execute("INSERT OR REPLACE INTO px VALUES (?,?,?)", (sig, mint, pr))
        return pr

    async def window(self, mint, before, until):
        """Semua transaksi koin di antara 2 tanda tangan, urut dari yang terlama. None = terlalu ramai."""
        out, cursor = [], before
        for _ in range(MAX_SIG_PAGES):
            page = await self.rpc("getSignaturesForAddress", [mint, {"limit": 1000, "before": cursor, "until": until}])
            out += page
            if len(page) < 1000:
                return [x for x in reversed(out) if x.get("err") is None]
            cursor = page[-1]["signature"]
        return None

    async def first_after(self, sigs, mint, slot, skip=()):
        for x in [x for x in sigs if x["slot"] >= slot and x["signature"] not in skip][:4]:
            pr = await self.price(x["signature"], mint)
            if pr:
                return pr
        return None


# SOP keluar bertahap. Angka bisa diubah: (kelipatan dari harga beli KITA, porsi dari jumlah awal yang dijual)
SOP_LADDER = ((1.2, 0.10), (1.5, 0.25), (2.0, 0.25))
SOP_TRAIL = 0.25         # setelah 60% terjual & modal belum kembali: jual sisa jika turun 25% dari puncak
SAMPLE_SLOTS = 3         # jejak harga: 1 transaksi tiap ~3 slot (~1,2 dtk)
MAX_PATH = 80            # paling banyak 80 titik harga per putaran (hemat kuota)


def sop_exit(path, p_in, exit_slot, p_exit, stake, fee, slip):
    """SOP kita, dijalankan di jejak harga sesudah kita membeli:
    1. jual 10% di 1,2x, 25% di 1,5x, 25% di 2x (dari jumlah awal) -> 60% terjual
    2. setelah 60%: trailing stop untuk sisanya; kalau harga masih naik ke puncak baru, jual secukupnya
       supaya modal awal + semua fee kembali
    3. setelah modal kembali: sisanya ditahan sampai trader yang kita copy keluar
    Belum kena tangga sama sekali -> ikut keluar saat leader keluar.
    Setiap order dieksekusi di titik harga BERIKUTNYA (bot butuh waktu). Hasil: (kelipatan modal, alasan)."""
    keep = 1 - fee - slip
    qty0 = stake * keep / p_in
    st = {"rem": qty0, "back": 0.0}

    def sell(q, px):
        q = min(q, st["rem"])
        if q > 1e-12:
            st["rem"] -= q
            st["back"] += q * px * keep - COPY_FEE_SOL

    need = stake + COPY_FEE_SOL           # modal + biaya transaksi beli
    level, peak, recovered, pending, reason = 0, p_in, False, None, "ikut leader"
    for slot, px in path:
        if slot >= exit_slot:
            break
        if pending:
            kind, q = pending
            pending = None
            if kind == "recover":         # hitung di harga eksekusi: cukup untuk menutup modal + fee
                q = (need - st["back"] + COPY_FEE_SOL) / (px * keep)
                recovered = True
            sell(q, px)
            if kind == "trail":
                return (st["back"] - COPY_FEE_SOL) / stake, "trailing"
        peak = max(peak, px)
        if level < len(SOP_LADDER) and px >= SOP_LADDER[level][0] * p_in:
            pending = ("ladder", qty0 * SOP_LADDER[level][1])
            level += 1
            reason = f"tangga {level}"
        elif level == len(SOP_LADDER) and not recovered:
            if st["back"] >= need:
                recovered = True
            elif px >= peak and px > SOP_LADDER[-1][0] * p_in:
                pending = ("recover", 0)
            elif px <= peak * (1 - SOP_TRAIL):
                pending = ("trail", st["rem"])
        if recovered:
            reason = "modal kembali, sisa ditahan"
    sell(st["rem"], p_exit)               # sisanya keluar bersama leader
    return (st["back"] - COPY_FEE_SOL) / stake, reason


def follow_exit(sells, outs, p_in, stake, fee, slip):
    """Cara lama: ikut menjual porsi yang sama setiap kali leader menjual."""
    keep = 1 - fee - slip
    qty0, frac = stake * keep / p_in, sum(f for _, _, f in sells)
    back = sum(qty0 * f / frac * po * keep - COPY_FEE_SOL for (_, _, f), po in zip(sells, outs))
    return (back - COPY_FEE_SOL) / stake


async def price_path(prices, sigs, mint, start_slot, end_slot):
    pick, last = [], -10 ** 12
    span = [x for x in sigs if start_slot <= x["slot"] <= end_slot]
    step = max(SAMPLE_SLOTS, (end_slot - start_slot) // MAX_PATH)
    for x in span:
        if x["slot"] >= last + step:
            pick.append(x)
            last = x["slot"]
    out = []
    for x in pick:
        pr = await prices.price(x["signature"], mint)
        if pr:
            out.append((x["slot"], pr))
    return out


async def copytest(rpc, db, wallets, n_trips, delays, stake, since=None):
    s = load_settings()
    prices = Prices(rpc, db)
    fee, slip = s.fee_pct, s.slippage_min
    if not wallets:
        prof = [(r["wallet"], json.loads(r["stats"])) for r in db.execute("SELECT * FROM profiles")]
        wallets = [w for w, st in sorted(prof, key=lambda x: -(x[1].get("pnl_sol") or -1e9))
                   if st.get("n_closed", 0) >= MIN_TRIPS][:5]
    if not wallets:
        sys.exit("Belum ada profil dompet. Jalankan dulu: discover lalu profile")
    ladder = ", ".join(f"{q:.0%} di {m}x" for m, q in SOP_LADDER)
    print(f"Copy-trade {len(wallets)} dompet, modal {stake} SOL per koin, fee {fee:.1%}/sisi + slippage {slip:.1%} + "
          f"{COPY_FEE_SOL * 1000:.1f} mSOL/transaksi. Jeda dalam slot (1 slot ~ 0,4 dtk).")
    print(f"SOP: {ladder}, lalu trailing {SOP_TRAIL:.0%} / tutup modal + fee, sisa ditahan sampai leader keluar.")
    total = {d: {"follow": [], "sop": [], "why": []} for d in delays}
    lead_all = []
    for w in wallets:
        rows = [{**dict(r), "tokens": json.loads(r["tokens"])} for r in db.execute(
            "SELECT tx.* FROM tx JOIN wallet_sigs ws ON ws.sig = tx.sig WHERE ws.wallet = ? AND tx.wallet = ?", (w, w))]
        own = sorted((r["slot"], r["sig"]) for r in rows)
        trips = [t for t in build_trips(rows) if t["closed"] and t["buy_sig"]]
        if since:
            trips = [t for t in trips if t["t_buy"] >= since[w]]
        trips = sorted(trips, key=lambda t: t["t_buy"])[-n_trips:]
        res = {d: {"follow": [], "sop": [], "why": []} for d in delays}
        lead, skipped = [], 0
        log(f"{w[:8]}...: menguji {len(trips)} putaran" + (" SESUDAH dipilih" if since else " terakhir"))
        for t in trips:
            last_slot = max(sl for _, sl, _ in t["sells"])
            nxt = next((sig for sl, sig in own if sl > last_slot + max(delays) + 5), None)
            sigs = await prices.window(t["mint"], nxt, t["buy_sig"]) if nxt else None
            if not sigs:
                skipped += 1
                continue
            mine = {t["buy_sig"], *(sig for sig, _, _ in t["sells"])}
            p_lead_in = await prices.price(t["buy_sig"], t["mint"])
            lead_out = [await prices.price(sig, t["mint"]) for sig, _, _ in t["sells"]]
            frac = sum(f for _, _, f in t["sells"])
            if p_lead_in and all(lead_out):
                lead.append(sum(f * po for (_, _, f), po in zip(t["sells"], lead_out)) / frac / p_lead_in)
            path = await price_path(prices, sigs, t["mint"], t["buy_slot"] + 1, last_slot + max(delays))
            for d in delays:
                p_in = await prices.first_after(sigs, t["mint"], t["buy_slot"] + d, mine)
                outs = [await prices.first_after(sigs, t["mint"], sl + d, mine) for _, sl, _ in t["sells"]]
                if not p_in or not all(outs):
                    continue
                res[d]["follow"].append(follow_exit(t["sells"], outs, p_in, stake, fee, slip))
                ret, why = sop_exit([x for x in path if x[0] > t["buy_slot"] + d], p_in, last_slot + d, outs[-1],
                                    stake, fee, slip)
                res[d]["sop"].append(ret)
                res[d]["why"].append(why)
            db.commit()
        print_copy(w, res, lead, skipped)
        for d in delays:
            for k in total[d]:
                total[d][k] += res[d][k]
        lead_all += lead
    if len(wallets) > 1:
        print_copy("SEMUA DOMPET", total, lead_all, 0)


def print_copy(name, res, lead, skipped):
    print(f"\n=== {name}" + (f"   ({skipped} putaran dilewati: koin terlalu ramai / data kurang)" if skipped else ""))
    if lead:
        print(f"  leader sendiri (harga curve, sebelum biaya): rata-rata {sum(lead) / len(lead):.3f}x, "
              f"untung {sum(x > 1 for x in lead) / len(lead):.0%}, n={len(lead)}")
    if not any(r["follow"] for r in res.values()):
        print("  belum ada putaran yang bisa diuji")
        return
    print(f"  {'kita ikut telat':<17}{'n':>5} | {'IKUT LEADER':^24} | {'SOP BERTAHAP':^24} | SOP kena tangga")
    print(f"  {'':<17}{'':>5} | {'rata2':>8}{'median':>8}{'untung':>8} | {'rata2':>8}{'median':>8}{'untung':>8} |")
    for d, r in res.items():
        a, b = r["follow"], r["sop"]
        if not a:
            continue
        hit = sum(w != "ikut leader" for w in r["why"]) / len(r["why"])
        cols = lambda xs: f"{sum(xs) / len(xs):>7.3f}x{stx.median(xs):>7.2f}x{sum(x > 1 for x in xs) / len(xs):>8.0%}"
        print(f"  {d:>3} slot (~{d * 0.4:4.1f}s){len(a):>5} | {cols(a)} | {cols(b)} | {hit:>6.0%}")


async def pick_leaders(db, top):
    prof = [(r["wallet"], json.loads(r["stats"])) for r in db.execute("SELECT * FROM profiles")]
    best = [(w, st) for w, st in sorted(prof, key=lambda x: -(x[1].get("pnl_sol") or -1e9))
            if st.get("n_closed", 0) >= MIN_TRIPS and (st.get("roi") or 0) > 1][:top]
    if not best:
        sys.exit("Belum ada dompet untung dengan cukup putaran. Jalankan discover + profile dulu.")
    now = time.time()
    db.executemany("INSERT OR REPLACE INTO leaders VALUES (?,?,?)", [(w, now, json.dumps(st)) for w, st in best])
    db.commit()
    print(f"Leader dipilih {time.strftime('%Y-%m-%d %H:%M')} (hanya dari data SEBELUM saat ini):")
    for w, st in best:
        print(f"  {w}  PnL {st['pnl_sol']:+.2f} SOL, ROI {st['roi']:.2f}x, {st['n_closed']} putaran")
    print("Jalankan 'python wallet_lab.py forward' beberapa jam / hari lagi untuk menguji transaksi BARU mereka.")


async def forward(rpc, db, n_trips, delays, stake, txs):
    leaders = {r["wallet"]: r["picked"] for r in db.execute("SELECT wallet, picked FROM leaders")}
    if not leaders:
        sys.exit("Belum ada leader. Jalankan dulu: python wallet_lab.py pick")
    for w in leaders:
        log(f"mengambil transaksi baru {w[:8]}... (dipilih {(time.time() - leaders[w]) / 3600:.1f} jam lalu)")
        await profile_wallet(rpc, db, w, txs, newest_only=True)
    print("FORWARD TEST: hanya transaksi yang terjadi SESUDAH leader dipilih (tanpa bias pemilihan).")
    await copytest(rpc, db, list(leaders), n_trips, delays, stake, since=leaders)


async def main_async(args):
    s = load_settings()
    if not s.helius_api_key:
        sys.exit("HELIUS_API_KEY kosong di .env: Wallet Lab butuh Helius (RPC publik terlalu dibatasi).")
    db = open_lab()
    rpc = Rpc(s.rpc_url)
    try:
        if args.cmd == "discover":
            await discover(rpc, db, args.tokens, args.early)
        elif args.cmd == "profile":
            wallets = [r[0] for r in db.execute("""SELECT wallet, COUNT(*) n FROM early_buys GROUP BY wallet
                                                   HAVING n >= ? ORDER BY n DESC, AVG(d_sec) LIMIT ?""", (args.min_hits, args.top))]
            if not wallets:
                sys.exit("Belum ada kandidat. Jalankan dulu: python wallet_lab.py discover")
            for i, w in enumerate(wallets, 1):
                log(f"[{i}/{len(wallets)}] membedah {w}")
                print_profile(w, await profile_wallet(rpc, db, w, args.txs))
        elif args.cmd == "copytest":
            await copytest(rpc, db, args.wallets, args.trips, args.delays, args.stake)
        elif args.cmd == "pick":
            await pick_leaders(db, args.top)
        elif args.cmd == "forward":
            await forward(rpc, db, args.trips, args.delays, args.stake, args.txs)
        elif args.cmd == "wallet":
            print_profile(args.address, await profile_wallet(rpc, db, args.address, args.txs))
        log(f"selesai, {rpc.calls} panggilan RPC")
    finally:
        await rpc.close()
        db.close()


def main():
    ap = argparse.ArgumentParser(description="Bedah strategi dompet sniper pump.fun (hanya membaca data publik)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover", help="cari dompet yang berulang kali beli paling awal")
    d.add_argument("--tokens", type=int, default=40)
    d.add_argument("--early", type=int, default=25, help="berapa transaksi pertama tiap token yang dibaca")
    p = sub.add_parser("profile", help="bedah dompet kandidat hasil discover")
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--min-hits", type=int, default=2)
    p.add_argument("--txs", type=int, default=400)
    w = sub.add_parser("wallet", help="bedah satu dompet publik")
    w.add_argument("address")
    w.add_argument("--txs", type=int, default=400)
    sub.add_parser("report", help="ringkasan semua profil + pola dompet yang untung")
    b = sub.add_parser("backtest", help="uji offline filter & cara jual ala sniper pada jejak harga yang sudah direkam")
    b.add_argument("--entry", type=int, nargs="+", default=[3, 10, 30], help="detik masuk setelah koin muncul")
    c = sub.add_parser("copytest", help="uji copy-trade dompet teratas dengan harga on-chain sungguhan")
    c.add_argument("wallets", nargs="*", help="kosong = 5 dompet dengan PnL tertinggi di profil")
    c.add_argument("--trips", type=int, default=60, help="berapa putaran terakhir per dompet")
    c.add_argument("--delays", type=int, nargs="+", default=[1, 3, 10, 25], help="jeda dalam slot")
    c.add_argument("--stake", type=float, default=0.25, help="modal SOL per koin")
    k = sub.add_parser("pick", help="kunci daftar leader sekarang untuk forward test")
    k.add_argument("--top", type=int, default=5)
    f = sub.add_parser("forward", help="uji copy-trade hanya pada transaksi leader SESUDAH dipilih")
    f.add_argument("--trips", type=int, default=200)
    f.add_argument("--delays", type=int, nargs="+", default=[1, 3, 10, 25])
    f.add_argument("--stake", type=float, default=0.25)
    f.add_argument("--txs", type=int, default=3000, help="maksimal transaksi baru yang diambil per leader")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.cmd == "backtest":
        backtest(args.entry)
    elif args.cmd == "report":
        db = open_lab()
        report(db)
        db.close()
    else:
        asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
