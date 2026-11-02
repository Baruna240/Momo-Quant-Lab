"""Perekam aliran transaksi per token meme (Fase 3): bahan deteksi bundle/sniper, wash trading, dev dump, dan label rug.

Sumber: PumpPortal subscribeTokenTrade di koneksi WebSocket yang SAMA dengan feed token baru (sim.py).
Setiap token baru yang lolos anti-spam diikuti selama 20 menit:
- 5 menit pertama: setiap transaksi disimpan mentah (detik ke-, dompet, beli/jual, SOL, token, harga)
- ringkasan di detik ke-10/30/60/120/300: jumlah beli/jual, SOL masuk/keluar, pembeli unik, porsi pembeli terbesar,
  sniper (beli <= 3 detik setelah lahir), beli berukuran identik (tanda bot/bundle), dompet yang beli lalu jual
  (tanda wash trading), dev sudah jual berapa persen, sniper "langganan" (dompet yang juga sniping banyak koin lain)
- SOL yang masuk SEBELUM langganan kita aktif (missed_sol): pembelian di blok yang sama dengan pembuatan token
  tidak terlihat per dompet, tapi volumenya terukur dari saldo bonding curve -> indikator bundle
- menit ke-20: label hasil yang konsisten (rug / dump / lulus / datar / normal) untuk melatih & menguji Safety
Data disimpan di flow.db (terpisah dari simulasi.db supaya backup harian tetap kecil). Hanya membaca data publik.
"""
import asyncio
import json
import sqlite3
import statistics as stx
import time

from config import BASE_DIR

FLOW_DB = BASE_DIR / "flow.db"
RAW_SEC = 300                  # transaksi mentah disimpan untuk 5 menit pertama
TRACK_SEC = 20 * 60            # diikuti sampai menit ke-20, lalu berhenti berlangganan
SNAPS = (10, 30, 60, 120, 300)
SNIPE_SEC = 3.0                # beli dalam 3 detik pertama = sniper / bundle
MAX_RAW = 1500                 # batas transaksi mentah per token (koin super ramai)
SERIAL_WINDOW = 3600           # sniper "langganan": ikut sniping >= 3 koin lain dalam 1 jam terakhir
SERIAL_MIN = 3
RETAIN_RAW_DAYS = 21           # transaksi mentah lebih tua dari ini dihapus (ringkasan & label tetap)
# mode sampel (tanpa API key PumpPortal): sebagian token dibaca ulang dari blockchain lewat RPC publik Solana
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
SAMPLE_EVERY = 8               # 1 dari 8 token baru (anggaran batas RPC publik)
SAMPLE_AT = 75                 # baca ulang transaksi 60 detik pertama saat token berumur 75 detik
SAMPLE_WINDOW = 60
SAMPLE_MAX_TX = 40             # paling banyak 40 transaksi per token (hemat batas RPC publik)
PUBLIC_RPC_INTERVAL = 1.1      # RPC publik menolak > ~10 panggilan / 10 dtk per jenis: jeda tetap antar panggilan


