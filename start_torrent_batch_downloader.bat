@echo off
setlocal

cd /d "%~dp0"

set "APP_EXE=%~dp0dist\TorrentBatchDownloader.exe"
set "SCRIPT=%~dp0torrent_batch_gui.py"

if exist "%APP_EXE%" (
    start "" "%APP_EXE%"
    exit /b 0
)

set "PYTHON_CMD="

if exist "%~dp0.venv\Scripts\python.exe" (
    set "PYTHON_CMD=%~dp0.venv\Scripts\python.exe"
    goto run_python
)

if exist "%~dp0venv\Scripts\python.exe" (
    set "PYTHON_CMD=%~dp0venv\Scripts\python.exe"
    goto run_python
)

where python >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_CMD=python"
    goto run_python
)

where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%SCRIPT%"
    set "EXIT_CODE=%ERRORLEVEL%"
    goto finish
)

echo Python was not found.
echo.
echo Install Python 3, create a .venv, or build dist\TorrentBatchDownloader.exe first.
echo See BUILD_WINDOWS.md for build instructions.
set "EXIT_CODE=1"
goto finish

:run_python
"%PYTHON_CMD%" "%SCRIPT%"
set "EXIT_CODE=%ERRORLEVEL%"

:finish
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
