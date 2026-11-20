"""Рендер страниц в настоящем браузере (Playwright + Chromium) — для страниц, где ссылки рисуются скриптами.

Нужен только как запасной режим (--browser): капчи и защиту от ботов не обходит. Установка:
    .venv/bin/python -m playwright install chromium
"""
from __future__ import annotations

import os
from urllib.parse import urljoin

_pw = None
_browser = None


class BrowserUnavailable(RuntimeError):
    pass


def _installed_chromium() -> str | None:
    """Ищет уже скачанный Chromium в кэше Playwright (версии библиотеки и браузера могут не совпадать)."""
    import glob
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.expanduser("~/.cache/ms-playwright")
    for pattern in ("chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
                    "chromium-*/chrome-linux64/chrome", "chromium-*/chrome-linux/chrome"):
        found = sorted(glob.glob(os.path.join(root, pattern)))
        if found:
            return found[-1]
    return None


def _system_browsers() -> list[str]:
    """Установленные в системе Edge/Chrome (Windows) — чтобы не вшивать Chromium в программу."""
    cands = []
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            cands += [os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                      os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")]
    cands += ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/microsoft-edge"]
    return [c for c in cands if os.path.exists(c)]


def _launch(proxy: bool):
    global _pw, _browser
    from playwright.sync_api import sync_playwright
    if _pw is None:
        _pw = sync_playwright().start()
    kw = {"headless": True}
    exe = _installed_chromium() or next(iter(_system_browsers()), None)
    if exe:
        kw["executable_path"] = exe
    p = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy and p:
        kw["proxy"] = {"server": p}
    return _pw.chromium.launch(**kw)


def render_links(url: str, timeout_ms: int = 45000, scroll: bool = True) -> list[tuple[str, str]]:
    """Открывает страницу, ждёт скрипты и возвращает [(абсолютная_ссылка, текст)] всех <a href>."""
    try:
        from playwright.sync_api import Error as PwError
    except ImportError as e:
        raise BrowserUnavailable("не установлен playwright (pip install playwright)") from e
    last: Exception | None = None
    for use_proxy in (True, False):  # сначала через системный прокси, затем напрямую
        browser = None
        try:
            browser = _launch(use_proxy)
            page = browser.new_page(user_agent="Mozilla/5.0 (X11; Linux x86_64) Downstream-Agent/0.1")
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PwError:
                pass  # часть сайтов никогда не «затихает» — берём, что успело загрузиться
            if scroll:  # подгрузка ссылок «ленивыми» блоками
                for _ in range(4):
                    page.mouse.wheel(0, 4000)
                    page.wait_for_timeout(400)
            items = page.eval_on_selector_all(
                "a[href]", "els => els.map(a => [a.getAttribute('href'), (a.innerText || a.title || '').trim()])")
            return [(urljoin(page.url, h), t) for h, t in items if h and not h.startswith(("#", "javascript:", "mailto:"))]
        except PwError as e:
            msg = str(e)
            if "Executable doesn't exist" in msg or "playwright install" in msg:
                raise BrowserUnavailable("Chromium не установлен: .venv/bin/python -m playwright install chromium") from e
            if "error while loading shared libraries" in msg or "Host system is missing dependencies" in msg:
                raise BrowserUnavailable("в системе нет библиотек для Chromium (sudo playwright install-deps)") from e
            last = e
        finally:
            if browser:
                browser.close()
    raise BrowserUnavailable(f"страница не открылась: {str(last)[:150]}")
