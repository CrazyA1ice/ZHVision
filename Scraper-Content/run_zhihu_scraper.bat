@echo off
setlocal
title Zhihu Scraper Launcher
cd /d "%~dp0"

set "VENV_DIR=%~dp0.venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "SCRAPER_SCRIPT=%~dp0zhihu_scraper.py"

if exist "%SCRAPER_SCRIPT%" goto CHECK_VENV
echo [ERROR] zhihu_scraper.py not found in: %CD%
goto FAIL

:CHECK_VENV
if exist "%VENV_PYTHON%" goto CHECK_DEPS
echo ============================================================
echo [1/3] Creating Python virtual environment .venv ...
echo ============================================================
python -m venv "%VENV_DIR%"
if errorlevel 1 (
    echo [ERROR] Failed to create virtual environment. Check Python installation.
    goto FAIL
)

:CHECK_DEPS
"%VENV_PYTHON%" -c "import playwright" >nul 2>nul
if not errorlevel 1 goto CHECK_BROWSER
echo [2/3] Installing playwright and html2text ...
"%VENV_PYTHON%" -m pip install playwright html2text -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
    echo [ERROR] Python dependencies could not be installed.
    goto FAIL
)

:CHECK_BROWSER
rem Playwright may already be importable while its version-specific Chromium binary is missing.
rem Query the executable path from THIS virtualenv; never rely on another Python's "playwright" command.
"%VENV_PYTHON%" -c "from pathlib import Path; from playwright.sync_api import sync_playwright; p=sync_playwright().start(); exe=Path(p.chromium.executable_path); p.stop(); raise SystemExit(0 if exe.is_file() else 1)" >nul 2>nul
if not errorlevel 1 goto RUN_APP

echo ============================================================
echo [3/3] Chromium is missing or outdated for this Playwright version.
echo Automatically downloading the required Chromium browser...
echo ============================================================
"%VENV_PYTHON%" -m playwright install chromium
if errorlevel 1 goto BROWSER_INSTALL_FAILED

rem Check that the browser expected by the installed Playwright actually exists.
"%VENV_PYTHON%" -c "from pathlib import Path; from playwright.sync_api import sync_playwright; p=sync_playwright().start(); exe=Path(p.chromium.executable_path); p.stop(); raise SystemExit(0 if exe.is_file() else 1)" >nul 2>nul
if errorlevel 1 goto BROWSER_INSTALL_FAILED
goto RUN_APP

:BROWSER_INSTALL_FAILED
echo [ERROR] Chromium installation failed or the executable is still missing.
echo [ACTION] Check network connectivity and retry with:
echo "%VENV_PYTHON%" -m playwright install chromium
goto FAIL

:RUN_APP
echo Starting Zhihu Scraper GUI ...
"%VENV_PYTHON%" "%SCRAPER_SCRIPT%"
if errorlevel 1 (
    echo [ERROR] Program exited with an error.
    goto FAIL
)
goto END

:FAIL
pause
exit /b 1

:END
endlocal
exit /b 0
