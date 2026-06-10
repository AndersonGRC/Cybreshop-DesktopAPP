@echo off
setlocal

REM ===============================================================
REM  Pipeline completo de build:
REM    1. PyInstaller   (build_exe.bat)        -> dist\CyberShopOffline\
REM    2. Inno Setup    (installer.iss)        -> Output\CyberShopSetup.exe
REM    3. Copia el .exe a static/installers/   (servido por /descargar)
REM
REM  Requisitos:
REM    - venv\ creado (corre run.bat al menos una vez antes)
REM    - Inno Setup 6 instalado en la ruta default de Program Files (x86)
REM ===============================================================

cd /d "%~dp0"

echo.
echo [1/3] Empaquetando con PyInstaller...
call ".\build_exe.bat"
if errorlevel 1 (
  echo ERROR: build_exe.bat fallo.
  exit /b %errorlevel%
)

REM Busca ISCC.exe en las rutas conocidas (default + drive D + drive E)
set "ISCC="
for %%P in (
  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
  "C:\Program Files\Inno Setup 6\ISCC.exe"
  "D:\Inno Setup 6\ISCC.exe"
  "E:\Inno Setup 6\ISCC.exe"
) do if exist %%P set "ISCC=%%~P"

if not defined ISCC (
  echo.
  echo ERROR: No se encontro Inno Setup 6 en ninguna ruta conocida.
  echo Descarga e instala desde https://jrsoftware.org/isdl.php
  echo O edita este .bat y agrega la ruta de tu ISCC.exe al loop de busqueda.
  exit /b 1
)
echo Usando ISCC: %ISCC%

echo.
echo [2/3] Compilando instalador con Inno Setup...
"%ISCC%" /Q installer.iss
if errorlevel 1 (
  echo ERROR: Inno Setup fallo.
  exit /b %errorlevel%
)

if not exist "Output\CyberShopSetup.exe" (
  echo ERROR: Output\CyberShopSetup.exe no se genero.
  exit /b 1
)

echo.
echo [3/3] Copiando a static\installers\ del Flask app...
set "DEST=..\CyberShop\app\static\installers"
if not exist "%DEST%" mkdir "%DEST%"
copy /Y "Output\CyberShopSetup.exe" "%DEST%\CyberShopSetup_base.exe"
if errorlevel 1 (
  echo ERROR: copia fallo.
  exit /b %errorlevel%
)

echo.
echo ===============================================================
echo  Build completado exitosamente.
echo  Instalador:    %CD%\Output\CyberShopSetup.exe
echo  Distribuido:   %CD%\..\CyberShop\app\static\installers\CyberShopSetup_base.exe
echo ===============================================================
echo.
echo Recordatorio: editar version.json y bumpear el campo "latest"
echo si esta version debe activar el auto-update en clientes existentes.
