# Memindahkan Momo Quant Lab ke VPS

Hasil akhir: simulator meme, simulator altcoin, dashboard, dan supervisor agent berjalan 24 jam di VPS Ubuntu,
menyala otomatis setelah server restart, dashboard tidak terbuka ke internet (diakses lewat terowongan SSH).

## 1. Buat kunci SSH (di laptop, sekali saja)
PowerShell:
```
ssh-keygen -t ed25519 -C "momo-vps"
```
Tekan Enter untuk lokasi bawaan. Passphrase boleh diisi (lebih aman) atau dikosongkan.
Lalu tampilkan kunci **publik** (aman dibagikan; yang rahasia adalah file tanpa `.pub`):
```
type $env:USERPROFILE\.ssh\id_ed25519.pub
```

## 2. Buat VPS (di situs penyedia, mis. DigitalOcean lewat GitHub Student Pack)
- Image: **Ubuntu 24.04 LTS**
- Region: **Singapore**
- Ukuran: 2 GB RAM / 1 vCPU (cukup) atau 4 GB / 2 vCPU (lebih lega untuk riset)
- Authentication: **SSH Key** → tempel isi `id_ed25519.pub` dari langkah 1
- Hostname: `momo-quant`
Catat alamat IP-nya.

## 3. Pasang semuanya (satu perintah)
```
ssh root@IP_VPS
curl -fsSL https://raw.githubusercontent.com/Baruna240/Momo-Quant-Lab/main/deploy/setup_vps.sh | bash
exit
```

## 4. Pindahkan data & .env dari laptop
Di folder proyek (PowerShell):
```
powershell -ExecutionPolicy Bypass -File deploy\pindah_ke_vps.ps1 -Ip IP_VPS
```
Skrip ini menghentikan bot di laptop dulu supaya database tidak rusak saat disalin.

## 5. Nyalakan
```
ssh root@IP_VPS momo-start
```

## 6. Buka dashboard dari laptop
```
ssh -N -L 8050:localhost:8050 momo@IP_VPS
```
Biarkan jendela itu terbuka, lalu buka http://localhost:8050 di browser (semua tema: `/`, `/lab`, `/prism`, `/alt` ...).

## Perintah harian di VPS
| Perlu | Perintah |
|---|---|
| Status layanan | `systemctl status momo-sim momo-alt momo-dashboard momo-agents` |
| Log simulator meme | `journalctl -u momo-sim -f` |
| Ambil kode terbaru dari GitHub & restart | `momo-update` (sebagai root) |
| Hentikan sementara | `systemctl stop momo-sim momo-alt momo-dashboard momo-agents` |
| Kill switch (stop semua entry baru) | `touch /home/momo/momo/KILL_SWITCH` |

## Aturan penting
- Setelah pindah, **jangan jalankan `mulai_semua.bat` di laptop** (bot berjalan dobel, data bentrok).
- Jangan buka port 8050 di firewall; selalu lewat terowongan SSH.
- Isi `.env` hanya ada di laptop dan VPS milikmu; jangan ditempel ke chat mana pun.
