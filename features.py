"""Pencatat ciri koin: bahan belajar untuk model AI penilai koin.

Semua token baru yang lolos anti-spam (dan semua koin yang dibeli bot) dicatat selama 20 menit:
- kondisi di detik ke-30/60/90/180: kelipatan harga dari peluncuran (m), SOL riil di curve (s),
  penurunan terdalam (d), puncak tertinggi (h), dan berapa kali harga berubah (a = keramaian transaksi)
- ciri saat lahir (gratis, dari feed pump.fun): SOL & persen token yang dibeli dev, berapa koin lain yang
  dibuat dompet dev yang sama dalam 24 jam, berapa koin bernama sama dalam 1 jam (tanda narasi yang sedang
  ditiru), mode mayhem, panjang nama
- metadata (kalau bisa diambil): ada link X / Telegram / website, panjang deskripsi
- jejak harga lengkap (path) supaya aturan jual apa pun bisa disimulasikan ulang secara offline
- hasil akhirnya: harga tertinggi, terendah, waktu menuju 2x, lulus ke DEX, harga di menit ke-20
Belum dipakai untuk keputusan apa pun; hanya dikumpulkan.
"""
import asyncio
import json
import re
import time

SAMPLE_EVERY = 1            # catat semua token baru yang lolos anti-spam
SNAPSHOTS = (30, 60, 90, 180)
YOUNG_SEC = 200             # token semuda ini dibaca secepat koin kandidat (dapat jejak harga yang rapat)
TRACK_SEC = 20 * 60         # amati selama 20 menit
POLL_SEC = 20               # token yang lebih tua dibaca tiap 20 detik (hemat kuota Helius)
UNSEEN_DROP_SEC = 90        # buang kalau akun curve tak pernah terbaca
DATA_VERSION = 2            # 2 = data lengkap (ciri lahir, keramaian, jejak harga)
IPFS_GATEWAY = "https://gateway.pinata.cloud/ipfs/"
META_MAX_AGE = 120          # metadata yang antre lebih lama dari ini dilewati
META_WORKERS = 3

NEW_COLS = [f"{c}{s}" for s in SNAPSHOTS for c in "ha"] + [
    "dev_sol", "dev_pct", "creator_n24", "name_n1h", "mayhem", "name_len",
    "tw", "tg", "web", "desc_len", "meta_ok", "path", "ver"]


def migrate(db):
    """Tambahkan kolom baru ke tabel lama (data lama tetap, kolom barunya kosong)."""
    have = {r[1] for r in db.execute("PRAGMA table_info(token_features)")}
    for col in NEW_COLS:
        if col not in have:
            db.execute(f"ALTER TABLE token_features ADD COLUMN {col} {'TEXT' if col == 'path' else 'REAL'}")
    db.commit()


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


