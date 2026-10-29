@echo off
rem Mematikan autostart: menghapus shortcut dari folder Startup Windows.
powershell -NoProfile -Command "Remove-Item -LiteralPath ([Environment]::GetFolderPath('Startup')+'\Memecoin Simulator.lnk') -ErrorAction SilentlyContinue"
echo Autostart DIMATIKAN. Simulator tidak lagi berjalan otomatis saat laptop dinyalakan.
pause
