@echo off
rem Membuat shortcut di folder Startup Windows supaya simulator jalan otomatis saat laptop dinyalakan.
cd /d "%~dp0"
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\Memecoin Simulator.lnk'); $s.TargetPath='%~dp0mulai_otomatis.bat'; $s.WorkingDirectory='%~dp0'; $s.WindowStyle=7; $s.Description='Simulator meme coin (otomatis saat Windows menyala)'; $s.Save()"
echo.
echo Autostart AKTIF. Simulator akan berjalan otomatis setiap kali laptop dinyalakan.
echo Untuk mematikan: klik dua kali hapus_autostart.bat
pause
