# Simulator Meme Coin (pump.fun / Solana)

**Paper trading: TIDAK memakai uang sungguhan dan TIDAK butuh wallet.**
Tujuannya menjawab dengan data nyata: *"Kalau modal $20 dibagi rata ke 20 meme coin baru, apakah untung?"*

## Cara kerja

1. Mendengarkan **setiap token baru** di pump.fun secara real-time (sekitar 30–40 token per menit).
2. Membaca **harga asli dari blockchain** (bonding curve) setiap 4 detik lewat Helius.
3. Dua strategi berjalan **bersamaan**, masing-masing dengan modal sendiri:

| Strategi | Cara membeli |
|---|---|
| `langsung` | Membeli **setiap** token baru beberapa detik setelah diluncurkan (ide awal Anda) |
| `filter` | Mengamati dulu 90 detik. Hanya membeli kalau: sudah ada minimal 5 SOL yang masuk, harga naik 1,3–4× tanpa pernah anjlok lebih dari 25%, dan 30 detik terakhir masih naik |

4. Aturan jual otomatis (sama untuk kedua strategi):
   - **Stop loss** −40%
   - **TP1** di 2×: jual 50%
   - **TP2** di 4×: jual 25%
   - Sisanya memakai **trailing stop** 30% dari puncak
   - Jual juga kalau **30 menit belum mencapai TP1**, atau **harga diam 10 menit** (koin mati)
5. Biaya dihitung: fee 1% per sisi, biaya jaringan, dan **slippage dinamis**. Slippage minimal 0,5% kalau harga diam, lalu membesar sesuai kecepatan gerak harga dalam 8 detik terakhir (setengah dari besarnya gerakan), dengan batas maksimal 10%. Koin sepi tanpa transaksi tidak lagi dikenai biaya yang tidak realistis.
6. **Harga real-time:** koin yang sedang dipegang dipantau lewat WebSocket Helius, jadi setiap transaksi beli/jual langsung terlihat dan stop loss/keluar darurat bereaksi seketika. Pembacaan berkala (status "confirmed") tetap berjalan sebagai cadangan. Catatan: rug yang terjadi dalam **satu transaksi** tetap tidak bisa dihindari, karena saat terlihat, harganya sudah jatuh.
6. Setiap 20 koin = 1 **putaran** ($20). Saat putaran selesai, hasilnya dikirim ke Telegram.

## Persiapan (sekali saja)

1. Buka file **`.env`** di folder ini dengan Notepad (klik kanan → Open with → Notepad).
2. Isi tiga baris ini, langsung setelah tanda `=`, tanpa spasi dan tanpa tanda kutip:
   ```
   TELEGRAM_BOT_TOKEN=token dari BotFather
   TELEGRAM_CHAT_ID=angka Chat ID Anda
   HELIUS_API_KEY=API key dari dashboard Helius
   ```
3. Simpan (Ctrl+S).

> 🔒 Jangan pernah membagikan isi file `.env` ke siapa pun, termasuk ke chat AI.

## Menjalankan

- **Klik dua kali `jalankan.bat`.** Jendela hitam akan terbuka dan menampilkan aktivitas simulator.
- Di Telegram, kirim ke bot Anda:
  - `/status`: posisi dan putaran yang sedang berjalan
  - `/laporan`: hasil keseluruhan
- **Berhenti:** tekan **Ctrl+C** di jendela hitam (atau tutup jendelanya). Posisi yang masih terbuka **tidak ditutup**. Saat simulator dijalankan lagi, posisi itu **dilanjutkan** beserta putarannya, seolah simulator tidak pernah berhenti.
- **Lihat laporan kapan saja:** klik dua kali **`laporan.bat`**.

Semua hasil tersimpan di `simulasi.db`. Kalau simulator dijalankan lagi, hasilnya **dilanjutkan**, tidak diulang dari nol. Untuk mulai dari nol, hapus file `simulasi.db`.

## Strategy Lab (8 varian diuji bersamaan)

Simulator menguji **8 varian strategi sekaligus** pada token-token yang sama. Masing-masing punya modal $20 per putaran sendiri. Daftarnya ada di `strategies.py`:

