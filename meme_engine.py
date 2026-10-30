"""Mesin aturan jual meme coin: SATU sumber kebenaran untuk simulator live (sim.py) dan backtest/replay (meme_ai.py).

Fungsi murni: diberi aturan varian + keadaan posisi + harga & waktu sekarang -> daftar order jual.
Tidak menyentuh harga eksekusi, fee, atau slippage (itu urusan pemanggil, lewat costs.py), sehingga live dan replay
dijamin memakai logika keluar yang persis sama.

Urutan aturan (dicek setiap ada harga baru):
1. sebelum TP1 : stop loss  ->  TP1 (jual sebagian)  ->  batas waktu tanpa TP1
2. sesudah TP1 : TP2 (jual sebagian)  ->  trailing stop dari puncak  ->  batas waktu sesudah TP1 (dihitung dari waktu beli)
3. kapan saja  : koin mati (harga tidak bergerak selama stale_min menit)
"""
from dataclasses import dataclass

EPS = 1e-9


@dataclass
class ExitState:
    qty0: float              # jumlah token saat beli
    remaining: float         # sisa token
    entry: float             # harga beli (sudah termasuk slippage)
    peak: float              # harga pasar tertinggi sejak beli
    opened: float            # waktu beli (detik)
    last_change: float       # waktu terakhir harga pasar berubah
    tp1: bool = False
    tp2: bool = False
    tp1_time: float = 0.0


def exit_orders(c, st, price, now):
    """c: aturan varian (stop_loss, tp1_mult, tp1_sell, tp2_mult, tp2_sell, trailing_stop, max_hold_min,
    max_hold_after_tp_min, stale_min); st: ExitState (peak & tanda TP DIPERBARUI di sini, remaining TIDAK).
    Hasil: [(jumlah token, alasan)] untuk dijual pemanggil, sesuai urutan."""
    orders, rem = [], st.remaining

    def sell(q, reason):
        nonlocal rem
        q = min(q, rem)
        if q > EPS:
            orders.append((q, reason))
            rem -= q

    st.peak = max(st.peak, price)
    m = price / st.entry if st.entry else 0.0
    age_min = (now - st.opened) / 60
    if not st.tp1:
        if m <= 1 - c.stop_loss:
            sell(rem, "stop loss")
        elif m >= c.tp1_mult:
            st.tp1, st.tp1_time = True, now
            sell(st.qty0 * c.tp1_sell, f"TP1 {c.tp1_mult:g}x")
        elif age_min >= c.max_hold_min:
            sell(rem, "batas waktu")
    if rem > st.qty0 * EPS and st.tp1:
        if not st.tp2 and m >= c.tp2_mult:
            st.tp2 = True
            sell(st.qty0 * c.tp2_sell, f"TP2 {c.tp2_mult:g}x")
        if rem > st.qty0 * EPS:
            if price <= st.peak * (1 - c.trailing_stop):
                sell(rem, "trailing stop")
            elif age_min >= c.max_hold_after_tp_min:
                sell(rem, "batas waktu")
    if rem > st.qty0 * EPS and now - st.last_change >= c.stale_min * 60:
        sell(rem, "koin mati (harga diam)")
    return orders
