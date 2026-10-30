"""Simulator meme coin pump.fun (PAPER TRADING: tidak ada uang sungguhan, tidak butuh wallet).

Alur: token baru (PumpPortal) -> harga dibaca dari bonding curve di blockchain (Helius RPC)
-> strategi memutuskan beli -> aturan TP / SL / trailing / batas waktu -> laporan ke Telegram.
Jalankan: python sim.py   (hentikan dengan Ctrl+C)
"""
import asyncio
import base64
import json
import struct
import sys
import time
from dataclasses import dataclass, field, replace

import httpx
import websockets

import socket
import sqlite3

from config import BASE_DIR, load_settings
from report import build_report, connect, kelly
from strategies import Variant, build_variants
from features import POLL_SEC as FEATURE_POLL_SEC, FeatureTracker
from altcoin import alt_status_text
from risk import RiskLimits, RiskManager
from costs import SLIP_MODEL_VERSION, slippage as slip_model
from flow import PUBLIC_RPC, SAMPLE_EVERY as FLOW_SAMPLE_EVERY, FlowTracker, Paced
from wallet_lab import Rpc as PublicRpc
from meme_engine import ExitState, exit_orders
from safety import assess as safety_assess

PUMP_WS = "wss://pumpportal.fun/api/data"
DEX_URL = "https://api.dexscreener.com/latest/dex/tokens/"
SOL_PRICE_SOURCES = (
    ("https://data-api.binance.vision/api/v3/ticker/price?symbol=SOLUSDT", lambda j: j["price"]),
    ("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd", lambda j: j["solana"]["usd"]),
)
WSOL = "So11111111111111111111111111111111111111112"
DEX_MIN_LIQ_USD = 3000  # abaikan pool DEX dengan likuiditas di bawah ini
DEX_MAX_JUMP = 5.0      # tolak harga DEX yang melompat >5x (naik/turun) dari harga valid terakhir
MAX_FILTER_CANDIDATES = 60
MAX_CURVE_MULT = 15  # bonding curve standar pump.fun mentok sekitar 14,7x dari harga peluncuran
SPAM_WINDOW_SEC = 600
# akun protokol pump.fun yang memegang suplai tambahan token "mayhem mode" (bukan dompet orang)
PROTOCOL_HOLDERS = {"BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s"}
AUTO_WINDOW = 60         # Auto Terbaik menilai 60 posisi terakhir tiap varian
AUTO_MIN_N = 10           # varian baru dinilai setelah punya minimal 10 posisi
AUTO_RECHECK_SEC = 600    # saat istirahat, nilai ulang tiap 10 menit

# Konsol Windows kadang tidak bisa menampilkan emoji/simbol nama token
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def decode_curve(b64):
    """Akun bonding curve pump.fun -> (harga SOL per token, SOL riil di curve, sudah lulus?)."""
    raw = base64.b64decode(b64)
    if len(raw) < 49:  # akun kosong/ditutup: curve sudah selesai (koin pindah ke DEX)
        return None
    v_tok, v_sol, _r_tok, r_sol, _supply = struct.unpack_from("<QQQQQ", raw, 8)
    price = (v_sol / 1e9) / (v_tok / 1e6) if v_tok else 0.0
    return price, r_sol / 1e9, bool(raw[48])


@dataclass
class Token:
    mint: str
    symbol: str
    curve: str
    created: float
    launch_price: float
    price: float = 0.0
    min_price: float = 0.0
    real_sol: float = 0.0
    complete: bool = False
    seen: bool = False
    last_change: float = 0.0
    history: list = field(default_factory=list)  # [(waktu, harga)]
    creator: str = ""        # dompet pembuat koin
    dev_initial: float = 0.0  # jumlah token yang dibeli pembuat saat peluncuran

    def update(self, price, now):
        if price != self.price:
            self.last_change = now
        self.price = price
        self.min_price = price if not self.seen else min(self.min_price, price)
        self.seen = True
        self.history.append((now, price))
        if len(self.history) > 120:
            del self.history[:-120]

    def price_ago(self, seconds, now):
        older = [p for t, p in self.history if t <= now - seconds]
        return older[-1] if older else None


@dataclass
class Position:
    strategy: str
    batch: int
    token: Token
    opened: float
    entry_price: float
    tokens: float
    cost_usd: float
    sol_usd: float  # harga SOL saat dibeli: hasil dihitung dengan kurs yang sama
    kelly_stake: float = 0.0  # berapa yang akan dipertaruhkan kalau memakai rem Kelly
    remaining: float = 0.0
    proceeds_usd: float = 0.0
    peak: float = 0.0
    tp1: bool = False
    tp2: bool = False
    tp1_time: float = 0.0
    closed: bool = False

    def mult(self):
        return self.token.price / self.entry_price if self.entry_price else 0.0


@dataclass
class Strategy:
    name: str
    cfg: Variant
    batch: int = 1
    # statistik untuk rem Kelly (posisi yang sudah ditutup)
    n_closed: int = 0
    n_wins: int = 0
    sum_win: float = 0.0
    sum_loss: float = 0.0
    kelly_pnl: float = 0.0
    # khusus Auto Terbaik
    paused: bool = False
    next_eval: float = 0.0
    follow: str = ""
    # anti-rug: koin yang sedang dicek sebelum dibeli, dan jumlah yang ditolak
    pending: set = field(default_factory=set)
    antirug_blocked: int = 0
    opened_in_batch: int = 0
    batch_started: float = field(default_factory=time.time)
    positions: list = field(default_factory=list)
    candidates: dict = field(default_factory=dict)