| # | Varian | Aturan |
|---|---|---|
| 1 | Langsung | Beli setiap koin baru, aturan jual standar |
| 2 | Filter 90d | Amati 90 dtk, ≥5 SOL masuk, harga 1,3–4x |
| 3 | Langsung + cut loss | Beli semua, SL −20%, buang koin yang diam 3 menit |
| 4 | Filter cepat 45d | Amati 45 dtk, ≥3 SOL, harga 1,2–3x |
| 5 | Filter lambat 180d | Amati 3 menit, ≥10 SOL, harga 1,5–6x |
| 6 | Momentum 60d | Amati 60 dtk, ≥8 SOL, sudah naik 2–6x |
| 7 | Filter + SL ketat | Filter 90d, SL −25%, trailing 20% |
| 8 | Filter + TP tinggi | Filter 90d, TP1 3x, TP2 8x, trailing 35% |

> **Varian 1 & 3 (Langsung, Langsung + cut loss) sudah DIPENSIUNKAN** sejak 29/10: dari 2.700+ posisi rugi sekitar −$400. Datanya tetap di database. Untuk menghidupkannya lagi, sebutkan namanya di `STRATEGIES` pada `.env`.

Untuk menambah varian, cukup tambahkan satu baris di `VARIANTS` (file `strategies.py`), lalu restart simulator. **Jangan mengganti nama varian yang sudah punya data.**

### Varian ke-9: Auto Terbaik 🤖
Varian ini tidak punya aturan sendiri. Di **awal setiap putaran**, ia menilai 60 posisi terakhir dari setiap varian lain, lalu **mengikuti aturan varian yang paling stabil**. Skornya adalah rata-rata hasil per posisi dikurangi ketidakpastiannya, jadi varian yang untung karena satu kebetulan tidak langsung dipilih.
- **PEMANASAN:** belum ada varian dengan ≥10 posisi, jadi sementara mengikuti Filter 90d.
- **IKUT …:** mengikuti varian terbaik selama satu putaran.
- **ISTIRAHAT:** varian terbaik pun terbukti rugi (≥30 posisi, rata-rata negatif), jadi **tidak trading**. Penilaian diulang setiap 10 menit.

Setiap keputusan dicatat di tabel `auto_log` dan dikirim ke Telegram saat berubah. Hasil Auto Terbaik ditampilkan di leaderboard, sehingga bisa **dibuktikan** apakah "AI memilih sendiri" lebih baik daripada satu varian tetap.

### Data belajar AI (pencatatan ciri koin)
`features.py` mengamati **semua token baru** (yang bukan spam) dan **semua koin yang dibeli bot** selama 20 menit. Yang disimpan di tabel `token_features`:
- detik ke-30/60/90/180: kelipatan harga dari peluncuran, SOL yang sudah masuk, penurunan terdalam, puncak tertinggi, dan **berapa kali harga berubah** (tanda ramai/sepinya transaksi)
- ciri saat lahir (gratis, dari feed pump.fun): **SOL dan persen token yang dibeli dev**, **berapa koin lain yang dibuat dompet dev yang sama dalam 24 jam** (tanda pembuat koin serial), **berapa koin bernama sama dalam 1 jam** (tanda narasi yang sedang ditiru), mode mayhem
- metadata kalau bisa diambil: ada link X / Telegram / website, panjang deskripsi
- **jejak harga lengkap** (kolom `path`): 3 menit pertama dibaca setiap 4 detik, sehingga aturan jual apa pun bisa disimulasikan ulang secara offline
- hasil akhir: harga tertinggi dan terendah, waktu menuju 2x, lulus ke DEX atau tidak, dan harga di menit ke-20

Baris dengan `ver = 2` adalah data lengkap (mulai 29/10). Data lama tetap ada, tapi kolom-kolom barunya kosong.

Data ini **belum dipakai untuk keputusan apa pun**. Ini bahan untuk melatih model AI penilai koin di tahap berikutnya.

