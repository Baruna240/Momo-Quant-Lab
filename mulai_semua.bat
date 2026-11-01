@echo off
cd /d "%~dp0"
rem Membuka simulator dan dashboard di dua jendela terpisah (langsung di-minimize)
rem JANGAN tutup jendela "Simulator meme coin" kalau ingin simulasi tetap berjalan.
start "Simulator meme coin - JANGAN DITUTUP" /min cmd /k python sim.py
start "Simulator altcoin - JANGAN DITUTUP" /min cmd /k python altcoin.py
timeout /t 3 /nobreak >nul
start "Dashboard meme coin" /min cmd /k python dashboard.py
rem Supervisor 8 agent: siklus tiap 10 menit (Champion vs Challenger sekali sehari). Hanya membaca data.
start "Supervisor agent" /min cmd /k python -m agents.supervisor --loop 600
