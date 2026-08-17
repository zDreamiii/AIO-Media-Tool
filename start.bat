@echo off
setlocal EnableExtensions

rem Always work from the folder this BAT file is stored in.
pushd "%~dp0" || goto :bad_folder

echo [AIO] Source-Version aus: %CD%

call :find_python
if errorlevel 1 goto :no_python

set "VENV=%CD%\.venv"
set "VENV_PY=%VENV%\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo [AIO] Erstelle lokale Python-Umgebung ...
    %PY_CMD% -m venv "%VENV%"
    if errorlevel 1 goto :failed
    echo [AIO] Installiere Abhaengigkeiten. Das ist nur beim ersten Start noetig ...
    "%VENV_PY%" -m pip install --disable-pip-version-check --upgrade pip
    if errorlevel 1 goto :failed
    "%VENV_PY%" -m pip install --disable-pip-version-check -e ".[transcription,ocr]"
    if errorlevel 1 goto :failed
)

echo [AIO] Starte AIO Media Tool ...
"%VENV_PY%" -m aio_media_tool
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :failed_code
popd
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
echo Fuer normale Nutzer ist die fertige AIO-Media-Tool.exe gedacht.
goto :end_error

:bad_folder
echo [FEHLER] Der Ordner dieser BAT-Datei konnte nicht geoeffnet werden.
goto :end_error_no_pop

:failed_code
echo.
echo [FEHLER] AIO Media Tool wurde mit Fehlercode %RC% beendet.
goto :end_error

:failed
echo.
echo [FEHLER] AIO Media Tool konnte nicht vorbereitet oder gestartet werden.
echo Die konkrete Fehlermeldung steht direkt oberhalb.
goto :end_error

:end_error
popd
:end_error_no_pop
pause
exit /b 1