class FeatureTracker:
    def __init__(self, db):
        self.db = db
        migrate(db)
        self.items = {}
        self.counter = 0
        self.creators = {}      # dompet dev -> [waktu peluncuran]
        self.names = {}         # nama/simbol ternormalisasi -> [waktu peluncuran]
        self.meta_queue = asyncio.Queue()

    def on_create(self, d, now):
        """Dipanggil untuk SETIAP token baru (termasuk spam) supaya hitungan dev & nama lengkap."""
        creator = d.get("traderPublicKey") or ""
        keys = {k for k in (_norm(d.get("name")), _norm(d.get("symbol"))) if k}
        hist = self.creators.setdefault(creator, [])
        hist[:] = [t for t in hist if now - t < 86400]
        n_creator = len(hist)
        hist.append(now)
        n_name = 0
        for k in keys:
            h = self.names.setdefault(k, [])
            h[:] = [t for t in h if now - t < 3600]
            n_name = max(n_name, len(h))
            h.append(now)
        if len(self.names) > 50000:
            self.names = {k: v for k, v in self.names.items() if v and now - v[-1] < 3600}
        if len(self.creators) > 50000:
            self.creators = {k: v for k, v in self.creators.items() if v and now - v[-1] < 86400}
        v_tok = float(d.get("vTokensInBondingCurve") or 0)
        return {"dev_sol": float(d.get("solAmount") or 0),
                "dev_pct": float(d.get("initialBuy") or 0) / 1e9 * 100,
                "creator_n24": n_creator, "name_n1h": n_name,
                "mayhem": int(bool(d.get("is_mayhem_mode"))), "name_len": len(d.get("name") or ""),
                "uri": d.get("uri") or "", "v_tok0": v_tok}

    def add(self, token, bought=False, ctx=None):
        it = self.items.get(token.mint)
        if it:
            it["bought"] |= int(bought)
            return
        if not bought:
            self.counter += 1
            if self.counter % SAMPLE_EVERY:
                return
        self.items[token.mint] = {"token": token, "sampled": int(not bought), "bought": int(bought), "snap": {},
                                  "max": 1.0, "min": 1.0, "t2x": None, "ctx": dict(ctx or {}), "moves": 0,
                                  "last_p": None, "path": [], "meta": {}}
        if ctx and ctx.get("uri"):
            self.meta_queue.put_nowait((time.time(), token.mint, ctx["uri"]))

    def tokens(self):
        return [it["token"] for it in self.items.values()]

    def young(self, now):
        return [it["token"] for it in self.items.values() if now - it["token"].created < YOUNG_SEC]

    def observe(self, now):
        for mint, it in list(self.items.items()):
            t = it["token"]
            age = now - t.created
            if not t.seen:
                if age > UNSEEN_DROP_SEC:
                    del self.items[mint]
                continue
            m = t.price / t.launch_price if t.launch_price else 0.0
            if it["last_p"] is not None and t.price != it["last_p"]:
                it["moves"] += 1
            last = it["path"][-1] if it["path"] else None
            # titik jejak disimpan kalau harga bergerak >= 0,5% atau sudah 60 detik (hemat ukuran database)
            moved = last is None or not last[1] or abs(m / last[1] - 1) >= 0.005
            if moved or age - last[0] >= 60:
                it["path"].append((int(age), float(f"{m:.4g}"), round(t.real_sol, 2)))
            it["last_p"] = t.price
            it["max"] = max(it["max"], m)
            it["min"] = min(it["min"], m)
            if it["t2x"] is None and m >= 2:
                it["t2x"] = age
            for s in SNAPSHOTS:
                if s not in it["snap"] and age >= s:
                    it["snap"][s] = (m, t.real_sol, it["min"], it["max"], it["moves"])
            if age >= TRACK_SEC or (t.complete and age >= SNAPSHOTS[-1]):
                self.save(mint, it, m, finalized=1)

    async def meta_worker(self, http):
        """Ambil metadata koin (link sosial & deskripsi). Gagal = dibiarkan kosong, tidak menghambat apa pun."""
        async def worker():
            while True:
                queued, mint, uri = await self.meta_queue.get()
                it = self.items.get(mint)
                if not it or time.time() - queued > META_MAX_AGE:
                    continue
                url = uri
                cid = uri[7:] if uri.startswith("ipfs://") else uri.split("/ipfs/", 1)[1] if "/ipfs/" in uri else None
                if cid:
                    url = IPFS_GATEWAY + cid
                if not url.startswith("http"):
                    continue
                try:
                    r = await http.get(url, timeout=8, follow_redirects=True)
                    j = r.json() if r.status_code == 200 else None
                except Exception:
                    j = None
                if isinstance(j, dict):
                    if isinstance(j.get("extensions"), dict):
                        j = {**j, **j["extensions"]}
                    it["meta"] = {"tw": int(bool(j.get("twitter"))), "tg": int(bool(j.get("telegram"))),
                                  "web": int(bool(j.get("website"))), "desc_len": len(str(j.get("description") or "")),
                                  "meta_ok": 1}
                else:
                    it["meta"] = {"meta_ok": 0}
        await asyncio.gather(*(worker() for _ in range(META_WORKERS)))

    def save(self, mint, it, final_mult, finalized):
        t = it["token"]
        row = {"mint": mint, "symbol": t.symbol, "created": t.created, "sampled": it["sampled"],
               "bought": it["bought"], "max_mult": it["max"], "min_mult": it["min"], "t2x": it["t2x"],
               "graduated": int(t.complete), "final_mult": final_mult, "finalized": finalized,
               "path": json.dumps(it["path"], separators=(",", ":")), "ver": DATA_VERSION}
        for s in SNAPSHOTS:
            m, sol, lo, hi, moves = it["snap"].get(s, (None,) * 5)
            row.update({f"m{s}": m, f"s{s}": sol, f"d{s}": lo, f"h{s}": hi, f"a{s}": moves})
        ctx = it["ctx"]
        for k in ("dev_sol", "dev_pct", "creator_n24", "name_n1h", "mayhem", "name_len"):
            row[k] = ctx.get(k)
        for k in ("tw", "tg", "web", "desc_len", "meta_ok"):
            row[k] = it["meta"].get(k)
        cols = list(row)
        self.db.execute(f"INSERT OR REPLACE INTO token_features({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                        [row[c] for c in cols])
        self.db.commit()
        del self.items[mint]

    def flush(self):
        """Saat simulator berhenti: simpan yang belum selesai diamati (ditandai finalized=0)."""
        now = time.time()
        for mint, it in list(self.items.items()):
            t = it["token"]
            if t.seen and now - t.created >= SNAPSHOTS[0]:
                self.save(mint, it, t.price / t.launch_price if t.launch_price else 0.0, finalized=0)