### Anti-rug (varian 10 & 11)
**Filter + Anti-rug** dan **Momentum + Anti-rug** memakai aturan yang sama dengan varian asalnya, ditambah pemeriksaan lewat Helius **sebelum membeli**:
- **Dev sudah menjual ≥50%** dari token yang dibelinya saat peluncuran → tolak
- **1 dompet memegang >10%** atau **5 dompet terbesar memegang >30%** suplai → tolak. Bonding curve dan akun protokol pump.fun (token "mayhem mode" bersuplai 2 miliar) tidak dihitung sebagai holder.
- Kalau pemeriksaan gagal (misalnya RPC error) → **tidak membeli** (pilihan yang lebih aman)
- Selama posisi terbuka, dev dicek ulang **setiap 30 detik**. Kalau dev mulai menjual, posisi **langsung ditutup** ("dev dump").

Batasnya bisa diubah di `.env`: `ANTIRUG_MAX_HOLDER_PCT`, `ANTIRUG_MAX_TOP5_PCT`, `ANTIRUG_MAX_DEV_SOLD`.

### Moonbag (varian 12 & 13) 🌙
Menguji pertanyaan **"lebih untung ambil profit cepat atau menahan untuk kenaikan besar?"**. Cara masuknya sama persis dengan varian asalnya, tapi cara jualnya berbeda:

| | Standar | Moonbag |
|---|---|---|
| Di 2x | jual 50% | jual **30%** |
| TP2 di 4x | jual 25% | **tidak ada** |
| Sisa yang ditahan | 25% | **70%** |
| Trailing stop | 30% dari puncak | **40%** dari puncak |
| Batas waktu setelah TP1 | 3 jam | praktis tanpa batas (30 hari) |

- **Filter lambat + Moonbag**: bandingkan dengan **Filter lambat 180d**.
- **Momentum + Anti-rug + Moonbag**: bandingkan dengan **Momentum + Anti-rug**.

Stop loss sebelum 2x (−40%) dan aturan "koin mati" (harga diam 10 menit) tetap berlaku.

### Rem Kelly
Setiap varian juga dihitung **"bagaimana hasilnya kalau ukuran taruhan diatur rumus Kelly"**, dicatat berdampingan dengan hasil modal tetap $1:
- **BELAJAR** (kurang dari 30 posisi selesai): taruhan tetap $1, karena datanya belum cukup.
- **STOP**: hasil varian menunjukkan keunggulan negatif, jadi Kelly menyarankan taruhan **$0**. Inilah "rem otomatis".
- **AKTIF**: taruhan = ¼ × Kelly × modal varian, maksimal 3× modal tetap.

Pengaturannya ada di `.env`: `KELLY_FRACTION`, `KELLY_MIN_SAMPLE`, `KELLY_MAX_STAKE_MULT`. Kalau notifikasi putaran di Telegram terlalu banyak, tambahkan `NOTIFY_BATCHES=false`.

## Simulator altcoin & koin besar 🪙
Modul terpisah (`altcoin.py`, database `altcoin.db`) yang memantau **30 koin terbesar** (CoinGecko, tanpa stablecoin/token emas) ditambah **koin incaran** Anda: MITO, STRK, DYM, IOST, SC. Harga dan candle diambil dari data publik Binance. Ini juga **simulasi, tanpa uang sungguhan**.

- Jalankan: `altcoin.bat` (sudah ikut `mulai_semua.bat` dan autostart)
- Dashboard: **http://localhost:8050/alt** (atau klik **ALTCOIN →** di dashboard meme coin)
- Telegram: ketik **/alt** untuk ringkasan. Setiap sinyal beli/jual dikirim otomatis (matikan dengan `ALT_NOTIFY=false`).

| Strategi | Timeframe | Masuk | Keluar |
|---|---|---|---|
| Trend EMA 1j | 1 jam | EMA20 memotong ke atas EMA50, harga di atas EMA200 | EMA20 < EMA50, atau trailing 3×ATR |
| Breakout 48j | 1 jam | Tembus titik tertinggi 48 jam dengan volume 1,5× | Tembus titik terendah 24 jam, atau trailing 3×ATR |
| Beli saat turun (RSI) | 1 jam | RSI < 30 tapi EMA50 > EMA200 | RSI > 55, atau setelah 48 jam |
| Swing 4j | 4 jam | EMA21 memotong ke atas EMA55, harga di atas EMA200 | EMA21 < EMA55, atau trailing 3,5×ATR |
| HODL BTC | – | Beli BTC sekali di awal | Tidak pernah dijual (pembanding) |

