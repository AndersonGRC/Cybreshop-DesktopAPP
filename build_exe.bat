@echo off
setlocal

cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
  echo No se encontro venv\Scripts\python.exe. Corre run.bat primero para crearlo.
  exit /b 1
)

venv\Scripts\python.exe -m pip install pyinstaller >nul
venv\Scripts\python.exe -m PyInstaller --noconfirm --clean CyberShopOffline.spec
if errorlevel 1 exit /b %errorlevel%

echo.
echo EXE generado en: %CD%\dist\CyberShopOffline\CyberShopOffline.exe
