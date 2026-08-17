@echo off
setlocal EnableExtensions

rem Always work from the folder this BAT file is stored in.
pushd "%~dp0" || goto :bad_folder

echo.
echo ==============================================
echo   AIO Media Tool - Windows EXE bauen
echo ==============================================
echo Projektordner: %CD%
echo.

call :find_python
if errorlevel 1 goto :no_python

echo [AIO] Verwende: %PY_CMD%

set "BUILD_VENV=%CD%\.build-venv"
set "BUILD_PY=%BUILD_VENV%\Scripts\python.exe"

if not exist "%BUILD_PY%" (
    echo [AIO] Erstelle lokale Build-Umgebung ...
    %PY_CMD% -m venv "%BUILD_VENV%"
    if errorlevel 1 goto :failed
)

echo [AIO] Installiere/aktualisiere Build-Abhaengigkeiten ...
"%BUILD_PY%" -m pip install --disable-pip-version-check --upgrade pip
if errorlevel 1 goto :failed
"%BUILD_PY%" -m pip install --disable-pip-version-check -e ".[dev,transcription,ocr]"
if errorlevel 1 goto :failed

if /I "%~1"=="--test" (
    echo.
    echo [AIO] Fuehre Tests aus ...
    "%BUILD_PY%" -m pytest
    if errorlevel 1 goto :failed
) else (
    echo.
    echo [AIO] Lokaler Schnell-Build: Tests werden uebersprungen.
    echo [AIO] Fuer Build + Tests: build_windows.bat --test
)

echo.
echo [AIO] Erzeuge AIO-Media-Tool.exe ...
"%BUILD_PY%" scripts\build.py
if errorlevel 1 goto :failed

if not exist "%CD%\dist\AIO-Media-Tool.exe" goto :missing_exe

echo.
echo ==============================================
echo   FERTIG
echo ==============================================
echo Die fertige Datei liegt hier:
echo %CD%\dist\AIO-Media-Tool.exe
echo.
explorer /select,"%CD%\dist\AIO-Media-Tool.exe"
popd
pause
exit /b 0

:find_python
set "PY_CMD="
where py >nul 2>&1
if not errorlevel 1 (
    py -3.12 -c "import sys" >nul 2>&1 && set "PY_CMD=py -3.12"
    if not defined PY_CMD py -3.13 -c "import sys" >nul 2>&1 && set "PY_CMD=py -3.13"
    if not defined PY_CMD py -3.11 -c "import sys" >nul 2>&1 && set "PY_CMD=py -3.11"
)
if not defined PY_CMD (
    where python >nul 2>&1
    if not errorlevel 1 (
        python -c "import sys; raise SystemExit(0 if (3,11) <= sys.version_info[:2] < (3,14) else 1)" >nul 2>&1 && set "PY_CMD=python"
    )
)
if not defined PY_CMD exit /b 1
exit /b 0

:no_python
echo [FEHLER] Python 3.11, 3.12 oder 3.13 wurde nicht gefunden.
echo Installiere Python von python.org und aktiviere beim Setup "Add python.exe to PATH".
goto :end_error

:bad_folder
echo [FEHLER] Der Ordner dieser BAT-Datei konnte nicht geoeffnet werden.
goto :end_error_no_pop

:missing_exe
echo [FEHLER] Der Build lief durch, aber dist\AIO-Media-Tool.exe fehlt.
goto :end_error

:failed
echo.
echo [FEHLER] Der Build ist fehlgeschlagen.
echo Die konkrete Fehlermeldung steht direkt oberhalb.
goto :end_error

:end_error
popd
:end_error_no_pop
pause
exit /b 1
