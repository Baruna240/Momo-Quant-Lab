"""Strategy Lab: daftar varian strategi yang diuji bersamaan pada feed token yang sama.

Setiap varian mewarisi pengaturan dasar dari config.py/.env lalu mengganti sebagian angkanya.
Menambah varian baru cukup dengan menambah satu baris di VARIANTS.
Nama varian dipakai di database: jangan diganti setelah ada datanya.
"""
from dataclasses import dataclass

ENTRY_FIELDS = ("entry_delay_sec", "observe_sec", "filter_min_real_sol", "filter_min_mult", "filter_max_mult",
                "filter_max_drawdown", "antirug", "antirug_max_holder_pct", "antirug_max_top5_pct",
                "antirug_max_dev_sold")
EXIT_FIELDS = ("stop_loss", "tp1_mult", "tp1_sell", "tp2_mult", "tp2_sell", "trailing_stop", "max_hold_min",
               "max_hold_after_tp_min", "stale_min")

# aturan jual moonbag: TP2 dimatikan (angka sangat tinggi), batas waktu setelah TP1 = 30 hari
NO_TP2 = 1e9
MOONBAG = {"tp1_mult": 2.0, "tp1_sell": 0.30, "tp2_mult": NO_TP2, "trailing_stop": 0.40, "max_hold_after_tp_min": 43200}

# (nama, mode, label, penjelasan, pengaturan yang diganti)
VARIANTS = [
    ("langsung", "langsung", "Langsung", "Beli setiap koin baru, aturan jual standar", {}),
    ("filter", "filter", "Filter 90d", "Amati 90 dtk, ≥5 SOL masuk, harga 1,3–4x", {}),
    ("langsung_cl", "langsung", "Langsung + cut loss", "Beli semua, tapi SL −20% & buang koin diam 3 mnt",
     {"stop_loss": 0.20, "stale_min": 3, "max_hold_min": 10}),
    ("filter_cepat", "filter", "Filter cepat 45d", "Amati 45 dtk, ≥3 SOL, harga 1,2–3x",
     {"observe_sec": 45, "filter_min_real_sol": 3, "filter_min_mult": 1.2, "filter_max_mult": 3.0}),
    ("filter_lambat", "filter", "Filter lambat 180d", "Amati 3 mnt, ≥10 SOL, harga 1,5–6x",
     {"observe_sec": 180, "filter_min_real_sol": 10, "filter_min_mult": 1.5, "filter_max_mult": 6.0}),
    ("momentum", "filter", "Momentum 60d", "Amati 60 dtk, ≥8 SOL, sudah naik 2–6x",
     {"observe_sec": 60, "filter_min_real_sol": 8, "filter_min_mult": 2.0, "filter_max_mult": 6.0}),
    ("filter_sl_ketat", "filter", "Filter + SL ketat", "Filter 90d, SL −25%, trailing 20%",
     {"stop_loss": 0.25, "trailing_stop": 0.20}),
    ("filter_tp_tinggi", "filter", "Filter + TP tinggi", "Filter 90d, TP1 3x, TP2 8x, trailing 35%",
     {"tp1_mult": 3.0, "tp2_mult": 8.0, "trailing_stop": 0.35}),
    # varian khusus: tidak punya aturan sendiri, tiap putaran mengikuti varian lain yang paling stabil
    ("auto", "auto", "Auto Terbaik", "AI memilih varian paling stabil tiap putaran; istirahat kalau semua terbukti rugi", {}),
    # anti-rug: aturan sama dengan varian asalnya + cek dev & konsentrasi holder (pembanding yang adil)
    ("filter_antirug", "filter", "Filter + Anti-rug", "Filter 90d + tolak koin yang dev-nya menjual / holder terkonsentrasi; keluar jika dev menjual",
     {"antirug": True}),
    ("momentum_antirug", "filter", "Momentum + Anti-rug", "Momentum 60d + cek dev & konsentrasi holder",
     {"observe_sec": 60, "filter_min_real_sol": 8, "filter_min_mult": 2.0, "filter_max_mult": 6.0, "antirug": True}),
    # moonbag: cara masuk sama dengan varian asalnya, tapi di 2x hanya jual 30% (tanpa TP2) lalu 70% sisanya
    # ditahan dengan trailing 40% dan praktis tanpa batas waktu -> menguji "tahan lama" vs "ambil untung cepat"
    ("lambat_moonbag", "filter", "Filter lambat + Moonbag", "Filter lambat 180d; di 2x jual 30%, sisa 70% ditahan (trailing 40%, tanpa batas waktu)",
     {"observe_sec": 180, "filter_min_real_sol": 10, "filter_min_mult": 1.5, "filter_max_mult": 6.0, **MOONBAG}),
    ("momentum_moonbag", "filter", "Momentum + Anti-rug + Moonbag", "Momentum + Anti-rug; di 2x jual 30%, sisa 70% ditahan (trailing 40%, tanpa batas waktu)",
     {"observe_sec": 60, "filter_min_real_sol": 8, "filter_min_mult": 2.0, "filter_max_mult": 6.0, "antirug": True, **MOONBAG}),
]


@dataclass
class Variant:
    name: str
    mode: str          # "langsung" atau "filter"
    label: str
    desc: str
    order: int         # urutan tetap -> warna tetap di dashboard
    entry_delay_sec: float
    observe_sec: float
    filter_min_real_sol: float
    filter_min_mult: float
    filter_max_mult: float
    filter_max_drawdown: float
    antirug: bool
    antirug_max_holder_pct: float
    antirug_max_top5_pct: float
    antirug_max_dev_sold: float
    stop_loss: float
    tp1_mult: float
    tp1_sell: float
    tp2_mult: float
    tp2_sell: float
    trailing_stop: float
    max_hold_min: float
    max_hold_after_tp_min: float
    stale_min: float


# varian yang sudah terbukti rugi berat: tidak dijalankan lagi (datanya tetap ada di database).
# Bisa dihidupkan lagi dengan menyebutnya di STRATEGIES pada .env.
RETIRED = {"langsung", "langsung_cl"}


def build_variants(settings):
    wanted = set(settings.strategy_list)
    out = []
    for order, (name, mode, label, desc, overrides) in enumerate(VARIANTS):
        if (wanted and name not in wanted) or (not wanted and name in RETIRED):
            continue
        base = {f: getattr(settings, f) for f in ENTRY_FIELDS + EXIT_FIELDS}
        base.update(overrides)
        out.append(Variant(name=name, mode=mode, label=label, desc=desc, order=order, **base))
    return out
