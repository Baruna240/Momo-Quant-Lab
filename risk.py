"""Lapisan risiko deterministik. Keputusan di sini TIDAK bisa ditimpa agent / AI / strategi mana pun:
batasnya dibaca sekali dari pengaturan ke objek beku (frozen) dan setiap entry wajib lewat check_entry().

Dua mode:
- "enforce": semua batas benar-benar memblokir entry baru (untuk strategi yang lolos validasi / paper trading juara)
- "shadow" : hanya kill switch & anti-order-ganda yang memblokir; batas lain DICATAT ("seandainya ditegakkan")
             supaya simulator riset tetap mengumpulkan data, tapi dampaknya tetap bisa diukur dengan replay()
Kill switch: buat file bernama KILL_SWITCH di folder proyek -> semua entry baru berhenti seketika (dua mode).
"""
import time
from dataclasses import dataclass, field

from config import BASE_DIR

KILL_FILE = BASE_DIR / "KILL_SWITCH"
DAY = 86400


@dataclass(frozen=True)
class RiskLimits:
    max_daily_loss: float = 5.0       # USD rugi bersih per strategi per hari (UTC); 25% dari modal putaran $20
    max_drawdown: float = 10.0        # USD turun dari puncak PnL kumulatif strategi
    max_exposure: float = 20.0        # USD modal yang boleh tertanam di posisi terbuka per strategi
    max_open: int = 20                # posisi terbuka bersamaan per strategi
    max_slippage: float = 0.08        # tolak entry kalau perkiraan slippage di atas 8%
    min_liquidity: float = 0.0        # SOL riil minimal di bonding curve
    dup_window_sec: float = 120.0     # koin yang sama di strategi yang sama tidak boleh dibeli lagi dalam 2 menit


@dataclass
class Decision:
    allowed: bool
    blocked_by: list = field(default_factory=list)   # alasan yang benar-benar memblokir
    shadow: list = field(default_factory=list)       # batas yang terlanggar tapi hanya dicatat (mode shadow)


@dataclass
class _Book:
    day: int = -1
    day_pnl: float = 0.0
    cum: float = 0.0
    peak: float = 0.0
    exposure: float = 0.0
    open_n: int = 0
    halted: bool = False              # drawdown terlampaui: berhenti sampai di-reset manual


class RiskManager:
    def __init__(self, limits=RiskLimits(), mode="shadow", kill_file=KILL_FILE, clock=time.time):
        if mode not in ("shadow", "enforce"):
            raise ValueError("mode harus 'shadow' atau 'enforce'")
        self.limits, self.mode, self.kill_file, self.clock = limits, mode, kill_file, clock
        self.books = {}
        self.recent = {}              # (strategi, kunci koin) -> waktu order terakhir

    def _book(self, strategy):
        return self.books.setdefault(strategy, _Book())

    def _roll(self, b, now):
        d = int(now // DAY)
        if d != b.day:
            b.day, b.day_pnl = d, 0.0

    def killed(self):
        return self.kill_file.exists()

    # ---------------------------------------------------------- kejadian
    def on_open(self, strategy, key, cost, now=None):
        now = self.clock() if now is None else now
        b = self._book(strategy)
        b.exposure += cost
        b.open_n += 1
        self.recent[(strategy, key)] = now
        if len(self.recent) > 20000:
            cut = now - self.limits.dup_window_sec
            self.recent = {k: t for k, t in self.recent.items() if t > cut}

    def on_close(self, strategy, cost, pnl, now=None):
        now = self.clock() if now is None else now
        b = self._book(strategy)
        self._roll(b, now)
        b.exposure = max(0.0, b.exposure - cost)
        b.open_n = max(0, b.open_n - 1)
        b.day_pnl += pnl
        b.cum += pnl
        b.peak = max(b.peak, b.cum)
        if b.cum - b.peak <= -self.limits.max_drawdown:
            b.halted = True

    def load_history(self, closed, open_positions=()):
        """Pulihkan keadaan setelah restart. closed: [(strategi, waktu tutup, modal, pnl)] urut waktu;
        open_positions: [(strategi, kunci koin, modal, waktu buka)]."""
        for strategy, ts, cost, pnl in closed:
            b = self._book(strategy)
            b.exposure += cost
            b.open_n += 1
            self.on_close(strategy, cost, pnl, now=ts)
        for strategy, key, cost, ts in open_positions:
            self.on_open(strategy, key, cost, now=ts)

    # ---------------------------------------------------------- keputusan
    def check_entry(self, strategy, key, cost, slippage=0.0, liquidity=None, now=None):
        now = self.clock() if now is None else now
        L, b = self.limits, self._book(strategy)
        self._roll(b, now)
        hard, soft = [], []
        if self.killed():
            hard.append("kill switch aktif")
        last = self.recent.get((strategy, key))
        if last is not None and now - last < L.dup_window_sec:
            hard.append("order ganda")
        if b.day_pnl <= -L.max_daily_loss:
            soft.append(f"rugi harian {b.day_pnl:.2f} melewati batas {L.max_daily_loss:.2f}")
        if b.halted or b.cum - b.peak <= -L.max_drawdown:
            soft.append(f"drawdown {b.cum - b.peak:.2f} melewati batas {L.max_drawdown:.2f}")
        if b.exposure + cost > L.max_exposure + 1e-9:
            soft.append(f"exposure {b.exposure + cost:.2f} > {L.max_exposure:.2f}")
        if b.open_n + 1 > L.max_open:
            soft.append(f"posisi terbuka > {L.max_open}")
        if slippage > L.max_slippage:
            soft.append(f"slippage {slippage:.1%} > {L.max_slippage:.0%}")
        if liquidity is not None and liquidity < L.min_liquidity:
            soft.append(f"likuiditas {liquidity:.1f} < {L.min_liquidity:.1f} SOL")
        if self.mode == "enforce":
            hard, soft = hard + soft, []
        return Decision(not hard, hard, soft)


def replay(trades, limits=RiskLimits()):
    """Seandainya batas rugi harian & drawdown ditegakkan: transaksi mana yang masih terjadi?
    trades: [{"opened", "closed", "pnl", "cost", ...}] -> (transaksi yang lolos, jumlah yang diblokir)."""
    rm = RiskManager(limits, mode="enforce", kill_file=BASE_DIR / ".tidak_ada_kill_switch")
    events = []
    for i, t in enumerate(trades):
        events.append((t.get("opened", t["closed"]), 0, i))
        events.append((t["closed"], 1, i))
    kept, blocked, live = [], 0, set()
    for ts, kind, i in sorted(events):
        t = trades[i]
        if kind == 0:
            d = rm.check_entry("x", i, t["cost"], now=ts)
            # hanya batas hasil (rugi harian & drawdown) yang diuji ulang; exposure sudah dibatasi simulator
            if any(r.startswith(("rugi harian", "drawdown")) for r in d.blocked_by):
                blocked += 1
                continue
            rm.on_open("x", i, t["cost"], now=ts)
            live.add(i)
        elif i in live:
            rm.on_close("x", t["cost"], t["pnl"], now=ts)
            kept.append(t)
    return kept, blocked
