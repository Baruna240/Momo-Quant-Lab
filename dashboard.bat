@echo off
cd /d "%~dp0"
echo Dashboard: http://localhost:8050  (tutup jendela ini untuk berhenti)
python dashboard.py
pause
