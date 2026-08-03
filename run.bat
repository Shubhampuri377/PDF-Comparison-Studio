@echo off
REM ---------------------------------------------------------------------
REM  PDF Comparison Studio - launcher
REM  Double-click this file, or run it from a terminal.
REM ---------------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [!] No virtual environment found at .venv
    echo     Falling back to the system Python.
    echo.
    set "PY=python"
)

REM pythonw.exe launches without a console window; fall back if it is absent.
set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if exist "%PYW%" if "%1"=="" (
    start "" "%PYW%" "%~dp0app.py"
    exit /b 0
)

"%PY%" "%~dp0app.py" %*
if errorlevel 1 (
    echo.
    echo [!] The application exited with an error.
    echo     If it mentions a missing module, install the dependencies:
    echo         .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
)
endlocal
