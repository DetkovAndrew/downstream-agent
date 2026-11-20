"""Распознавание PDF: текстовый слой + таблицы (PyMuPDF), OCR (Tesseract) для сканов.

Результат: data/text/<company>/<файл>.jsonl, по строке на страницу:
{"page": n, "source": "text"|"ocr", "text": "...", "tables": [[[ячейки]]]}
"""
from __future__ import annotations

import json
import time
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pymupdf as fitz  # PyMuPDF

from downstream_agent.collector import DATA
from downstream_agent.paths import resource_dir
from downstream_agent.collector import ROOT

MIN_CHARS = 40  # меньше — считаем страницу сканом
LANG_BY_SITE = {"ru": ["rus", "eng"], "zh": ["chi_sim", "eng"], "en": ["eng"]}
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # без мигающего окна консоли в .exe


def _tesseract_cmd() -> str | None:
    """Встроенный Tesseract (в .exe), затем из PATH, затем обычная установка в Windows."""
    cands = [os.environ.get("TESSERACT_CMD") or ""]
    for base in (resource_dir() / "tesseract", ROOT / "tesseract_win"):
        cands.append(str(base / ("tesseract.exe" if os.name == "nt" else "tesseract")))
    cands += [shutil.which("tesseract") or "", r"C:\Program Files\Tesseract-OCR\tesseract.exe"]
    return next((c for c in cands if c and os.path.exists(c)), None)


def _tessdata(cmd: str) -> str | None:
    """Каталог моделей: TESSDATA_PREFIX (например, свои модели без прав администратора), затем рядом с tesseract."""
    for d in (Path(os.environ.get("TESSDATA_PREFIX", "/nonexistent")), Path(cmd).parent / "tessdata"):
        if (d / "eng.traineddata").exists():
            return str(d)
    return None


def _tesseract(codes: list[str] | None = None) -> tuple[str, str, str | None] | None:
    """(команда, строка языков, каталог моделей) для нужных языков; None — OCR недоступен."""
    cmd = _tesseract_cmd()
    if not cmd:
        return None
    td = _tessdata(cmd)
    args = [cmd, "--list-langs"] + (["--tessdata-dir", td] if td else [])
    try:
        out = subprocess.run(args, capture_output=True, text=True, creationflags=_NO_WINDOW, timeout=30).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return None
    langs = "+".join(l for l in (codes or ["eng"]) if l in out) or ("eng" if "eng" in out else "")
    return (cmd, langs, td) if langs else None


def _ocr(page: fitz.Page, tess: tuple[str, str, str | None]) -> str:
    cmd, langs, td = tess
    with tempfile.TemporaryDirectory() as d:
        img = Path(d) / "p.png"
        page.get_pixmap(dpi=250).save(img)
        args = [cmd, str(img), "stdout", "-l", langs] + (["--tessdata-dir", td] if td else [])
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           creationflags=_NO_WINDOW, timeout=240)
        return r.stdout


def _tables_isolated(pdf: Path, pages: list[int], log=print, page_timeout: float = 90.0) -> dict[int, list]:
    """Таблицы страниц в отдельном процессе. Упавшая или зависшая страница пропускается (без таблиц), остальные продолжаются."""
    import multiprocessing as mp
    import queue as qmod

    from downstream_agent._tablesworker import table_worker
    ctx = mp.get_context("spawn")
    result: dict[int, list] = {}
    todo = list(pages)
    skipped = []
    while todo:
        q = ctx.Queue()
        proc = ctx.Process(target=table_worker, args=(str(pdf), todo, q), daemon=True)
        proc.start()
        current, finished, deadline = None, False, time.time() + page_timeout
        while True:
            try:
                msg = q.get(timeout=1.0)
            except qmod.Empty:
                if not proc.is_alive() or time.time() > deadline:
                    break  # процесс упал или завис на текущей странице
                continue
            deadline = time.time() + page_timeout
            if msg[0] == "start":
                current = msg[1]
            elif msg[0] == "done":
                result[msg[1]] = msg[2]
                current = None
            else:
                finished = True
                break
        if proc.is_alive():
            proc.terminate()
        proc.join(timeout=5)
        if finished:
            break
        bad = current if current is not None else todo[0]
        skipped.append(bad)
        result[bad] = []
        todo = [n for n in todo if n not in result]
    if skipped:
        log(f"   ⚠ {pdf.name}: таблицы на стр. {', '.join(map(str, skipped))} пропущены (сбой PyMuPDF), текст страниц сохранён")
    return result


def recognize_pdf(pdf: Path, out: Path, tables: bool = True, ocr: bool = True, log=print, site_langs: list[str] | None = None) -> int:
    codes = [c for l in (site_langs or ['en']) for c in LANG_BY_SITE.get(l, [])] or ['eng']
    tess = _tesseract(list(dict.fromkeys(codes))) if ocr else None
    doc = fitz.open(pdf)
    out.parent.mkdir(parents=True, exist_ok=True)
    pages: list[dict] = []
    n_ocr = 0
    for i, page in enumerate(doc, 1):
        text, source = page.get_text().strip(), "text"
        if len(text) < MIN_CHARS and tess:
            text, source = _ocr(page, tess).strip(), "ocr"
            n_ocr += 1
        pages.append({"page": i, "source": source, "text": text, "tables": [],
                      "layout_text": page.get_text(sort=True).strip() if source == "text" else text})
    if tables and not os.environ.get("AGENT_NO_TABLES"):
        found = _tables_isolated(pdf, [p["page"] for p in pages if p["source"] == "text"], log)
        for p in pages:
            p["tables"] = found.get(p["page"], [])
    tmp = out.with_suffix(".jsonl.part")
    with tmp.open("w", encoding="utf-8") as f:
        for p in pages:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    tmp.replace(out)  # файл появляется только целиком: прерванный запуск не оставит обрезок
    scans = sum(1 for p in pages if len(p["text"]) < MIN_CHARS and p["source"] == "text")
    note = f", OCR {n_ocr} ({tess[1]})" if tess else (f", ⚠ {sum(1 for p in doc if len(p.get_text().strip()) < MIN_CHARS)} стр. без текстового слоя, OCR недоступен" if scans or not tess else "")
    log(f"   {pdf.name}: {len(doc)} стр.{note}")
    return len(doc)


def _site_langs() -> dict[str, list[str]]:
    from downstream_agent.collector import load_config
    try:
        return {c["id"]: c.get("langs", ["en"]) for c in load_config(ROOT / "companies.toml")["companies"]}
    except Exception:  # noqa: BLE001
        return {}


def recognize_all(only: list[str] | None = None, force=False, log=print, **kw) -> int:
    done = 0
    langs = _site_langs()
    for pdf in sorted((DATA / "raw").glob("*/reports/*.pdf")):
        company = pdf.parent.parent.name
        if only and company not in only:
            continue
        out = DATA / "text" / company / (pdf.stem + ".jsonl")
        if out.exists() and not force:
            with out.open(encoding="utf-8") as cached:
                if "layout_text" in json.loads(cached.readline()):
                    continue
        try:
            recognize_pdf(pdf, out, log=log, site_langs=langs.get(company), **kw)
            done += 1
        except Exception as e:
            log(f"   ! {pdf.name}: {e}")
            out.with_suffix(".jsonl.part").unlink(missing_ok=True)
    return done