- Modal **$100 per strategi**, dibagi ke maksimal **5 posisi** bersamaan. Fee 0,1% per sisi + slippage (0,05% koin 10 besar, 0,15% lainnya).
- Stop loss awal = harga masuk − beberapa × ATR (ukuran gejolak koin itu). Stop dicek **setiap menit**, sinyal dicek setiap candle 1 jam / 4 jam tertutup.
- **Backtest otomatis** dijalankan tiap 24 jam (1 jam: 3.000 candle ≈ 117 hari, 4 jam: 1.000 candle ≈ 133 hari). Bisa juga manual: `python altcoin.py --backtest`.

### Futures (simulasi) & AI
Strategi futures bisa **LONG** (untung saat harga naik) dan **SHORT** (untung saat harga turun) dengan leverage. Masing-masing juga punya modal simulasi **$100**.

| Strategi | Leverage | Masuk | Keluar |
|---|---|---|---|
| Futures Swing 2 arah | 3× | 4 jam: EMA21 memotong EMA55 searah EMA200 (LONG/SHORT) | Tren berbalik, atau trailing 3,5×ATR |
| Futures Short Breakdown | 3× | SHORT saat jebol titik terendah 48 jam + volume 1,5×, di bawah EMA200 | Tembus titik tertinggi 24 jam, atau trailing 3×ATR |
| Futures Short saat naik (RSI) | 2× | SHORT saat RSI > 70 dalam tren turun (EMA50 < EMA200) | RSI < 45, atau setelah 48 jam |
| **Futures AI** | 2× | Model AI yakin harga 12 jam ke depan NAIK (LONG) atau TURUN (SHORT) | 12 jam, tebakan AI berbalik, atau trailing 2,5×ATR |

- **Cara AI belajar** (`alt_ai.py`): dari ±95.000 candle 1 jam (35 koin), model *gradient boosting* mempelajari 18 ciri (momentum 1–72 jam, RSI, jarak ke EMA, volatilitas, volume, posisi dalam range, kondisi BTC) untuk menebak apakah 12 jam ke depan harga NAIK, TURUN, atau DATAR. AI hanya bertindak pada 10% tebakan paling yakin. Model **dilatih ulang tiap 24 jam** dengan data terbaru dan disimpan di `models/alt_ai.pkl`.
- **Pengaman:** AI hanya boleh membuka posisi ke arah (LONG/SHORT) yang **terbukti** di data uji: minimal 200 sinyal, benar > 50%, dan rata-rata > 0,2%. Kalau belum ada arah yang lolos, AI menunggu dan tidak membuka posisi.
- **Penilaian jujur:** kualitas AI diukur di data yang **tidak ikut dipelajari**. Backtest-nya *walk-forward*: belajar dari 70% data awal, lalu trading di 30% sisanya. Hasil latihan tampil di panel **FUTURES AI · HASIL LATIHAN** di dashboard.
- Realisme: fee futures 0,05% per sisi dari nilai kontrak, **funding rate** OKX dibayar tiap 8 jam, dan **likuidasi** isolated margin (margin habis kalau harga menyentuh harga likuidasi; stop selalu dipasang sebelum harga itu). Harga memakai candle spot Binance sebagai pendekatan, dan hanya koin yang punya kontrak perpetual (di OKX) yang di-trade.
- Hasil futures dihitung **terhadap margin**, jadi untung dan rugi ikut dikali leverage.
- Posisi tersimpan di database, jadi dilanjutkan setelah restart.
- Pengaturan di `.env`: `ALT_BUDGET_USD`, `ALT_MAX_POSITIONS`, `ALT_TOP_N`, `ALT_WATCHLIST` (contoh `MITO,STRK,DYM,IOST,SC,ARB`), `ALT_NOTIFY`.

## Kendali & ketahanan

### Perintah Telegram
| Perintah | Fungsi |
|---|---|
| `/status` atau `/varian` | Peringkat semua varian, status Kelly, dan putaran yang sedang berjalan |
| `/laporan` | Hasil keseluruhan |
| `/stop` | **Jeda trading**: tidak ada posisi baru, tapi posisi yang terbuka tetap dikelola sampai selesai. Status ini **diingat** walaupun simulator di-restart |
| `/mulai` | Lanjutkan trading |

