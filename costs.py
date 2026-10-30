"""Model biaya meme coin bersama: dipakai simulator live (sim.py) DAN replay/backtest (meme_ai.py, wallet_lab.py),
supaya keduanya tidak pernah berbeda.

Slippage asimetris, dikalibrasi dengan harga on-chain (calibrate.py, 31 Okt 2026, 300 transaksi, telat 1-5 detik):
- beli : nyata median -0,04%, rata-rata -0,3%  -> model 0,5% tetap (bantalan konservatif untuk dampak order & priority fee)
- jual : nyata median 1,2%,  rata-rata 3,4%    -> model 0,5% + 0,2 x gerak harga 8 detik terakhir (rata-rata model ~3,4%)
  Jual dibuat mengikuti gerak harga karena penjualan sering terjadi saat harga sedang jatuh (stop loss, dev dump).
Model lama (sebelum 31 Okt 2026): 0,5% + 0,5 x gerak harga di kedua sisi -> terlalu pesimis 3-6x.
"""
SLIP_MODEL_VERSION = 2


def slippage(settings, move, side):
    """move = |harga sekarang / harga ~8 detik lalu - 1|; side = "buy" atau "sell". Hasil: pecahan (0.01 = 1%)."""
    k = settings.slip_buy_k if side == "buy" else settings.slip_sell_k
    return min(settings.slippage_max, settings.slippage_min + k * abs(move))
