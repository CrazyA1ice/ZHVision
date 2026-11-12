"""Guard Windows scraper launcher against missing Chromium after Playwright upgrades."""
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parents[1] / "Scraper-Content" / "run_zhihu_scraper.bat"


def test_playwright_browser_executable_is_checked_before_launch():
    script = LAUNCHER.read_text(encoding="utf-8")
    assert ':CHECK_BROWSER' in script
    assert 'from playwright.sync_api import sync_playwright' in script
    assert 'p.chromium.executable_path' in script
    assert 'exe.is_file()' in script
    assert script.index(':CHECK_BROWSER') < script.index(':RUN_APP')


def test_missing_chromium_is_installed_with_scraper_venv_and_verified():
    script = LAUNCHER.read_text(encoding="utf-8")
    assert '"%VENV_PYTHON%" -m playwright install chromium' in script
    assert script.count('p.chromium.executable_path') >= 2
    assert 'if errorlevel 1 goto BROWSER_INSTALL_FAILED' in script
    assert 'if not errorlevel 1 goto RUN_APP' in script
