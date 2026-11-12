@echo off
title Zhihu Scraper Launcher
cd /d "%~dp0"

set "VENV_DIR=%~dp0.venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "SCRAPER_SCRIPT=%~dp0zhihu_scraper.py"

if exist "%SCRAPER_SCRIPT%" goto CHECK_VENV
echo [ERROR] zhihu_scraper.py not found in: %CD%
pause
exit /b 1

:CHECK_VENV
if exist "%VENV_PYTHON%" goto CHECK_DEPS
echo ============================================================
echo [1/3] Creating Python virtual environment .venv ...
echo ============================================================
python -m venv "%VENV_DIR%"
if not errorlevel 1 goto INSTALL_DEPS
echo [ERROR] Failed to create virtual environment. Check Python installation.
pause
exit /b 1

:INSTALL_DEPS
echo [2/3] Installing playwright and html2text ...
"%VENV_PYTHON%" -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
"%VENV_PYTHON%" -m pip install playwright html2text -i https://pypi.tuna.tsinghua.edu.cn/simple
echo [3/3] Installing Playwright Chromium browser ...
"%VENV_PYTHON%" -m playwright install chromium
goto RUN_APP

:CHECK_DEPS
"%VENV_PYTHON%" -c "import playwright" >nul 2>nul
if not errorlevel 1 goto RUN_APP
echo Installing missing dependencies in .venv ...
"%VENV_PYTHON%" -m pip install playwright html2text -i https://pypi.tuna.tsinghua.edu.cn/simple
"%VENV_PYTHON%" -m playwright install chromium

:RUN_APP
echo Starting Zhihu Scraper GUI ...
"%VENV_PYTHON%" "%SCRAPER_SCRIPT%"
if not errorlevel 1 goto END
echo.
echo [ERROR] Program exited with an error.
pause

:END