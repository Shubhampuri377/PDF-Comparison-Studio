@echo off
REM ---------------------------------------------------------------------
REM  PDF Comparison Studio - build a standalone Windows executable
REM
REM  Produces dist\PDFComparisonStudio.exe, which needs no Python
REM  installation on the target machine.
REM ---------------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo ============================================================
echo   PDF Comparison Studio - build
echo ============================================================
echo.

echo [1/5] Checking PyInstaller...
"%PY%" -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo       not installed - installing now
    "%PY%" -m pip install pyinstaller
    if errorlevel 1 goto :failed
) else (
    echo       ok
)
echo.

echo [2/5] Pre-flight checks...
"%PY%" tools\preflight.py
if errorlevel 1 goto :failed
echo.

echo [3/5] Cleaning previous build...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
echo       done
echo.

echo [4/5] Building (this takes a few minutes)...
"%PY%" -m PyInstaller app.spec --noconfirm --log-level WARN
if errorlevel 1 goto :failed
echo.

echo [5/6] Verifying the executable actually runs...
if not exist "dist\PDFComparisonStudio.exe" (
    echo       [!] Expected dist\PDFComparisonStudio.exe was not produced.
    goto :failed
)
REM A build can link cleanly and still die the first time it spawns a worker.
REM The self test forces that path before the .exe reaches anyone.
"dist\PDFComparisonStudio.exe" --selftest
if errorlevel 1 (
    echo       [!] The built executable failed its self test.
    goto :failed
)
echo.

echo [6/6] Result
for %%F in ("dist\PDFComparisonStudio.exe") do (
    set /a MB=%%~zF/1048576
    call echo       dist\PDFComparisonStudio.exe  ^(%%MB%% MB^)
)
echo.
echo ============================================================
echo   Build complete.
echo.
echo   Ship ONLY the .exe. Never ship tools\signing_seed.txt -
echo   anyone holding it can mint licences for your product.
echo ============================================================
echo.
pause
endlocal
exit /b 0

:failed
echo.
echo ============================================================
echo   BUILD FAILED - see the messages above.
echo ============================================================
echo.
pause
endlocal
exit /b 1