class Simulator:
    def __init__(self, settings):
        self.s = settings
        self.db = connect()
        self.sol_usd = settings.sol_usd_fallback
        self.outbox = []
        self.started = time.time()
        self.new_tokens_seen = 0
        self.spam_skipped = 0
        self.recent_symbols = {}
        self.last_recorded = {}
        self.last_stats = 0.0
        self.antirug_cache = {}   # mint -> (waktu, lolos?, alasan)
        self.bg_tasks = set()
        row = self.db.execute("SELECT value FROM stats WHERE key='trading_paused'").fetchone()
        self.trading_paused = bool(row and row[0])  # status /stop diingat walau simulator di-restart
        self.recover_orphans()
        self.strategies = {}
        for v in build_variants(settings):
            last = self.db.execute("SELECT MAX(batch) FROM positions WHERE strategy=?", (v.name,)).fetchone()[0]
            n, wins, sw, sl, kp = self.db.execute(
                "SELECT COUNT(*), COALESCE(SUM(pnl_usd>0),0), COALESCE(SUM(CASE WHEN pnl_usd>0 THEN pnl_usd END),0),"
                " COALESCE(-SUM(CASE WHEN pnl_usd<=0 THEN pnl_usd END),0),"
                " COALESCE(SUM(pnl_usd * COALESCE(kelly_stake, cost_usd) / cost_usd),0)"
                " FROM positions WHERE strategy=?", (v.name,)).fetchone()
            self.strategies[v.name] = Strategy(v.name, v, batch=(last or 0) + 1, n_closed=n, n_wins=wins,
                                               sum_win=sw, sum_loss=sl, kelly_pnl=kp)
        self.resumed = self.resume_positions()
        # lapisan risiko deterministik: dipulihkan dari riwayat supaya batas harian/drawdown tetap benar setelah restart
        self.risk = RiskManager(RiskLimits(), mode=settings.risk_mode)
        self.risk.load_history(
            self.db.execute("SELECT strategy, closed, cost_usd, pnl_usd FROM positions WHERE closed IS NOT NULL"
                            " ORDER BY closed").fetchall(),
            [(p.strategy, p.token.mint, p.cost_usd, p.opened) for st in self.strategies.values()
             for p in st.positions if not p.closed])
        self.db.execute("CREATE TABLE IF NOT EXISTS risk_events(ts REAL, strategy TEXT, mint TEXT, action TEXT, reasons TEXT)")
        row = self.db.execute("SELECT value FROM stats WHERE key='slip_model'").fetchone()
        if not row or row[0] != SLIP_MODEL_VERSION:
            self.db.execute("INSERT OR REPLACE INTO stats(key, value) VALUES('slip_model', ?)", (SLIP_MODEL_VERSION,))
            self.db.execute("INSERT OR REPLACE INTO stats(key, value) VALUES('slip_model_since', ?)", (time.time(),))
        self.db.commit()
        self.features = FeatureTracker(self.db)
        self.flow_on = bool(settings.pumpportal_api_key)   # subscribeTokenTrade butuh API key PumpPortal
        # aliran transaksi per token (flow.db) + skor Safety di detik ke-60.
        # Tanpa API key: mode sampel gratis (1 dari 5 token dibaca ulang dari blockchain lewat RPC publik Solana)
        self.flow = FlowTracker(sample_every=0 if self.flow_on else FLOW_SAMPLE_EVERY)
        self.auto_base = {n: st.cfg for n, st in self.strategies.items() if st.cfg.mode == "auto"}
        for name in self.auto_base:
            st = self.strategies[name]
            last = self.db.execute("SELECT follow, action FROM auto_log ORDER BY ts DESC LIMIT 1").fetchone()
            if st.opened_in_batch and last and last[1] != "istirahat" and last[0] in self.strategies:
                # putaran masih berjalan: lanjutkan dengan aturan varian yang sedang diikuti
                rules, base = self.strategies[last[0]].cfg, self.auto_base[name]
                st.follow = last[0]
                st.cfg = replace(rules, name=base.name, label=f"{base.label} → {rules.label}", desc=base.desc,
                                 order=base.order)
            else:
                self.auto_choose(st)
        self.ws_live = 0
        self.http = None

    def auto_choose(self, st):
        """Auto Terbaik: pilih varian dengan skor paling stabil, atau istirahat kalau yang terbaik pun rugi.
        Skor = rata-rata hasil per posisi dikurangi ketidakpastiannya (std / akar n), dari 60 posisi terakhir."""
        best = None
        for name, other in self.strategies.items():
            if other.cfg.mode == "auto" or name in self.auto_base:
                continue
            rows = [r[0] for r in self.db.execute(
                "SELECT pnl_usd FROM positions WHERE strategy=? ORDER BY closed DESC LIMIT ?", (name, AUTO_WINDOW))]
            n = len(rows)
            if n < AUTO_MIN_N:
                continue
            mean = sum(rows) / n
            std = (sum((x - mean) ** 2 for x in rows) / (n - 1)) ** 0.5
            score = mean - std / n ** 0.5
            if best is None or score > best[1]:
                best = (name, score, n, mean)
        base = self.auto_base[st.name]
        previous = (st.paused, st.follow)
        if best is None:
            follow = "filter" if "filter" in self.strategies else next(
                n for n, o in self.strategies.items() if n not in self.auto_base)
            action, score, n, detail = "pemanasan", None, 0, "belum ada varian dengan ≥10 posisi, sementara ikut " + \
                self.strategies[follow].cfg.label
        elif best[3] < 0 and best[2] >= self.s.kelly_min_sample:
            follow, action, score, n = best[0], "istirahat", best[1], best[2]
            detail = (f"varian terbaik ({self.strategies[follow].cfg.label}) pun rata-rata {best[3]:+.3f} USD/posisi"
                      f" dari {n} posisi, jadi tidak trading dulu")
        else:
            follow, action, score, n = best[0], "ikuti", best[1], best[2]
            detail = f"skor {best[1]:+.3f}, rata-rata {best[3]:+.3f} USD/posisi dari {n} posisi"
        st.paused = action == "istirahat"
        st.next_eval = time.time() + AUTO_RECHECK_SEC
        st.follow = "" if st.paused else follow
        if not st.paused:
            rules = self.strategies[follow].cfg
            st.cfg = replace(rules, name=base.name, label=f"{base.label} → {rules.label}", desc=base.desc,
                             order=base.order)
        else:
            st.candidates.clear()
        self.db.execute("INSERT INTO auto_log(ts,follow,action,score,n,detail) VALUES(?,?,?,?,?,?)",
                        (time.time(), follow, action, score, n, detail))
        self.db.commit()
        if (st.paused, st.follow) != previous:
            icon = "😴" if st.paused else "🤖"
            what = "ISTIRAHAT (tidak trading)" if st.paused else f"mengikuti {self.strategies[follow].cfg.label}"
            self.notify(f"{icon} Auto Terbaik putaran #{st.batch}: {what}\nAlasan: {detail}")

    def recover_orphans(self):
        """Cadangan untuk posisi lama tanpa data token (dari versi sebelum fitur 'lanjutkan setelah restart'):
        ditutup di harga terakhir. Posisi yang punya data token dilanjutkan oleh resume_positions()."""
        s = self.s
        active = [v.name for v in build_variants(s)]   # posisi varian yang dipensiunkan juga ditutup di sini
        orphans = self.db.execute(
            "SELECT t.strategy, t.batch, t.mint, t.symbol, t.ts, t.price_sol, t.qty, t.usd, t.sol, t.kelly_stake"
            " FROM trades t LEFT JOIN positions p ON p.mint=t.mint AND p.strategy=t.strategy AND p.batch=t.batch"
            " LEFT JOIN position_meta m ON m.mint=t.mint AND m.strategy=t.strategy AND m.batch=t.batch"
            " WHERE t.side='BELI' AND p.id IS NULL AND (m.mint IS NULL OR t.strategy NOT IN (%s))"
            % ",".join("?" * len(active)), active).fetchall()
        for strategy, batch, mint, symbol, opened, entry, qty, cost, sol, kelly_stake in orphans:
            sold_qty, sold_usd = self.db.execute(
                "SELECT COALESCE(SUM(qty),0), COALESCE(SUM(usd),0) FROM trades"
                " WHERE side='JUAL' AND mint=? AND strategy=? AND batch=?", (mint, strategy, batch)).fetchone()
            last, peak = self.db.execute(
                "SELECT (SELECT price FROM prices WHERE mint=? ORDER BY ts DESC LIMIT 1), MAX(price)"
                " FROM prices WHERE mint=? AND ts>=?", (mint, mint, opened)).fetchone()
            price = last or entry / (1 + s.slippage_min)
            remaining = max(qty - sold_qty, 0)
            gross = remaining * price * (1 - s.slippage_min) * (1 - s.fee_pct)
            net_sol = gross - s.tx_cost_sol if gross > s.tx_cost_sol else 0.0
            usd = net_sol * (cost / sol)
            reason = "simulator mati mendadak" if strategy in active else "varian dipensiunkan"
            now = time.time()
            self.db.execute("INSERT INTO trades(ts,strategy,batch,mint,symbol,side,reason,price_sol,qty,sol,usd)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (now, strategy, batch, mint, symbol, "JUAL", reason, price, remaining, net_sol, usd))
            proceeds = sold_usd + usd
            self.db.execute(
                "INSERT INTO positions(strategy,batch,mint,symbol,opened,closed,cost_usd,proceeds_usd,pnl_usd,"
                "ret_mult,peak_mult,reason,graduated,kelly_stake) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
                (strategy, batch, mint, symbol, opened, now, cost, proceeds, proceeds - cost, proceeds / cost,
                 (peak or price) / entry, reason, kelly_stake))
        if orphans:
            self.db.commit()
            log(f"{len(orphans)} posisi dari sesi sebelumnya (mati mendadak) ditutup di harga terakhir")

    def resume_positions(self):
        """Lanjutkan putaran yang belum selesai: posisi terbuka dimuat ulang dan tetap dikelola
        (TP/SL/trailing) seolah simulator tidak pernah berhenti. Harga diperbarui di pembacaan berikutnya."""
        tokens, resumed = {}, 0
        for st in self.strategies.values():
            last = self.db.execute("SELECT MAX(batch) FROM trades WHERE strategy=? AND side='BELI'",
                                   (st.name,)).fetchone()[0]
            if last is None:
                continue
            if self.db.execute("SELECT 1 FROM batches WHERE strategy=? AND batch=?", (st.name, last)).fetchone():
                st.batch = max(st.batch, last + 1)
                continue
            buys = self.db.execute(
                "SELECT ts, mint, symbol, price_sol, qty, usd, sol, kelly_stake FROM trades"
                " WHERE strategy=? AND batch=? AND side='BELI' ORDER BY ts", (st.name, last)).fetchall()
            st.batch, st.opened_in_batch, st.batch_started = last, len(buys), buys[0][0]
            for ts, mint, symbol, entry, qty, cost, sol, kstake in buys:
                closed = self.db.execute("SELECT proceeds_usd FROM positions WHERE strategy=? AND batch=? AND mint=?",
                                         (st.name, last, mint)).fetchone()
                meta = self.db.execute("SELECT curve, launch, creator, dev_initial, created FROM position_meta"
                                       " WHERE strategy=? AND batch=? AND mint=?", (st.name, last, mint)).fetchone()
                lastp, peak = self.db.execute(
                    "SELECT (SELECT price FROM prices WHERE mint=? ORDER BY ts DESC LIMIT 1), MAX(price)"
                    " FROM prices WHERE mint=? AND ts>=?", (mint, mint, ts)).fetchone()
                price = lastp or entry / (1 + self.s.slippage_min)
                if mint not in tokens:
                    curve, launch, creator, dev_initial, created = meta or ("", entry, "", 0.0, ts)
                    tokens[mint] = Token(mint, symbol, curve, created, launch, price=price, min_price=price,
                                         last_change=time.time(), creator=creator or "", dev_initial=dev_initial or 0.0)
                sold = self.db.execute("SELECT COALESCE(SUM(qty),0), COALESCE(SUM(usd),0), GROUP_CONCAT(reason, '|')"
                                       " FROM trades WHERE strategy=? AND batch=? AND mint=? AND side='JUAL'",
                                       (st.name, last, mint)).fetchone()
                reasons = sold[2] or ""
                pos = Position(st.name, last, tokens[mint], ts, entry, qty, cost, cost / sol if sol else self.sol_usd,
                               kelly_stake=kstake or 0.0, remaining=max(qty - sold[0], 0.0),
                               proceeds_usd=closed[0] if closed else sold[1], peak=peak or price,
                               tp1="TP1" in reasons, tp2="TP2" in reasons, closed=bool(closed))
                st.positions.append(pos)
                resumed += not pos.closed
        if resumed:
            log(f"{resumed} posisi terbuka dari sesi sebelumnya DILANJUTKAN (tidak ditutup paksa)")
        return resumed

    # ------------------------------------------------------------------ utilitas
    def notify(self, text):
        log("[TG] " + text.replace("\n", " | "))
        if self.s.telegram_bot_token and self.s.telegram_chat_id:
            self.outbox.append(text)

    def tracked_tokens(self):
        tokens = {}
        for st in self.strategies.values():
            for t in st.candidates.values():
                tokens[t.mint] = t
            for p in st.positions:
                if not p.closed:
                    tokens[p.token.mint] = p.token
        return tokens

    # ------------------------------------------------------------------ data masuk
    async def listen_new_tokens(self):
        backoff = 2
        while True:
            try:
                url = PUMP_WS + (f"?api-key={self.s.pumpportal_api_key}" if self.flow_on else "")
                async with websockets.connect(url, open_timeout=20, ping_interval=20) as ws:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    again = self.flow.active() if self.flow_on else []
                    if again:   # sambung ulang: lanjutkan langganan transaksi token yang masih diikuti
                        await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": again}))
                    self.flow.take_changes()
                    log("Terhubung ke feed token baru pump.fun" + (" + aliran transaksi" if self.flow_on else
                                                                     " (aliran transaksi nonaktif: PUMPPORTAL_API_KEY kosong)"))
                    backoff = 2
                    syncer = asyncio.create_task(self.flow_subscriber(ws)) if self.flow_on else None
                    try:
                        async for msg in ws:
                            d = json.loads(msg)
                            tx = d.get("txType")
                            if tx == "create" and d.get("pool", "pump") == "pump":
                                self.on_new_token(d)
                            elif tx in ("buy", "sell"):
                                self.flow.on_trade(d)
                    finally:
                        if syncer:
                            syncer.cancel()
            except Exception as e:  # koneksi putus -> sambung ulang
                err = repr(e).replace(self.s.pumpportal_api_key, "***") if self.s.pumpportal_api_key else repr(e)
                log(f"Feed token baru terputus ({err}), sambung ulang dalam {backoff} dtk")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def flow_subscriber(self, ws):
        """Langganan transaksi token baru secepatnya (dikumpulkan tiap 0,3 dtk) di koneksi yang sama dengan feed
        token baru, dan berhenti berlangganan token yang sudah 20 menit diikuti."""
        while True:
            await asyncio.sleep(0.3)
            sub, unsub = self.flow.take_changes()
            if sub:
                await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": sub}))
            if unsub:
                await ws.send(json.dumps({"method": "unsubscribeTokenTrade", "keys": unsub}))

    def on_new_token(self, d):
        self.new_tokens_seen += 1
        now = time.time()
        ctx = self.features.on_create(d, now)   # hitungan dev & nama dicatat untuk semua token, termasuk spam
        # anti-spam: simbol yang sama diluncurkan berulang-ulang dalam 10 menit dilewati
        sym = (d.get("symbol") or "").strip().lower()
        last = self.recent_symbols.get(sym)
        self.recent_symbols[sym] = now
        if len(self.recent_symbols) > 5000:
            self.recent_symbols = {k: t for k, t in self.recent_symbols.items() if now - t < SPAM_WINDOW_SEC}
        if last is not None and now - last < SPAM_WINDOW_SEC:
            self.spam_skipped += 1
            return
        launch = d["vSolInBondingCurve"] / d["vTokensInBondingCurve"]
        token = Token(d["mint"], (d.get("symbol") or "?")[:16], d["bondingCurveKey"], now, launch,
                      price=launch, last_change=now, creator=d.get("traderPublicKey") or "",
                      dev_initial=float(d.get("initialBuy") or 0))
        self.features.add(token, ctx=ctx)  # data belajar AI
        self.flow.start(token, token.creator, float(d.get("initialBuy") or 0), float(d.get("vSolInBondingCurve") or 0), ctx=ctx)
        if self.trading_paused:   # /stop dari Telegram: tidak membuka posisi baru
            return
        for st in self.strategies.values():
            if st.paused:
                continue
            room = self.s.coins_per_batch - st.opened_in_batch
            if st.cfg.mode == "langsung" and len(st.candidates) < room:
                st.candidates[token.mint] = token
            elif st.cfg.mode == "filter" and room > 0 and len(st.candidates) < MAX_FILTER_CANDIDATES:
                st.candidates[token.mint] = token

    async def poll_curves(self, tokens):
        pending = [t for t in tokens if not t.complete]
        for i in range(0, len(pending), 100):
            chunk = pending[i:i + 100]
            body = {"jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
                    "params": [[t.curve for t in chunk], {"encoding": "base64", "commitment": "confirmed"}]}
            r = await self.http.post(self.s.rpc_url, json=body)
            if r.status_code in (401, 403):
                raise RuntimeError("HELIUS_API_KEY ditolak. Cek isi file .env")
            r.raise_for_status()
            now = time.time()
            for t, acc in zip(chunk, r.json()["result"]["value"]):
                if acc is None:
                    continue
                decoded = decode_curve(acc["data"][0])
                if decoded is None:  # curve sudah ditutup: harga lanjut dari DexScreener
                    if not t.complete:
                        t.complete, t.last_change = True, time.time()
                    continue
                price, real_sol, complete = decoded
                t.real_sol = real_sol
                if complete and not t.complete:
                    t.complete = True
                    log(f"{t.symbol} LULUS dari bonding curve -> harga lanjut dari DexScreener")
                    t.last_change = now
                    continue
                t.update(price, now)

    async def poll_dex(self, tokens):
        grads = [t for t in tokens if t.complete]
        for i in range(0, len(grads), 30):
            chunk = {t.mint: t for t in grads[i:i + 30]}
            r = await self.http.get(DEX_URL + ",".join(chunk))
            if r.status_code != 200:
                continue
            best = {}
            for pair in r.json().get("pairs") or []:
                mint = pair.get("baseToken", {}).get("address")
                if mint not in chunk or pair.get("quoteToken", {}).get("address") != WSOL:
                    continue
                liq = (pair.get("liquidity") or {}).get("usd") or 0
                if liq < DEX_MIN_LIQ_USD:  # pool tiruan/sepi sering berharga tidak masuk akal
                    continue
                rank = (pair.get("dexId") == "pumpswap", liq)  # utamakan pool resmi PumpSwap
                if mint not in best or rank > best[mint][0]:
                    best[mint] = (rank, float(pair["priceNative"]))
            now = time.time()
            for mint, (_rank, price) in best.items():
                t = chunk[mint]
                ref = t.price
                if ref and not (ref / DEX_MAX_JUMP <= price <= ref * DEX_MAX_JUMP):
                    log(f"Harga DEX {t.symbol} ditolak: {price:.3e} vs terakhir {ref:.3e} (lompatan tidak wajar)")
                    continue
                t.update(price, now)

    async def ws_prices(self):
        """Harga REAL-TIME untuk koin yang sedang dipegang: berlangganan perubahan akun bonding curve
        lewat WebSocket Helius. Setiap transaksi beli/jual langsung terlihat, sehingga stop loss dan
        keluar darurat tidak menunggu pembacaan berikutnya. Pembacaan berkala tetap berjalan sebagai cadangan."""
        if not self.s.helius_api_key:
            log("WebSocket harga real-time nonaktif (HELIUS_API_KEY kosong)")
            return
        url = f"wss://mainnet.helius-rpc.com/?api-key={self.s.helius_api_key}"
        backoff = 2
        while True:
            subs, sub2curve, pending, req = {}, {}, {}, [0]
            try:
                async with websockets.connect(url, ping_interval=20, max_size=None, open_timeout=20) as ws:
                    log("WebSocket harga real-time terhubung")
                    backoff = 2

                    async def send(method, params):
                        req[0] += 1
                        await ws.send(json.dumps({"jsonrpc": "2.0", "id": req[0], "method": method, "params": params}))
                        return req[0]

                    async def sync():  # sesuaikan langganan dengan posisi yang sedang terbuka
                        while True:
                            want = {p.token.curve for st in self.strategies.values() for p in st.positions
                                    if not p.closed and not p.token.complete and p.token.curve}
                            for curve in want - subs.keys() - set(pending.values()):
                                pending[await send("accountSubscribe",
                                                   [curve, {"encoding": "base64", "commitment": "confirmed"}])] = curve
                            for curve in list(subs.keys() - want):
                                sid = subs.pop(curve)
                                sub2curve.pop(sid, None)
                                await send("accountUnsubscribe", [sid])
                            self.ws_live = len(subs)
                            await asyncio.sleep(2)

                    syncer = asyncio.create_task(sync())
                    try:
                        async for msg in ws:
                            d = json.loads(msg)
                            if d.get("id") in pending:
                                curve = pending.pop(d["id"])
                                if "result" in d:
                                    subs[curve] = d["result"]
                                    sub2curve[d["result"]] = curve
                            elif d.get("method") == "accountNotification":
                                self.on_curve_update(sub2curve.get(d["params"]["subscription"]),
                                                     d["params"]["result"]["value"])
                    finally:
                        syncer.cancel()
            except Exception as e:
                self.ws_live = 0
                log(f"WebSocket harga terputus ({e!r}), sambung ulang dalam {backoff} dtk")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def on_curve_update(self, curve, value):
        if not curve or not value:
            return
        decoded = decode_curve(value["data"][0])
        now = time.time()
        price, real_sol, complete = decoded if decoded else (0.0, 0.0, True)
        for st in self.strategies.values():
            for p in st.positions:
                t = p.token
                if p.closed or t.curve != curve or t.complete:
                    continue
                t.real_sol = real_sol
                if complete:
                    t.complete, t.last_change = True, now
                else:
                    t.update(price, now)
        self.evaluate_exits()  # langsung cek SL/TP/trailing dengan harga terbaru

    async def poll_loop(self):
        last_dex = last_feat = last_dev = 0.0
        while True:
            await asyncio.sleep(self.s.poll_sec)
            try:
                tracked = self.tracked_tokens()
                tokens = list(tracked.values())
                # token muda yang sedang dipelajari ikut dibaca secepat kandidat (jejak harga rapat)
                young = [t for t in self.features.young(time.time()) if t.mint not in tracked]
                await self.poll_curves(tokens + young)
                if time.time() - last_dex >= 15:
                    last_dex = time.time()
                    await self.poll_dex(tokens)
                if time.time() - last_feat >= FEATURE_POLL_SEC:  # token sampel: lebih jarang, hemat kuota
                    last_feat = time.time()
                    young_ids = {t.mint for t in young}
                    await self.poll_curves([t for t in self.features.tokens()
                                            if t.mint not in tracked and t.mint not in young_ids])
                self.features.observe(time.time())
                self.flow.tick(time.time(), lambda snap, it: safety_assess(snap, it["ctx"], it["missed"]))
                if time.time() - last_dev >= self.s.antirug_dev_check_sec:
                    last_dev = time.time()
                    await self.antirug_exit_check()
                for name in self.auto_base:
                    st = self.strategies[name]
                    if st.paused and time.time() >= st.next_eval:
                        self.auto_choose(st)
            except RuntimeError as e:
                log(f"FATAL: {e}")
                raise
            except Exception as e:
                log(f"Gagal membaca harga ({e!r}), coba lagi")
                continue
            self.evaluate_entries()
            self.evaluate_exits()
            self.record_prices()
            self.check_batches()
            self.write_stats()

    # ------------------------------------------------------------------ keputusan
    def evaluate_entries(self):
        now = time.time()
        s = self.s
        for st in self.strategies.values():
            c = st.cfg
            if st.paused or c.mode == "auto":
                st.candidates.clear()
                continue
            for mint, t in list(st.candidates.items()):
                age = now - t.created
                if st.opened_in_batch + len(st.pending) >= s.coins_per_batch:
                    st.candidates.clear()
                    break
                if not t.seen:
                    if age > 30:
                        del st.candidates[mint]
                    continue
                if t.price > t.launch_price * MAX_CURVE_MULT:
                    del st.candidates[mint]  # tidak mungkin di curve standar: data/konfigurasi aneh
                    continue
                if c.mode == "langsung":
                    if age >= c.entry_delay_sec:
                        del st.candidates[mint]
                        self.open_position(st, t)
                    continue
                # mode "filter"
                if t.min_price < t.launch_price * (1 - c.filter_max_drawdown) or t.complete:
                    del st.candidates[mint]
                    continue
                if age < c.observe_sec:
                    continue
                del st.candidates[mint]
                m = t.price / t.launch_price
                before = t.price_ago(30, now)
                if (t.real_sol >= c.filter_min_real_sol and c.filter_min_mult <= m <= c.filter_max_mult
                        and before is not None and t.price >= before):
                    if c.antirug:  # cek dev & holder dulu (butuh panggilan ke blockchain)
                        st.pending.add(mint)
                        task = asyncio.create_task(self.antirug_then_open(st, t))
                        self.bg_tasks.add(task)
                        task.add_done_callback(self.bg_tasks.discard)
                    else:
                        self.open_position(st, t)

    # ------------------------------------------------------------------ anti-rug
    async def rpc(self, method, params):
        r = await self.http.post(self.s.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(body["error"].get("message", "RPC error"))
        return body["result"]

    async def dev_balance(self, t):
        res = await self.rpc("getTokenAccountsByOwner", [t.creator, {"mint": t.mint}, {"encoding": "jsonParsed"}])
        return sum(float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0) for a in res["value"])

    async def antirug_check(self, t, c):
        """Lolos kalau pembuat koin belum menjual banyak dan kepemilikan token tidak terkonsentrasi."""
        cached = self.antirug_cache.get(t.mint)
        if cached and time.time() - cached[0] < 20:
            return cached[1], cached[2]
        try:
            if t.creator and t.dev_initial > 0:
                sold = 1 - await self.dev_balance(t) / t.dev_initial
                if sold >= c.antirug_max_dev_sold:
                    return self._cache(t, False, f"dev sudah jual {sold:.0%}")
            largest = (await self.rpc("getTokenLargestAccounts", [t.mint]))["value"]
            infos = (await self.rpc("getMultipleAccounts", [[a["address"] for a in largest],
                                                            {"encoding": "jsonParsed"}]))["value"]
            supply = float((await self.rpc("getTokenSupply", [t.mint]))["value"]["uiAmount"] or 1e9)
            amounts = []
            for a, info in zip(largest, infos):
                owner = (((info or {}).get("data") or {}).get("parsed") or {}).get("info", {}).get("owner")
                amount = float(a.get("uiAmount") or 0)
                if owner in PROTOCOL_HOLDERS:  # token "mayhem mode": +1 miliar dipegang protokol pump.fun
                    supply -= amount
                elif owner != t.curve:          # token milik bonding curve bukan "holder"
                    amounts.append(amount)
            shares = [a / max(supply, 1.0) for a in amounts]
            shares.sort(reverse=True)
            top1, top5 = (shares[0] if shares else 0), sum(shares[:5])
            if top1 > c.antirug_max_holder_pct:
                return self._cache(t, False, f"1 dompet pegang {top1:.0%}")
            if top5 > c.antirug_max_top5_pct:
                return self._cache(t, False, f"5 dompet teratas pegang {top5:.0%}")
            return self._cache(t, True, f"top1 {top1:.0%}, top5 {top5:.0%}")
        except Exception as e:  # gagal cek = jangan beli (lebih aman)
            return self._cache(t, False, f"cek gagal ({type(e).__name__})")

    def _cache(self, t, ok, reason):
        self.antirug_cache[t.mint] = (time.time(), ok, reason)
        if len(self.antirug_cache) > 3000:
            cutoff = time.time() - 60
            self.antirug_cache = {m: v for m, v in self.antirug_cache.items() if v[0] > cutoff}
        return ok, reason

    async def antirug_then_open(self, st, t):
        ok, reason = await self.antirug_check(t, st.cfg)
        st.pending.discard(t.mint)
        if not ok:
            st.antirug_blocked += 1
            log(f"[{st.name}] ANTI-RUG tolak {t.symbol}: {reason}")
            return
        if (not st.paused and not self.trading_paused and st.opened_in_batch < self.s.coins_per_batch
                and t.mint not in {p.token.mint for p in st.positions}):
            self.open_position(st, t)

    async def antirug_exit_check(self):
        """Posisi terbuka di varian anti-rug: keluar kalau pembuat koin mulai menjual."""
        for st in self.strategies.values():
            if not st.cfg.antirug:
                continue
            for pos in st.positions:
                t = pos.token
                if pos.closed or not t.creator or t.dev_initial <= 0:
                    continue
                try:
                    sold = 1 - await self.dev_balance(t) / t.dev_initial
                except Exception:
                    continue
                if sold >= st.cfg.antirug_max_dev_sold and not pos.closed:
                    self.sell(pos, pos.remaining, f"dev dump ({sold:.0%} dijual)")

    def kelly_stake(self, st):
        """Taruhan yang disarankan rem Kelly untuk varian ini, dari hasilnya sendiri sejauh ini."""
        s = self.s
        k = kelly(st.n_closed, st.n_wins, st.sum_win, st.sum_loss, s)
        if k["status"] == "belajar":
            return s.per_coin_usd, k
        if k["f"] <= 0:
            return 0.0, k
        bankroll = max(s.batch_budget_usd + st.kelly_pnl, 0.0)
        return min(bankroll * s.kelly_fraction * k["f"], s.per_coin_usd * s.kelly_max_stake_mult), k

    def slippage(self, t, side):
        """Slippage terkalibrasi (costs.py): beli tetap kecil, jual membesar kalau harga sedang bergerak cepat.
        Untuk token yang baru lahir, gerakannya diukur dari harga peluncuran."""
        s = self.s
        ref = t.price_ago(s.slip_lookback_sec, time.time()) or t.launch_price or t.price
        move = abs(t.price / ref - 1) if ref else 0.0
        return slip_model(s, move, side)

    def open_position(self, st, t):
        s = self.s
        slip = self.slippage(t, "buy")
        d = self.risk.check_entry(st.name, t.mint, s.per_coin_usd, slippage=slip, liquidity=t.real_sol)
        if d.blocked_by or d.shadow:
            self.db.execute("INSERT INTO risk_events VALUES (?,?,?,?,?)", (time.time(), st.name, t.mint,
                            "blokir" if not d.allowed else "catat", "; ".join(d.blocked_by + d.shadow)))
            self.db.commit()
        if not d.allowed:
            log(f"[{st.name}] RISIKO blokir {t.symbol}: {'; '.join(d.blocked_by)}")
            return
        sol = s.per_coin_usd / self.sol_usd
        net = sol * (1 - s.fee_pct) - s.tx_cost_sol
        entry = t.price * (1 + slip)
        qty = net / entry
        self.features.add(t, bought=True)
        stake, k = self.kelly_stake(st)
        pos = Position(st.name, st.batch, t, time.time(), entry, qty, s.per_coin_usd, self.sol_usd,
                       kelly_stake=stake, remaining=qty, peak=t.price)
        st.positions.append(pos)
        st.opened_in_batch += 1
        self.risk.on_open(st.name, t.mint, s.per_coin_usd)
        self.record_trade(pos, "BELI", "masuk", entry, qty, sol, s.per_coin_usd, kelly_stake=stake, slip=slip)
        # data token disimpan supaya posisi bisa dilanjutkan kalau simulator di-restart
        self.db.execute("INSERT OR REPLACE INTO position_meta(strategy,batch,mint,curve,launch,creator,dev_initial,created)"
                        " VALUES(?,?,?,?,?,?,?,?)", (st.name, st.batch, t.mint, t.curve, t.launch_price, t.creator,
                                                     t.dev_initial, t.created))
        self.db.commit()
        log(f"[{st.name}] BELI {t.symbol} ${s.per_coin_usd:.2f} @ {t.price / t.launch_price:.2f}x harga"
            f" peluncuran, slip {slip:.1%} ({st.opened_in_batch}/{s.coins_per_batch}), Kelly: {k['status']} ${stake:.2f}")

    def sell(self, pos, qty, reason):
        s = self.s
        qty = min(qty, pos.remaining)
        slip = self.slippage(pos.token, "sell")
        gross = qty * pos.token.price * (1 - slip) * (1 - s.fee_pct)
        net_sol = gross - s.tx_cost_sol if gross > s.tx_cost_sol else 0.0  # debu: ditinggalkan
        usd = net_sol * pos.sol_usd
        pos.remaining -= qty
        pos.proceeds_usd += usd
        self.record_trade(pos, "JUAL", reason, pos.token.price, qty, net_sol, usd, slip=slip)
        log(f"[{pos.strategy}] JUAL {pos.token.symbol} {qty / pos.tokens:.0%} @ {pos.mult():.2f}x ({reason})")
        if pos.remaining <= pos.tokens * 1e-9:
            self.close_position(pos, reason)

    def evaluate_exits(self):
        """Aturan jual dari mesin bersama (meme_engine.py): persis sama dengan yang dipakai backtest."""
        now = time.time()
        for st in self.strategies.values():
            c = st.cfg  # aturan jual milik varian ini
            for pos in st.positions:
                if pos.closed:
                    continue
                es = ExitState(pos.tokens, pos.remaining, pos.entry_price, pos.peak, pos.opened, pos.token.last_change,
                               pos.tp1, pos.tp2, pos.tp1_time)
                orders = exit_orders(c, es, pos.token.price, now)
                pos.peak, pos.tp1, pos.tp2, pos.tp1_time = es.peak, es.tp1, es.tp2, es.tp1_time
                for qty, reason in orders:
                    if pos.closed:
                        break
                    self.sell(pos, qty, reason)

    def close_position(self, pos, reason):
        pos.closed = True
        ret = pos.proceeds_usd / pos.cost_usd
        peak = pos.peak / pos.entry_price
        pnl = pos.proceeds_usd - pos.cost_usd
        self.db.execute(
            "INSERT INTO positions(strategy,batch,mint,symbol,opened,closed,cost_usd,proceeds_usd,pnl_usd,"
            "ret_mult,peak_mult,reason,graduated,kelly_stake) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pos.strategy, pos.batch, pos.token.mint, pos.token.symbol, pos.opened, time.time(), pos.cost_usd,
             pos.proceeds_usd, pnl, ret, peak, reason, int(pos.token.complete), pos.kelly_stake))
        self.db.commit()
        self.risk.on_close(pos.strategy, pos.cost_usd, pnl)
        st = self.strategies.get(pos.strategy)
        if st:
            st.n_closed += 1
            if pnl > 0:
                st.n_wins += 1
                st.sum_win += pnl
            else:
                st.sum_loss += -pnl
            st.kelly_pnl += pnl * pos.kelly_stake / pos.cost_usd
        text = (f"[{pos.strategy}] {pos.token.symbol} ditutup: modal ${pos.cost_usd:.2f} -> ${pos.proceeds_usd:.2f}"
                f" ({ret:.2f}x), puncak {peak:.1f}x, alasan: {reason}")
        if ret >= self.s.big_win_mult:
            self.notify("🚀 " + text + f"\nhttps://pump.fun/coin/{pos.token.mint}")
        elif self.s.notify_each_close:
            self.notify(text)
        else:
            log(text)

    def check_batches(self):
        for st in self.strategies.values():
            if st.opened_in_batch < self.s.coins_per_batch or any(not p.closed for p in st.positions):
                continue
            invested = sum(p.cost_usd for p in st.positions)
            returned = sum(p.proceeds_usd for p in st.positions)
            pnl = returned - invested
            wins = sum(p.proceeds_usd > p.cost_usd for p in st.positions)
            best = max(st.positions, key=lambda p: p.proceeds_usd)
            self.db.execute("INSERT INTO batches(strategy,batch,started,ended,invested,returned,pnl)"
                            " VALUES(?,?,?,?,?,?,?)",
                            (st.name, st.batch, st.batch_started, time.time(), invested, returned, pnl))
            self.db.commit()
            minutes = (time.time() - st.batch_started) / 60
            text = (f"{'✅' if pnl >= 0 else '❌'} PUTARAN #{st.batch} {st.cfg.label} selesai ({minutes:.0f} mnt)\n"
                    f"Modal ${invested:.2f} -> kembali ${returned:.2f} ({pnl:+.2f} USD, {pnl / invested:+.0%})\n"
                    f"Untung {wins}/{len(st.positions)} koin. Terbaik: {best.token.symbol}"
                    f" {best.proceeds_usd / best.cost_usd:.2f}x")
            if self.s.notify_batches:
                self.notify(text)
            else:
                log(text)
            st.batch += 1
            st.opened_in_batch = 0
            st.positions = []
            st.batch_started = time.time()
            if st.name in self.auto_base:
                self.auto_choose(st)

    def record_prices(self):
        """Simpan harga koin yang sedang dipegang (hanya saat berubah) untuk chart di dashboard."""
        now = time.time()
        rows = []
        for st in self.strategies.values():
            for p in st.positions:
                t = p.token
                if not p.closed and t.price and self.last_recorded.get(t.mint) != t.price:
                    self.last_recorded[t.mint] = t.price
                    rows.append((t.mint, now, t.price))
        if rows:
            self.db.executemany("INSERT INTO prices(mint,ts,price) VALUES(?,?,?)", rows)
            self.db.commit()
        if len(self.last_recorded) > 2000:
            open_mints = {p.token.mint for st in self.strategies.values() for p in st.positions}
            self.last_recorded = {m: v for m, v in self.last_recorded.items() if m in open_mints}

    def set_trading_paused(self, paused):
        self.trading_paused = paused
        if paused:
            for st in self.strategies.values():
                st.candidates.clear()
        self.last_stats = 0.0
        self.write_stats()  # langsung disimpan supaya diingat walau simulator di-restart

    async def backup_loop(self):
        """Backup database sekali sehari ke folder backups/, simpan N hari terakhir."""
        folder = BASE_DIR / "backups"
        while True:
            try:
                folder.mkdir(exist_ok=True)
                target = folder / f"simulasi-{time.strftime('%Y-%m-%d')}.db"
                if not target.exists():
                    dst = sqlite3.connect(target)
                    self.db.backup(dst)
                    dst.close()
                    log(f"Backup database dibuat: backups/{target.name}")
                for old in sorted(folder.glob("simulasi-20*.db"))[:-self.s.backup_keep]:  # arsip reset tidak disentuh
                    old.unlink()
            except Exception as e:
                log(f"Backup gagal ({e!r})")
            await asyncio.sleep(3600)  # cek tiap jam; file baru hanya dibuat sekali per hari

    def write_stats(self):
        """Statistik live untuk panel 'execution cycle' di dashboard (paling sering tiap 5 detik)."""
        now = time.time()
        if now - self.last_stats < 5:
            return
        self.last_stats = now
        rows = [("heartbeat", now), ("started", self.started), ("tokens_seen", self.new_tokens_seen),
                ("spam_skipped", self.spam_skipped), ("sol_usd", self.sol_usd),
                ("coins_per_batch", self.s.coins_per_batch), ("batch_budget_usd", self.s.batch_budget_usd),
                ("features_tracking", len(self.features.items)), ("trading_paused", int(self.trading_paused)),
                ("flow_tracking", len(self.flow.items)), ("flow_trades", self.flow.trades_seen), ("flow_on", 1 if self.flow_on else 2),
                ("flow_sample_ok", self.flow.sample_ok), ("flow_sample_fail", self.flow.sample_fail),
                ("antirug_blocked", sum(st.antirug_blocked for st in self.strategies.values())),
                ("ws_live", self.ws_live)]
        for st in self.strategies.values():
            rows += [(f"watch_{st.name}", len(st.candidates)), (f"batch_{st.name}", st.batch),
                     (f"opened_{st.name}", st.opened_in_batch)]
        self.db.executemany("INSERT OR REPLACE INTO stats(key,value) VALUES(?,?)", rows)
        self.db.commit()

    def record_trade(self, pos, side, reason, price, qty, sol, usd, kelly_stake=None, slip=None):
        self.db.execute("INSERT INTO trades(ts,strategy,batch,mint,symbol,side,reason,price_sol,qty,sol,usd,"
                        "kelly_stake,slip) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (time.time(), pos.strategy, pos.batch, pos.token.mint, pos.token.symbol, side, reason,
                         price, qty, sol, usd, kelly_stake, slip))
        self.db.commit()

    # ------------------------------------------------------------------ Telegram & status
    def status_text(self):
        hours = (time.time() - self.started) / 3600
        lines = [f"STATUS SIMULATOR (jalan {hours:.1f} jam, {self.new_tokens_seen} token baru terpantau,"
                 f" {self.spam_skipped} spam dilewati, SOL ${self.sol_usd:.2f})"]
        if self.trading_paused:
            lines.append("⏸ TRADING DIJEDA: tidak membuka posisi baru (ketik /mulai untuk lanjut)")
        lines.append("\nSTRATEGY LAB (hasil semua posisi selesai, modal tetap | rem Kelly):")
        ranked = sorted(self.strategies.values(), key=lambda st: -(st.sum_win - st.sum_loss))
        for i, st in enumerate(ranked, 1):
            k = kelly(st.n_closed, st.n_wins, st.sum_win, st.sum_loss, self.s)
            if k["status"] == "belajar":
                kel = f"belajar {st.n_closed}/{self.s.kelly_min_sample}"
            elif k["status"] == "stop":
                kel = "STOP (edge negatif)"
            else:
                kel = f"aktif f={k['f']:.2f}"
            open_n = sum(not p.closed for p in st.positions)
            label = st.cfg.label + (" 😴 ISTIRAHAT" if st.paused else "")
            lines.append(f"{i}. {label}: {st.sum_win - st.sum_loss:+.2f} USD | Kelly {st.kelly_pnl:+.2f}"
                         f" | {kel} | putaran #{st.batch} {st.opened_in_batch}/{self.s.coins_per_batch},"
                         f" dipegang {open_n}")
        return "\n".join(lines)

    async def telegram_sender(self):
        url = f"https://api.telegram.org/bot{self.s.telegram_bot_token}/sendMessage"
        while True:
            await asyncio.sleep(1)
            while self.outbox:
                text = self.outbox[0]
                try:
                    r = await self.http.post(url, json={"chat_id": self.s.telegram_chat_id, "text": text[:4000],
                                                        "disable_web_page_preview": True})
                    if r.status_code == 429:
                        await asyncio.sleep(r.json().get("parameters", {}).get("retry_after", 5))
                        continue
                    if r.status_code != 200:
                        log(f"Telegram menolak pesan ({r.status_code}): {r.text[:200]}")
                except Exception as e:
                    log(f"Gagal kirim Telegram ({e!r})")
                    break
                self.outbox.pop(0)

    async def telegram_commands(self):
        url = f"https://api.telegram.org/bot{self.s.telegram_bot_token}/getUpdates"
        offset = None
        while True:
            try:
                r = await self.http.get(url, params={"timeout": 25, "offset": offset}, timeout=35)
                for upd in r.json().get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or {}
                    if str(msg.get("chat", {}).get("id")) != str(self.s.telegram_chat_id):
                        continue  # abaikan orang lain
                    cmd = (msg.get("text") or "").split("@")[0].strip().lower()
                    if cmd in ("/status", "/varian"):
                        self.notify(self.status_text())
                    elif cmd == "/laporan":
                        self.notify(build_report(self.db))
                    elif cmd == "/alt":
                        self.notify(alt_status_text())
                    elif cmd == "/stop":
                        self.set_trading_paused(True)
                        open_n = sum(not p.closed for st in self.strategies.values() for p in st.positions)
                        self.notify(f"⏸ Trading DIJEDA. Tidak ada posisi baru yang dibuka.\n{open_n} posisi yang masih"
                                    " terbuka tetap dikelola sampai ditutup (TP/SL/dll). Data koin tetap dikumpulkan.\n"
                                    "Ketik /mulai untuk melanjutkan.")
                    elif cmd == "/mulai":
                        self.set_trading_paused(False)
                        self.notify("▶ Trading DILANJUTKAN. Semua varian kembali membuka posisi baru.")
                    elif cmd in ("/start", "/help"):
                        self.notify("Perintah:\n/status atau /varian - peringkat varian & putaran berjalan\n"
                                    "/laporan - hasil keseluruhan\n/alt - simulasi altcoin & koin besar\n/stop - jeda trading (posisi terbuka tetap dikelola)\n"
                                    "/mulai - lanjutkan trading"
                                    + ("\n\n⏸ Saat ini trading sedang DIJEDA." if self.trading_paused else ""))
            except Exception as e:
                log(f"Gagal membaca perintah Telegram ({e!r})")
                await asyncio.sleep(5)

    async def fetch_sol_price(self, first=False):
        for url, pick in SOL_PRICE_SOURCES:
            try:
                r = await self.http.get(url)
                price = float(pick(r.json()))
            except Exception as e:
                log(f"Gagal ambil harga SOL dari {url.split('/')[2]} ({e!r})")
                continue
            if not first and abs(price / self.sol_usd - 1) > 0.20:
                log(f"Harga SOL ${price:.2f} tidak masuk akal (sebelumnya ${self.sol_usd:.2f}), diabaikan")
                continue
            self.sol_usd = price
            return

    async def sol_price_loop(self):
        while True:
            await asyncio.sleep(300)
            await self.fetch_sol_price()

    async def summary_loop(self):
        while True:
            await asyncio.sleep(self.s.summary_every_min * 60)
            self.notify(self.status_text())

    # ------------------------------------------------------------------ jalan & berhenti
    async def run(self):
        s = self.s
        self.http = httpx.AsyncClient(timeout=20)
        if not s.helius_api_key:
            log("PERINGATAN: HELIUS_API_KEY kosong, pakai RPC publik (lambat & sering dibatasi)")
        tg = bool(s.telegram_bot_token and s.telegram_chat_id)
        if not tg:
            log("PERINGATAN: token/Chat ID Telegram kosong, laporan hanya tampil di layar ini")
        await self.fetch_sol_price(first=True)
        self.notify(f"🟢 Simulator mulai. Strategy Lab: {len(self.strategies)} varian"
                    f" ({', '.join(st.cfg.label for st in self.strategies.values())}). "
                    f"Modal per putaran ${s.batch_budget_usd:g} / {s.coins_per_batch} koin"
                    f" (${s.per_coin_usd:.2f} per koin). SOL ${self.sol_usd:.2f}.\n"
                    "Ketik /status atau /laporan kapan saja.")
        if self.trading_paused:
            self.notify("⏸ Catatan: trading masih DIJEDA (dari /stop sebelumnya). Ketik /mulai untuk melanjutkan.")
        tasks = [self.listen_new_tokens(), self.poll_loop(), self.sol_price_loop(), self.summary_loop(),
                 self.backup_loop(), self.ws_prices(), self.features.meta_worker(self.http)]
        if not self.flow_on:   # mode sampel aliran transaksi: RPC publik Solana dengan jeda tetap antar panggilan
            pub = PublicRpc(PUBLIC_RPC)
            pub.sem = asyncio.Semaphore(1)
            tasks.append(self.flow.sample_worker(Paced(pub)))
        if tg:
            tasks += [self.telegram_sender(), self.telegram_commands()]
        await asyncio.gather(*tasks)

    def shutdown(self):
        """Posisi terbuka TIDAK ditutup: akan dilanjutkan saat simulator dijalankan lagi."""
        open_pos = [p for st in self.strategies.values() for p in st.positions if not p.closed]
        self.features.flush()
        self.flow.flush()
        log(f"Simulator berhenti. {len(open_pos)} posisi terbuka akan dilanjutkan saat simulator dijalankan lagi.")
        print("\n" + build_report(self.db))
        if self.s.telegram_bot_token and self.s.telegram_chat_id:
            try:
                httpx.post(f"https://api.telegram.org/bot{self.s.telegram_bot_token}/sendMessage",
                           json={"chat_id": self.s.telegram_chat_id,
                                 "text": "⏹ Simulator dihentikan.\n\n" + build_report(self.db)}, timeout=10)
            except Exception:
                pass
        self.db.close()


LOCK_PORT = 8051  # dipakai sebagai "kunci": hanya satu simulator boleh berjalan


def main():
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        print("Simulator sudah berjalan di jendela lain. Jendela ini tidak menjalankan simulator kedua.")
        time.sleep(8)
        return
    sim = Simulator(load_settings())
    try:
        asyncio.run(sim.run())
    except KeyboardInterrupt:
        pass
    finally:
        sim.shutdown()


if __name__ == "__main__":
    main()
