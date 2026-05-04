@echo off
setlocal

cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
  echo [run.bat] Creando venv local...
  python -m venv venv
  if errorlevel 1 (
    echo [run.bat] python no esta en PATH. Instala Python 3.11+ y reintenta.
    exit /b 1
  )
  venv\Scripts\python.exe -m pip install --upgrade pip
  venv\Scripts\python.exe -m pip install -r requirements.txt
  if errorlevel 1 exit /b %errorlevel%
)

venv\Scripts\python.exe -m PyQt6 >nul 2>nul
if errorlevel 1 (
  echo [run.bat] Instalando PyQt6 en el venv...
  venv\Scripts\python.exe -m pip install -r requirements.txt
)

echo [run.bat] Lanzando CyberShop Desktop Offline...
venv\Scripts\python.exe main.py
exit /b %errorlevel%