### Jalan otomatis saat laptop menyala
- Untuk mengaktifkan: klik dua kali **`pasang_autostart.bat`**. Setelah itu, setiap Windows menyala, simulator dan dashboard berjalan sendiri (minimize, menunggu 30 detik dulu supaya internet siap).
- Untuk mematikan: klik dua kali **`hapus_autostart.bat`**.
- Laptop tetap harus **tidak sleep**: Settings → System → Power → Sleep = Never (saat dicolok charger).

### Backup harian
Database otomatis di-backup **sekali sehari** ke folder `backups/` (7 hari terakhir disimpan). Kalau `simulasi.db` rusak, salin backup terbaru, lalu ganti namanya menjadi `simulasi.db`.

### Tidak bisa berjalan dobel
Kalau `mulai_semua.bat` diklik saat simulator sudah berjalan, jendela kedua hanya menampilkan pesan *"Simulator sudah berjalan…"* lalu menutup sendiri. Data tidak akan dobel.

## Dashboard web

- **Klik dua kali `mulai_semua.bat`** untuk membuka simulator dan dashboard sekaligus, masing-masing di jendela sendiri. Browser akan terbuka di **http://localhost:8050**.
- Kalau simulator sudah berjalan, cukup klik dua kali **`dashboard.bat`**.
- Isi dashboard (diperbarui otomatis setiap 10 detik):
  - **Kartu hasil per strategi:** hasil bersih, ROI, win rate, putaran untung, hasil terbaik
  - **Kurva untung-rugi kumulatif** kedua strategi
  - **Distribusi puncak:** berapa koin yang sempat naik 1–2×, 2–5×, 5–10×, dan ≥10×
  - **Chart setiap koin** (klik baris di tabel): harga dalam kelipatan harga beli, penanda BELI/JUAL, garis stop loss dan TP, market cap, plus tautan ke pump.fun dan DexScreener
  - **Tabel** koin yang sedang dipegang, yang sudah ditutup, dan hasil tiap putaran
- Dashboard hanya **membaca** `simulasi.db`, jadi aman dibuka-tutup kapan saja tanpa mengganggu simulator.
- Dashboard hanya bisa dibuka dari komputer ini (localhost), tidak bisa diakses orang lain dari internet.

## Mengubah pengaturan

Semua angka bisa diubah di `.env` (hapus tanda `#` di depan barisnya). Daftar lengkap dan penjelasannya ada di `config.py`. Contoh:
```
BATCH_BUDGET_USD=20
COINS_PER_BATCH=20
STRATEGIES=filter
NOTIFY_EACH_CLOSE=true
```

## Cara membaca hasil

- **Hasil bersih / win rate:** apakah strategi untung setelah biaya.
- **"Sempat menyentuh ≥10×"**: berapa koin yang *pernah* naik sejauh itu, meskipun bot tidak sempat menjual di puncaknya.
- **Putaran untung vs rugi:** jawaban langsung untuk pertanyaan "apakah modal $20 tertutup?".
- Jalankan **minimal beberapa hari** (puluhan putaran) sebelum menarik kesimpulan. Hasil 1–2 putaran masih sangat dipengaruhi keberuntungan.

## Keterbatasan (penting)

- Ini **simulasi**. Di dunia nyata, bot bisa lebih lambat, slippage bisa lebih besar, dan transaksi bisa gagal. **Kalau simulasi rugi, versi nyata hampir pasti juga rugi.** Kalau simulasi untung, belum tentu versi nyata untung.
- Selama di bonding curve pump.fun, harga **maksimal sekitar 14,7×** dari harga peluncuran. Kenaikan di atas itu hanya mungkin setelah token **lulus ke DEX** (harga lalu dibaca dari DexScreener).
- Biaya sewa token account (~0,002 SOL per token) **tidak dihitung**, karena bisa ditarik kembali setelah token dijual. Tapi di dunia nyata, untuk posisi $1, dana itu tertahan sementara dan jumlahnya cukup besar.
- Paket gratis Helius punya batas kuota bulanan. Simulator membaca harga setiap 4 detik (`POLL_SEC`). Kalau kuota cepat habis, naikkan ke 6–8.
