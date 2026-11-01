# Pindahkan data & .env dari laptop ke VPS (jalankan di PowerShell, dari folder proyek):
#   powershell -ExecutionPolicy Bypass -File deploy\pindah_ke_vps.ps1 -Ip 123.45.67.89
#
# 1. menghentikan simulator, dashboard & supervisor di laptop (database tidak boleh disalin saat sedang ditulis)
# 2. mengirim semua database + folder models + .env ke /home/momo/momo di VPS lewat SSH (scp)
# 3. memberi tahu langkah terakhir (momo-start di VPS)
# Isi .env tidak ditampilkan di mana pun; file langsung dikirim terenkripsi lewat SSH ke server milikmu.
param([Parameter(Mandatory = $true)][string]$Ip, [string]$User = "momo")
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

Write-Host "== 1/3 menghentikan proses Momo di laptop"
$procs = Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
  Where-Object { $_.CommandLine -match 'sim\.py|altcoin\.py|dashboard\.py|agents\.supervisor' }
foreach ($p in $procs) {
  $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($p.ParentProcessId)"
  if ($parent -and $parent.Name -eq 'cmd.exe') { Stop-Process -Id $parent.ProcessId -Force -ErrorAction SilentlyContinue }
  Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 3

Write-Host "== 2/3 mengirim data ke ${User}@${Ip}"
$files = @("simulasi.db", "altcoin.db", "flow.db", "wallet_lab.db", "research.db", "alt_history.db", "agents.db", ".env") |
  Where-Object { Test-Path $_ }
scp $files "${User}@${Ip}:/home/${User}/momo/"
if (Test-Path "models") { scp -r models "${User}@${Ip}:/home/${User}/momo/" }
ssh "${User}@${Ip}" "chmod 600 /home/${User}/momo/.env"

Write-Host "== 3/3 selesai"
Write-Host "Sekarang nyalakan di VPS:  ssh root@$Ip momo-start"
Write-Host "JANGAN jalankan mulai_semua.bat di laptop lagi (bot akan berjalan dobel)."
