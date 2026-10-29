@echo off
rem Dipanggil otomatis saat Windows menyala (lihat pasang_autostart.bat).
rem Menunggu internet sebentar, lalu membuka simulator & dashboard (minimize, tanpa membuka browser).
cd /d "%~dp0"
timeout /t 30 /nobreak >nul
start "Simulator meme coin - JANGAN DITUTUP" /min cmd /k python sim.py
start "Simulator altcoin - JANGAN DITUTUP" /min cmd /k python altcoin.py
timeout /t 3 /nobreak >nul
start "Dashboard meme coin" /min cmd /k python dashboard.py --no-browser
