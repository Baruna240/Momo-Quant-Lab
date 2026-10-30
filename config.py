"""Pengaturan simulator. Nilai bisa diubah lewat file .env (nama variabel = huruf besar)."""
import os
from dataclasses import dataclass, fields
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "simulasi.db"


def load_env(path=BASE_DIR / ".env"):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


@dataclass
class Settings:
    # --- rahasia (diisi di .env) ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    helius_api_key: str = ""

    # --- modal ---
    batch_budget_usd: float = 20.0      # modal per putaran
    coins_per_batch: int = 20           # dibagi rata ke sekian koin
    strategies: str = ""                # kosong = semua varian di strategies.py; atau "langsung,filter"

    # --- rem Kelly (dihitung per varian dari hasilnya sendiri) ---
    kelly_fraction: float = 0.25        # pakai 1/4 Kelly (Kelly penuh terlalu agresif)
    kelly_min_sample: int = 30          # sebelum 30 posisi selesai: fase belajar, modal tetap
    kelly_max_stake_mult: float = 3.0   # taruhan Kelly maksimal 3x modal tetap per koin

    # --- masuk (beli) ---
    entry_delay_sec: float = 3.0        # "langsung": jeda realistis setelah token dibuat
    observe_sec: float = 90.0           # "filter": lama mengamati token sebelum memutuskan
    filter_min_real_sol: float = 5.0    # minimal SOL yang sudah masuk ke bonding curve
    filter_min_mult: float = 1.3        # harga minimal sudah naik 1,3x dari peluncuran
    filter_max_mult: float = 4.0        # tapi belum lebih dari 4x (jangan beli di pucuk)
    filter_max_drawdown: float = 0.25   # buang jika harga pernah turun >25% di bawah peluncuran

    # --- anti-rug (hanya untuk varian yang mengaktifkannya, lihat strategies.py) ---
    antirug: bool = False
    antirug_max_holder_pct: float = 0.10  # lewati jika 1 dompet (selain bonding curve) pegang >10% suplai
    antirug_max_top5_pct: float = 0.30    # lewati jika 5 dompet terbesar pegang >30% suplai
    antirug_max_dev_sold: float = 0.50    # lewati / keluar jika pembuat koin sudah jual >=50% tokennya
    antirug_dev_check_sec: float = 30.0   # cek ulang dev untuk posisi terbuka tiap 30 detik

    # --- keluar (jual) ---
    stop_loss: float = 0.40             # jual semua jika rugi 40% (sebelum TP1)
    tp1_mult: float = 2.0               # take profit 1 di 2x ...
    tp1_sell: float = 0.50              # ... jual 50% dari jumlah awal
    tp2_mult: float = 4.0               # take profit 2 di 4x ...
    tp2_sell: float = 0.25              # ... jual 25% dari jumlah awal
    trailing_stop: float = 0.30         # setelah TP1: jual sisa jika turun 30% dari puncak
    max_hold_min: float = 30.0          # jual jika 30 menit belum menyentuh TP1
    max_hold_after_tp_min: float = 180.0
    stale_min: float = 10.0             # jual jika harga tidak bergerak 10 menit (koin mati)

    # --- biaya (perkiraan) ---
    fee_pct: float = 0.01               # fee platform per sisi
    tx_cost_sol: float = 0.0001         # biaya jaringan + priority fee per transaksi
    # slippage dinamis: minimal kecil, membesar kalau harga sedang bergerak cepat saat transaksi
    # slippage = min + k x |perubahan harga dalam 8 detik terakhir|, dibatasi maksimal
    slippage_min: float = 0.005         # slippage dasar per sisi (model terkalibrasi, lihat costs.py)
    slip_buy_k: float = 0.0             # beli: tidak bergantung gerak harga (nyata ~0%)
    slip_sell_k: float = 0.2            # jual: + 0,2 x gerak harga 8 detik terakhir (nyata rata-rata ~3,4%)
    slippage_max: float = 0.10
    slip_lookback_sec: float = 8.0

    # --- sistem ---
    poll_sec: float = 4.0               # interval baca harga dari blockchain
    sol_usd_fallback: float = 100.0
    notify_each_close: bool = False     # kirim Telegram untuk setiap posisi ditutup
    notify_batches: bool = True         # kirim Telegram setiap putaran selesai
    big_win_mult: float = 2.0           # selalu kirim notifikasi jika hasil >= 2x modal
    summary_every_min: float = 60.0     # ringkasan berkala ke Telegram
    backup_keep: int = 7                # simpan backup database harian 7 hari terakhir
    risk_mode: str = "shadow"           # "shadow": batas risiko dicatat saja (riset); "enforce": benar-benar memblokir

    @property
    def per_coin_usd(self):
        return self.batch_budget_usd / self.coins_per_batch

    @property
    def strategy_list(self):
        return [s.strip() for s in self.strategies.split(",") if s.strip()]

    @property
    def rpc_url(self):
        if self.helius_api_key:
            return f"https://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"
        return "https://api.mainnet-beta.solana.com"


def load_settings():
    load_env()
    s = Settings()
    for f in fields(Settings):
        raw = os.environ.get(f.name.upper())
        if raw is None or raw == "":
            continue
        kind = type(getattr(s, f.name))
        if kind is bool:
            value = raw.lower() in ("1", "true", "yes", "ya")
        else:
            value = kind(raw)
        setattr(s, f.name, value)
    return s
