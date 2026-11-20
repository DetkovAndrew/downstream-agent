"""Извлечение утверждений о downstream из распознанных страниц PDF."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from downstream_agent import llm
from downstream_agent.collector import DATA

SYSTEM = (
    "Extract explicit factual statements about oil-company downstream from report pages. Include completed events, "
    "current operations, plans, targets, decisions, construction, commissioning, capacity changes, outages, "
    "refinery utilization/throughput, retail networks, petrochemicals, lubricants and product logistics. "
    "Exclude upstream and generic financial results unless they explicitly describe downstream. "
    "Each item: company (as provided), period, segment, category (plan|target|decision|construction|launch|operation|change|incident|other), "
    "status (planned|in_progress|completed|current|unknown), statement (Russian, faithful translation or concise paraphrase), "
    "quote (exact text from page), page (integer). Never infer beyond the quote. Reply JSON {\"items\": [...]}"
)


def extract(company_names: dict[str, str] | None = None, companies: list[str] | None = None,
            log=print, delay: float = 1.0) -> list[dict]:
    names = company_names or {}
    out: list[dict] = []
    for f in sorted((DATA / "text").glob("*/*.jsonl")):
        cid = f.parent.name
        if companies and cid not in companies:
            continue
        pages = [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
        pages = [p for p in pages if p.get("text") or p.get("layout_text")]
        for start in range(0, len(pages), 3):
            batch = pages[start:start + 3]
            body = "\n\n".join(f"PAGE {p['page']}\n{p.get('layout_text') or p['text']}" for p in batch)
            log(f"{names.get(cid,cid)} · {f.stem}: страницы {batch[0]['page']}–{batch[-1]['page']}")
            try:
                result = llm.ask_json(SYSTEM, f"Company: {names.get(cid,cid)}\n\n{body}", timeout=300)
            except llm.LlmUnavailable as e:
                log(f"! Ошибка модели: {str(e)[:160]}")
                continue
            page_text = {int(p["page"]): (p.get("layout_text") or p.get("text", "")) for p in batch}
            for item in result.get("items", []):
                try:
                    page = int(item["page"])
                except (KeyError, TypeError, ValueError):
                    continue
                quote = str(item.get("quote", "")).strip()
                source = page_text.get(page, "")
                if page not in page_text or len(quote) < 12 or re.sub(r"\s+", " ", quote).casefold() not in re.sub(r"\s+", " ", source).casefold():
                    continue
                out.append({"company": names.get(cid,cid), "file": f.stem, "page": page,
                            "period": str(item.get("period", "")), "segment": str(item.get("segment", "")),
                            "category": str(item.get("category", "other")), "status": str(item.get("status", "unknown")),
                            "statement": str(item.get("statement", "")).strip(), "quote": quote})
            time.sleep(delay)
    return out


def build_excel(rows: list[dict], out: Path) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook(); ws = wb.active; ws.title = "Downstream утверждения"
    ws.append(["Компания", "Документ", "Стр.", "Период", "Сегмент", "Категория", "Статус", "Утверждение", "Цитата"])
    for x in rows:
        ws.append([x[k] for k in ("company", "file", "page", "period", "segment", "category", "status", "statement", "quote")])
    for c in ws[1]: c.font = Font(bold=True); c.fill = PatternFill("solid", fgColor="DDEBF7")
    for col, width in zip("ABCDEFGHI", (24, 42, 9, 16, 22, 18, 18, 80, 90)): ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"; ws.auto_filter.ref = ws.dimensions
    out.parent.mkdir(parents=True, exist_ok=True); ws.parent.save(out)
    return out
