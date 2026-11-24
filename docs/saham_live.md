# Saham BEI: dari paper trading ke akun sungguhan

Semua order di `saham.py` (dan di halaman `/saham`) lewat kelas `Broker` di `saham_broker.py`.
Sekarang yang aktif hanya `PaperBroker`, yang memakai uang simulasi di `saham.db`. Kalau nanti mau pakai akun
sungguhan, cukup isi `LiveBroker`. Strategi dan dashboard tidak perlu diubah.

## Fakta penting sebelum mulai

- **Dompet digital tidak bisa dipakai untuk trading saham.** GoPay, OVO, DANA, ShopeePay dan sejenisnya tidak bisa
  menyimpan saham. Di Indonesia, saham hanya bisa dibeli lewat **perusahaan sekuritas** (Anggota Bursa berizin OJK)
  dengan **rekening efek + RDN** (Rekening Dana Nasabah) atas nama Anda sendiri. Sebagian aplikasi menerima top-up
  RDN lewat e-wallet atau virtual account, tapi order beli/jualnya tetap dijalankan oleh sekuritas.
- **API order untuk nasabah ritel jarang ada.** Sebagian besar sekuritas ritel belum membuka API trading publik.
  Tanyakan langsung ke sekuritas Anda apakah mereka punya API resmi untuk nasabah (kadang ada untuk nasabah
  tertentu atau lewat FIX untuk institusi).
- **Jangan pakai API tidak resmi** (misalnya hasil membongkar aplikasi HP sekuritas). Itu biasanya melanggar
  syarat layanan, bisa membuat akun diblokir, dan berisiko keamanan untuk uang Anda.

## Syarat sebelum pindah ke uang sungguhan

Ini disarankan, bukan aturan bursa:

1. Paper trading **forward** (bukan backtest) berjalan minimal 3 bulan.
2. Minimal sekitar 50 trade selesai per strategi. Dengan trade lebih sedikit, hasil masih banyak dipengaruhi
   kebetulan.
3. Setelah fee, return mengalahkan IHSG pada periode yang sama dan max drawdown masih bisa Anda terima.
4. Mulai dengan modal kecil (misalnya 1 lot per order).

## Cara mengisi `LiveBroker` (kalau sudah ada API resmi)

1. Isi `cash()`, `positions()` dan `place(order, quote)` di `saham_broker.py` memakai API resmi sekuritas.
   Kembalikan `Fill` dengan harga dan fee yang **benar-benar terjadi**, bukan perkiraan.
2. Simpan kunci API di `.env` (file ini tidak pernah di-commit), jangan di kode.
3. Tambahkan pengaman sebelum order dikirim:
   - batas nilai per order dan batas rugi harian (kalau terlewati, berhenti sendiri);
   - kill switch, misalnya file `STOP` yang menghentikan semua order;
   - catat setiap order ke `saham.db` seperti di mode paper.
4. Aktifkan dengan `SAHAM_BROKER=live` di `.env`. Selama `LiveBroker` belum diisi, mode ini sengaja gagal dengan
   pesan error, sehingga tidak ada order sungguhan yang terkirim karena salah setting.