def connect(path=FLOW_DB):
    db = sqlite3.connect(path, timeout=10)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS token_trades (mint TEXT, age REAL, trader TEXT, side INTEGER, sol REAL, tokens REAL,
                                             price REAL, created REAL);
    CREATE INDEX IF NOT EXISTS token_trades_mint ON token_trades(mint);
    CREATE INDEX IF NOT EXISTS token_trades_created ON token_trades(created);
    CREATE TABLE IF NOT EXISTS token_flow (mint TEXT PRIMARY KEY, symbol TEXT, created REAL, creator TEXT,
        dev_tokens REAL, launch_price REAL, missed_sol REAL, first_age REAL, n_trades INTEGER, n_traders INTEGER,
        snaps TEXT, peak_mult REAL, t_peak REAL, trough_after_peak REAL, final_mult REAL, dev_sold_frac REAL,
        dev_first_sell_age REAL, graduated INTEGER, label TEXT, safety60 REAL, safety60_reasons TEXT, finalized REAL);
    CREATE TABLE IF NOT EXISTS sample_log (ts REAL, mint TEXT, kind TEXT, ok INTEGER, secs REAL, calls INTEGER, err TEXT);
    CREATE TABLE IF NOT EXISTS ws_messages (ts REAL, message TEXT, count INTEGER, PRIMARY KEY (message));
    """)
    db.commit()
    return db


def snapshot(raw, T, creator, dev_tokens, serial):
    """Ringkasan aliran transaksi sampai detik ke-T. raw: [(age, trader, side, sol, tokens, price)] urut waktu."""
    tr = [r for r in raw if r[0] <= T]
    buys = [r for r in tr if r[2] > 0]
    sells = [r for r in tr if r[2] < 0]
    buy_sol, sell_sol = sum(r[3] for r in buys), sum(r[3] for r in sells)
    per_buyer = {}
    for r in buys:
        per_buyer[r[1]] = per_buyer.get(r[1], 0.0) + r[3]
    buyers, sellers = set(per_buyer), {r[1] for r in sells}
    snipers = {r[1] for r in buys if r[0] <= SNIPE_SEC and r[1] != creator}
    sizes = {}
    for r in buys:
        k = round(r[3], 3)
        sizes[k] = sizes.get(k, 0) + 1
    both = buyers & sellers
    vol = buy_sol + sell_sol
    wash_vol = sum(r[3] for r in tr if r[1] in both)
    dev_sold = sum(r[4] for r in sells if r[1] == creator)
    return {
        "n_buy": len(buys), "n_sell": len(sells), "buy_sol": round(buy_sol, 4), "sell_sol": round(sell_sol, 4),
        "buyers": len(buyers), "sellers": len(sellers),
        "buy_ratio": round(buy_sol / vol, 4) if vol else None,
        "top1_share": round(max(per_buyer.values()) / buy_sol, 4) if buy_sol else None,
        "snipers": len(snipers), "sniper_sol": round(sum(r[3] for r in buys if r[1] in snipers and r[0] <= SNIPE_SEC), 4),
        "same_size": round(max(sizes.values()) / len(buys), 4) if len(buys) >= 3 else None,
        "round_trippers": len(both), "wash_share": round(wash_vol / vol, 4) if vol else None,
        "dev_sell_frac": round(min(1.0, dev_sold / dev_tokens), 4) if dev_tokens else None,
        "serial_snipers": len(snipers & serial),
        "price": tr[-1][5] if tr else None,
    }


def label_token(peak_mult, trough_after_peak, final_mult, dev_sold_frac, graduated):
    """Label hasil 20 menit, dipakai untuk menilai apakah pemeriksaan keamanan benar-benar berguna.
    trough_after_peak = harga terendah SETELAH puncak dibagi harga puncak (0,1 = jatuh 90% dari puncak)."""
    if graduated:
        return "lulus"
    if (dev_sold_frac or 0) >= 0.5 and trough_after_peak is not None and trough_after_peak <= 0.25:
        return "rug"
    if trough_after_peak is not None and trough_after_peak <= 0.2 and peak_mult >= 1.3:
        return "dump"
    if peak_mult < 1.3:
        return "datar"
    return "normal"


class FlowTracker:
    def __init__(self, path=FLOW_DB, clock=time.time, sample_every=0):
        self.db = connect(path)
        self.clock = clock
        self.sample_every = sample_every      # 0 = mode stream (PumpPortal); > 0 = mode sampel lewat RPC publik
        self.counter = 0
        self.jobs = None                      # antrean baca-ulang transaksi awal (dibuat saat worker berjalan)
        self.dev_jobs = None                  # antrean cek saldo dev (jalur sendiri supaya tidak tertahan)
        self.sample_ok = self.sample_fail = 0
        self.items = {}
        self.to_sub, self.to_unsub = [], []
        self.snipe_log = {}            # dompet -> [waktu ikut sniping], untuk sniper "langganan"
        self.trades_seen = 0
        self.last_retention = 0.0

    # ---------------------------------------------------------- dari feed
    def start(self, token, creator, dev_tokens, launch_v_sol=None, ctx=None, now=None):
        now = self.clock() if now is None else now
        if token.mint in self.items:
            return
        if self.sample_every:
            self.counter += 1
            if self.counter % self.sample_every:
                return
        self.items[token.mint] = {"sample": bool(self.sample_every), "queued": False, "raw_ready": not self.sample_every,
                                  "dev_done": not self.sample_every, "dev_queued": False,"token": token, "created": now, "creator": creator or "", "dev_tokens": dev_tokens or 0.0,
                                  "launch_v_sol": launch_v_sol, "ctx": dict(ctx or {}),
                                  "raw": [], "missed": None, "first_age": None, "n": 0, "traders": set(),
                                  "peak": 1.0, "t_peak": 0.0, "trough": None, "last": 1.0, "dev_sold": 0.0,
                                  "dev_first": None, "grad": False, "safety": None}
        self.to_sub.append(token.mint)

    def on_trade(self, d, now=None):
        it = self.items.get(d.get("mint"))
        if not it:
            return
        now = self.clock() if now is None else now
        side = 1 if d.get("txType") == "buy" else -1 if d.get("txType") == "sell" else 0
        if not side:
            return
        self.trades_seen += 1
        age = now - it["created"]
        sol, tok = float(d.get("solAmount") or 0), float(d.get("tokenAmount") or 0)
        v_sol, v_tok = float(d.get("vSolInBondingCurve") or 0), float(d.get("vTokensInBondingCurve") or 0)
        trader = d.get("traderPublicKey") or ""
        launch = it["token"].launch_price
        price = v_sol / v_tok if v_tok else (sol / tok if tok else 0.0)
        if d.get("pool") and d.get("pool") != "pump":
            it["grad"] = True
        if it["missed"] is None and v_sol:
            # SOL di curve sebelum transaksi pertama yang kita lihat, dikurangi saldo saat lahir = dibeli tanpa terlihat
            launch_v_sol = it["launch_v_sol"]
            before = v_sol - side * sol
            it["missed"] = max(0.0, before - launch_v_sol) if launch_v_sol else None
            it["first_age"] = round(age, 2)
        it["n"] += 1
        it["traders"].add(trader)
        if age <= RAW_SEC and len(it["raw"]) < MAX_RAW:
            it["raw"].append((round(age, 2), trader, side, sol, tok, price))
            if side > 0 and age <= SNIPE_SEC and trader != it["creator"]:
                hist = self.snipe_log.setdefault(trader, [])
                hist[:] = [t for t in hist if now - t < SERIAL_WINDOW]
                hist.append(now)
        m = price / launch if launch else 0.0
        if m > it["peak"]:
            it["peak"], it["t_peak"], it["trough"] = m, age, None
        elif it["peak"] > 0:
            r = m / it["peak"]
            it["trough"] = r if it["trough"] is None else min(it["trough"], r)
        it["last"] = m
        if side < 0 and trader == it["creator"]:
            it["dev_sold"] += tok
            if it["dev_first"] is None:
                it["dev_first"] = round(age, 2)

    def serial_set(self, now):
        return {w for w, h in self.snipe_log.items() if sum(now - t < SERIAL_WINDOW for t in h) >= SERIAL_MIN}

    # ---------------------------------------------------------- berkala
    def tick(self, now=None, safety_fn=None):
        """Panggil berkala: hitung skor Safety di detik ke-60, simpan & berhenti mengikuti token yang sudah 20 menit."""
        now = self.clock() if now is None else now
        serial = None
        for mint, it in list(self.items.items()):
            age = now - it["created"]
            if it["sample"]:
                self._sample_step(mint, it, age)
            if it["safety"] is None and age >= 60 and it["raw_ready"] and safety_fn:
                serial = serial if serial is not None else self.serial_set(now)
                snap = snapshot(it["raw"], 60, it["creator"], it["dev_tokens"], serial)
                it["safety"] = safety_fn(snap, it)
            if age >= TRACK_SEC and (it["dev_done"] or age >= TRACK_SEC + 300):
                serial = serial if serial is not None else self.serial_set(now)
                self.save(mint, it, serial, now)
        if len(self.snipe_log) > 50000:
            self.snipe_log = {w: h for w, h in self.snipe_log.items() if h and now - h[-1] < SERIAL_WINDOW}
        if now - self.last_retention > 6 * 3600:
            self.last_retention = now
            self.db.execute("DELETE FROM token_trades WHERE created < ?", (now - RETAIN_RAW_DAYS * 86400,))
            self.db.commit()

    # ---------------------------------------------------------- mode sampel (RPC publik, gratis)
    def _sample_step(self, mint, it, age):
        """Harga puncak/lembah dari harga yang dipantau simulator; antrekan baca-ulang transaksi & cek saldo dev."""
        t = it["token"]
        if t.launch_price and t.price:
            m = t.price / t.launch_price
            if m > it["peak"]:
                it["peak"], it["t_peak"], it["trough"] = m, age, None
            elif it["peak"] > 0:
                r = m / it["peak"]
                it["trough"] = r if it["trough"] is None else min(it["trough"], r)
            it["last"] = m
        it["grad"] = it["grad"] or bool(getattr(t, "complete", False))
        if self.jobs is None:
            return
        if not it["queued"] and age >= SAMPLE_AT:
            it["queued"] = True
            self.jobs.put_nowait(("early", mint))
        if not it["dev_queued"] and age >= TRACK_SEC and self.dev_jobs is not None:
            it["dev_queued"] = True
            self.dev_jobs.put_nowait(("dev", mint))

    async def sample_worker(self, rpc):
        """Dua jalur kerja mode sampel: baca-ulang transaksi awal, dan cek saldo dev di menit ke-20."""
        self.jobs, self.dev_jobs = asyncio.Queue(), asyncio.Queue()
        await asyncio.gather(self._lane(rpc, self.jobs), self._lane(rpc, self.dev_jobs))

    async def _lane(self, rpc, queue):
        while True:
            kind, mint = await queue.get()
            it = self.items.get(mint)
            if not it:
                continue
            t0, calls0, err, ok = time.time(), getattr(rpc, "calls", 0), None, 0
            try:
                if kind == "early":
                    await self._backfill(rpc, mint, it)
                else:
                    res = await rpc("getTokenAccountsByOwner", [it["creator"], {"mint": mint}, {"encoding": "jsonParsed"}])
                    bal = sum(float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0) for a in res["value"])
                    it["dev_sold"] = max(it["dev_sold"], it["dev_tokens"] - bal)
                self.sample_ok += 1
                ok = 1
            except Exception as e:
                self.sample_fail += 1     # gagal = pakai yang sudah terlihat di transaksi awal, tidak menghambat yang lain
                err = f"{type(e).__name__}: {e}"[:200]
            finally:
                if kind == "early":
                    it["raw_ready"] = True
                else:
                    it["dev_done"] = True
                self.db.execute("INSERT INTO sample_log VALUES (?,?,?,?,?,?,?)", (time.time(), mint, kind, ok, round(time.time() - t0, 1),
                                getattr(rpc, "calls", 0) - calls0, err))
                self.db.commit()

    async def _backfill(self, rpc, mint, it):
        sigs, before = [], None
        for _ in range(3):                # koin yang sangat ramai: cukup 3000 transaksi terakhir
            page = await rpc("getSignaturesForAddress", [mint, {"limit": 1000, **({"before": before} if before else {})}])
            sigs += page
            if len(page) < 1000:
                break
            before = page[-1]["signature"]
        else:
            return                        # transaksi awalnya terlalu jauh di belakang: lewati
        sigs = [x for x in reversed(sigs) if x.get("err") is None and x.get("blockTime")]
        if not sigs:
            return
        t0, slot0 = sigs[0]["blockTime"], sigs[0]["slot"]          # transaksi pembuatan token
        early = [x for x in sigs if x["blockTime"] - t0 <= SAMPLE_WINDOW][:SAMPLE_MAX_TX]
        raw, block0 = [], 0.0
        for x in early:
            tx = await rpc("getTransaction", [x["signature"], {"encoding": "json", "maxSupportedTransactionVersion": 1}])
            tr = trade_from_tx(tx, mint)
            if not tr:
                continue
            trader, side, sol, tok, price = tr
            raw.append((float(x["blockTime"] - t0), trader, side, sol, tok, price))
            if x["slot"] == slot0 and side > 0 and trader != it["creator"]:
                block0 += sol
        it["raw"] = sorted(raw, key=lambda r: r[0])
        it["n"] = len(raw)
        it["traders"] = {r[1] for r in raw}
        it["missed"] = block0                 # mode sampel: SOL dibeli di blok yang sama dengan pembuatan token
        it["first_age"] = 0.0
        dev_sells = [r for r in raw if r[2] < 0 and r[1] == it["creator"]]
        if dev_sells:
            it["dev_first"] = min(r[0] for r in dev_sells)
            it["dev_sold"] = max(it["dev_sold"], sum(r[4] for r in dev_sells))   # cadangan bila cek saldo dev gagal

    def save(self, mint, it, serial, now, finalized=True):
        t = it["token"]
        snaps = {T: snapshot(it["raw"], T, it["creator"], it["dev_tokens"], serial) for T in SNAPS}
        dev_frac = min(1.0, it["dev_sold"] / it["dev_tokens"]) if it["dev_tokens"] else None
        label = label_token(it["peak"], it["trough"], it["last"], dev_frac, it["grad"]) if finalized else None
        score, reasons = it["safety"] or (None, [])
        self.db.execute("INSERT OR REPLACE INTO token_flow VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            mint, t.symbol, it["created"], it["creator"], it["dev_tokens"], t.launch_price, it["missed"], it["first_age"],
            it["n"], len(it["traders"]), json.dumps(snaps, separators=(",", ":")), it["peak"], it["t_peak"], it["trough"],
            it["last"], dev_frac, it["dev_first"], int(it["grad"]), label, score, json.dumps(reasons), now if finalized else None))
        self.db.executemany("INSERT INTO token_trades VALUES (?,?,?,?,?,?,?,?)",
                            [(mint, *r, it["created"]) for r in it["raw"]])
        self.db.commit()
        del self.items[mint]
        self.to_unsub.append(mint)

    def note_message(self, text, now=None):
        """Balasan PumpPortal (konfirmasi / penolakan langganan) dicatat supaya masalah key atau saldo langsung terlihat."""
        text = str(text)[:300]
        self.db.execute("""INSERT INTO ws_messages VALUES (?,?,1) ON CONFLICT(message)
                           DO UPDATE SET ts = excluded.ts, count = count + 1""", (now or self.clock(), text))
        self.db.commit()

    def take_changes(self):
        sub, unsub = self.to_sub, self.to_unsub
        self.to_sub, self.to_unsub = [], []
        return sub, unsub

    def active(self):
        return list(self.items)

    def flush(self):
        """Saat simulator berhenti: simpan yang sedang diikuti (label kosong karena belum 20 menit)."""
        now = self.clock()
        serial = self.serial_set(now)
        for mint, it in list(self.items.items()):
            if it["n"]:
                self.save(mint, it, serial, now, finalized=False)
            else:
                del self.items[mint]


class Paced:
    """Bungkus RPC dengan jeda tetap antar panggilan (dipakai bersama oleh semua jalur kerja mode sampel)."""
    def __init__(self, rpc, interval=PUBLIC_RPC_INTERVAL):
        self.rpc, self.interval, self.last, self.lock = rpc, interval, 0.0, asyncio.Lock()

    @property
    def calls(self):
        return getattr(self.rpc, "calls", 0)

    async def __call__(self, method, params):
        async with self.lock:
            wait = self.last + self.interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self.last = time.monotonic()
        return await self.rpc(method, params)


def trade_from_tx(tx, mint):
    """Transaksi mentah -> (dompet, 1 beli / -1 jual, SOL, token, harga) dari sudut pandang pembayar fee."""
    from wallet_lab import parse_tx, trade_price
    p = parse_tx(tx) if tx else None
    if not p or not p["ok"]:
        return None
    tok = p["tokens"].get(mint)
    price = trade_price(tx, mint) if tok else None
    if not tok or not price:
        return None
    return p["wallet"], (1 if tok > 0 else -1), price * abs(tok), abs(tok), price


# ---------------------------------------------------------------- laporan
def report(path=FLOW_DB):
    db = connect(path)
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute("SELECT * FROM token_flow WHERE finalized IS NOT NULL")]
    n_raw = db.execute("SELECT COUNT(*) FROM token_trades").fetchone()[0]
    print(f"Token selesai diikuti: {len(rows)} · transaksi mentah tersimpan: {n_raw:,}")
    if not rows:
        print("Belum ada token yang selesai (butuh 20 menit sejak simulator berjalan).")
        return
    labels = {}
    for r in rows:
        labels[r["label"]] = labels.get(r["label"], 0) + 1
    print("Label hasil 20 menit: " + ", ".join(f"{k} {v} ({v / len(rows):.0%})" for k, v in sorted(labels.items(), key=lambda x: -x[1])))
    missed = [r["missed_sol"] for r in rows if r["missed_sol"] is not None]
    if missed:
        print(f"SOL masuk sebelum kita bisa melihat (bundle/sniper blok-0): median {stx.median(missed):.2f} SOL, "
              f"{sum(m > 1 for m in missed) / len(missed):.0%} token > 1 SOL")
    scored = [r for r in rows if r["safety60"] is not None]
    if len(scored) >= 30:
        print("\nApakah skor Safety (detik ke-60) memisahkan rug? (skor tinggi = lebih berisiko)")
        for lo, hi in ((0, 20), (20, 40), (40, 60), (60, 101)):
            b = [r for r in scored if lo <= r["safety60"] < hi]
            if b:
                bad = sum(r["label"] in ("rug", "dump") for r in b)
                print(f"  skor {lo:>3}-{min(hi, 100):<3}: {len(b):>5} token, rug/dump {bad / len(b):.0%}")
    else:
        print(f"\nSkor Safety baru dinilai setelah >= 30 token punya skor (sekarang {len(scored)}).")


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    report()
