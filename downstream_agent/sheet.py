"""Сводная таблица Excel из распознанных PDF: Gemini вытягивает показатели, пустое остаётся пустым.

Конвейер: data/text/<company>/*.jsonl -> (Gemini, батчи страниц) -> data/extracted/<company>/<файл>.json
          -> Excel: «Сводная» (компания × период × сегмент, колонки = показатели), «Факты» (все значения
          с цитатой и страницей), «Конфликты», «Покрытие».
Ничего не считается и не додумывается: факт принимается, только если цитата дословно есть на странице
и число из цитаты совпадает с value_text.
"""
from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from pathlib import Path

from downstream_agent import llm
from downstream_agent.collector import DATA, ROOT

EXTRACTION_VERSION = 3

# (ключ, название, единица по умолчанию, группа, слова-подсказки для предотбора страниц)
METRICS: list[tuple[str, str, str, str, list[str]]] = [
    ("revenue", "Выручка", "млн", "Финансы", ["revenue", "sales", "выручка", "营业收入", "营业额"]),
    ("net_income", "Чистая прибыль", "млн", "Финансы", ["net income", "net profit", "чистая прибыль", "净利润"]),
    ("shareholder_income", "Прибыль, относящаяся к акционерам", "млн", "Финансы", ["attributable to", "shareholders"]),
    ("dividend_per_share", "Дивиденд на акцию", "", "Финансы", ["dividend per share"]),
    ("ebitda", "EBITDA", "млн", "Финансы", ["ebitda"]),
    ("adj_ebitda", "Скорректированная EBITDA", "млн", "Финансы", ["adjusted ebitda", "скорректированная ebitda"]),
    ("adj_net_income", "Скорректированная чистая прибыль", "млн", "Финансы", ["adjusted net income", "скорректированная чистая"]),
    ("ebit", "EBIT / операционная прибыль", "млн", "Финансы", ["ebit", "operating profit", "operating income", "прибыль от продаж", "营业利润"]),
    ("operating_cf", "Операционный денежный поток", "млн", "Финансы", ["operating cash", "cash flow from operat", "операционн", "经营活动"]),
    ("capex", "CAPEX", "млн", "Финансы", ["capex", "capital expenditure", "капитальные", "资本开支", "资本性支出"]),
    ("total_assets", "Активы всего", "млн", "Баланс", ["total assets", "итого активы", "总资产", "资产总计"]),
    ("total_equity", "Капитал всего", "млн", "Баланс", ["total equity", "итого капитал", "所有者权益", "股东权益"]),
    ("total_debt", "Долг всего", "млн", "Баланс", ["total debt", "borrowings", "кредиты и займы", "总债务", "借款"]),
    ("net_debt", "Чистый долг", "млн", "Баланс", ["net debt", "чистый долг", "净债务"]),
    ("capital_employed", "Капитал (capital employed), средний", "млн", "Эффективность", ["capital employed", "используемый капитал", "已动用资本"]),
    ("roace", "ROACE", "%", "Эффективность", ["roace", "return on average capital", "рентабельность"]),
    ("roce", "ROCE", "%", "Эффективность", ["roce", "return on capital employed", "资本回报率"]),
    ("roe", "ROE", "%", "Эффективность", ["roe", "return on equity", "рентабельность собственного", "净资产收益率"]),
    ("dividends", "Дивиденды", "млн", "Финансы", ["dividend", "дивиденд", "股息"]),
    ("refinery_throughput", "Объём переработки", "", "Downstream", ["throughput", "refinery", "переработк", "加工量", "原油加工"]),
    ("refinery_utilization", "Загрузка НПЗ", "%", "Downstream", ["utilization", "utilisation", "загрузка", "开工率"]),
    ("refining_margin", "Маржа переработки", "", "Downstream", ["refining margin", "refining indicator", "маржа", "炼油毛利", "裂解价差"]),
    ("light_products_yield", "Выход светлых нефтепродуктов", "%", "Downstream", ["light product", "светлых", "轻油收率"]),
    ("nelson_index", "Индекс Нельсона", "", "Downstream", ["nelson", "нельсон"]),
    ("petchem_output", "Выпуск нефтехимии", "", "Downstream", ["petrochemical", "нефтехим", "石化产品", "乙烯"]),
    ("retail_stations", "АЗС/розничные точки", "шт.", "Downstream", ["service stations", "retail sites", "азс", "加油站"]),
    ("downstream_ebitda", "EBITDA Downstream", "млн", "Downstream", ["downstream", "refining and marketing", "переработка и сбыт", "炼油"]),
    ("downstream_capex", "CAPEX Downstream", "млн", "Downstream", ["downstream", "refining and marketing", "переработка и сбыт", "炼油"]),
    ("downstream_capital_employed", "Capital employed Downstream", "млн", "Downstream", ["downstream", "refining and marketing", "переработка и сбыт"]),
    ("downstream_revenue", "Выручка Downstream", "млн", "Downstream", ["downstream", "refining and marketing", "переработка и сбыт", "炼油"]),
    ("downstream_net_income", "Прибыль Downstream", "млн", "Downstream", ["downstream", "refining and marketing", "переработка и сбыт", "炼油"]),
]
KEYS = [m[0] for m in METRICS]
HINTS = sorted({h for m in METRICS for h in m[4]})

SYSTEM = (
    "You extract oil-company KPIs from pages of an annual/interim report or press release. "
    "Extract ONLY values explicitly written on the pages; never compute, convert, estimate or guess. "
    "Ignore upstream entirely (exploration, production volumes, reserves). "
    "If a metric is calculated on a rolling 12-month basis, set period to 'LTM <Mon YYYY>' (e.g. LTM Jun 2026). "
    "Revenue means total revenues / sales (e.g. 'Revenues and other income related to sales'). "
    "Match the metric EXACTLY: if the page's figure is a different measure (e.g. adjusted net income is NOT EBITDA, "
    "capital investment is NOT downstream capex, production is NOT throughput), skip it - never map to a similar key. "
    "Report each currency/column separately (e.g. SAR and USD tables are two facts). "
    "Metrics (use these exact keys): " + "; ".join(f"{k} = {n}" for k, n, *_ in METRICS) + ". "
    "For every value return an object with: metric (key), value_text (the number exactly as written, e.g. '19.8' or '2,028,003'), "
    "value (the same number as a plain JSON number), unit (as written: '%', 'mb/d', '$/bbl', 'млн т', 'million tonnes'...), "
    "scale (million/billion/thousand/none, as stated in the table header or text), currency (USD/SAR/RUB/CNY/EUR/... or ''), "
    "period (normalised: FY2025, H1 2026, Q1 2026, 9M 2025, LTM Jun 2026), segment (group, downstream, upstream, "
    "refining, marketing, chemicals, other), definition (short note if the report says the metric uses a specific/revised "
    "definition, else ''), kind (actual | target | forecast), page (page number), "
    "quote (a SHORT exact copy of the text/table row on the page containing the number). "
    "Include prior-year comparatives as separate objects with their own period. "
    "Read EVERY relevant table row, including segment tables and operational KPIs. Preserve column headers and units. "
    "Keep segments separate: Marketing uses marketing; Chemicals and Products uses downstream; Integrated Gas uses other. "
    "Use refining for refinery operational KPIs. Never assign a segment's financial figures to group. "
    "Ignore reconciliation adjustments (identified items, impacts, NCI adjustments) as standalone earnings. "
    "Never use revenue for income/profit. Use shareholder_income for income attributable to shareholders; "
    "net_income only for income for the period including non-controlling interests. "
    "Use dividend_per_share for dividends per share, never dividends. "
    "Adjusted Earnings may be stored as adj_net_income with its original definition. "
    "Chemicals sales volumes are sales, not production; never use petchem_output for sales. "
    "Quote must contain the metric's actual row label and its number. "
    "Skip targets/forecasts unless nothing else exists (mark kind accordingly). "
    "Reply JSON: {\"facts\": [...]}. If the pages contain none of the metrics: {\"facts\": []}."
)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def _page_text(p: dict) -> str:
    if p.get("layout_text"):
        return p["layout_text"]
    tbl = "\n".join(" | ".join(str(c or "") for c in row) for t in p.get("tables", []) for row in t)
    return p["text"] + ("\n" + tbl if tbl else "")


def _relevant(p: dict) -> bool:
    t = _page_text(p).lower()
    return bool(re.search(r"\d", t)) and any(h in t for h in HINTS)


def _batches(pages: list[dict], size: int, max_chars: int) -> list[list[dict]]:
    out, cur, chars = [], [], 0
    for p in pages:
        n = min(len(_page_text(p)), max_chars)
        if cur and (len(cur) >= size or chars + n > size * max_chars):
            out.append(cur)
            cur, chars = [], 0
        cur.append(p)
        chars += n
    return out + ([cur] if cur else [])


_NUM_RE = re.compile(r"\d[\d\s,.]*\d|\d")
# Layout text separates table columns with whitespace; never merge adjacent cells.
_CELL_NUMBER_RE = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")
_WORD_RE = re.compile(r"[^\W\d_]{3,}")


def _num_key(text: str) -> str:
    """Число без разделителей тысяч и скобок: '(3,086)' -> '3086', '19.8%' -> '19.8', '1,467mb/d' -> '1467'."""
    m = _NUM_RE.search(str(text))
    return re.sub(r"[\s,]", "", m.group(0)).rstrip(".") if m else ""


def _loose_page(vt: str, quote: str, by_page: dict[int, str], hint: int) -> int | None:
    """Мягкая проверка: число есть на странице, а рядом (±300 символов) стоят слова из подписи показателя."""
    key = _num_key(vt)
    if not key or (len(key.replace(".", "")) < 2 and "." not in key):
        return None  # одиночные цифры слишком неоднозначны
    words = set(_WORD_RE.findall(str(quote).lower())) - {"million", "billion", "thousand", "usd", "eur", "rub", "sar", "cny"}
    need = max(1, int(0.5 * len(words)))
    for page in sorted(by_page, key=lambda n: n != hint):  # сначала указанная моделью страница
        text = by_page[page]
        for m in _CELL_NUMBER_RE.finditer(text):
            if re.sub(r"[\s,]", "", m.group(0)).rstrip(".") != key:
                continue
            window = text[max(0, m.start() - 300): m.end() + 300].lower()
            if not words or len(words & set(_WORD_RE.findall(window))) >= need:
                return page
    return None


def _accept(f: dict, by_page: dict[int, str], loose: bool = True) -> dict | None:
    """Проверка факта. Точная: цитата дословно на странице и число в цитате. Мягкая (loose): число есть на странице
    рядом со словами из подписи показателя (цитаты, склеенные из строки и колонки таблицы, и числа в скобках)."""
    try:
        page = int(f["page"])
        metric, quote, vt = f["metric"], str(f["quote"]), str(f["value_text"])
    except (KeyError, TypeError, ValueError):
        return None
    if metric not in KEYS:
        return None
    try:
        value = float(f["value"])
    except (KeyError, TypeError, ValueError):
        return None
    # A matching number alone does not establish which measure it belongs to.
    label = _norm(quote)
    if metric == "revenue" and not re.search(r"revenue|sales|выручк|营业收入|营业额", label):
        return None
    if metric == "net_income" and re.search(r"attributable.*shareholder", label):
        metric = "shareholder_income"
    if metric == "dividends" and re.search(r"per share|на акци", label):
        metric = "dividend_per_share"
    try:
        written = float(_num_key(vt))
        if str(vt).strip().startswith(("-", "(")):
            written = -written
        if abs(written - value) > max(1e-8, abs(value) * 1e-9):
            return None
    except ValueError:
        return None
    verified = "точная"
    exact = len(quote) >= 6 and _norm(vt) in _norm(quote)
    if exact and (page in by_page and _norm(quote) in _norm(by_page[page])):
        pass
    elif exact and (found := [n for n, t in by_page.items() if _norm(quote) in _norm(t)]):
        page = found[0]  # модель иногда путает номер страницы
    elif loose and (lp := _loose_page(vt, quote, by_page, page)) is not None:
        page, verified = lp, "мягкая"
    else:
        return None
    return {"metric": metric, "value": value, "value_text": vt, "unit": str(f.get("unit", "")),
            "scale": str(f.get("scale", "")), "currency": str(f.get("currency", "")),
            "period": str(f.get("period", "")).strip(), "segment": str(f.get("segment") or "group").lower(),
            "definition": str(f.get("definition", "")), "kind": str(f.get("kind", "actual")).lower(),
            "page": page, "quote": quote, "verified": verified}


def extract_document(jsonl: Path, company: str, out: Path, pages_per_call: int = 3, max_chars: int = 50000,
                     prefilter: bool = True, delay: float = 3.0, max_pages: int | None = None, log=print,
                     loose: bool = True) -> list[dict]:
    pages = [json.loads(l) for l in open(jsonl, encoding="utf-8")]
    document_text = " ".join(p["text"] for p in pages).lower()
    rolling_roace = "roace" in document_text and "current and previous three quarters" in document_text
    if prefilter:
        pages = [p for p in pages if _relevant(p)]
    if max_pages:
        pages = pages[:max_pages]
    by_page = {p["page"]: _page_text(p) for p in pages}
    ocr_pages = {p["page"] for p in pages if p.get("source") == "ocr"}
    facts, rejected, failed = [], 0, 0
    for i, batch in enumerate(_batches(pages, pages_per_call, max_chars), 1):
        # Never silently discard the bottom of a page or its table rows.
        body = "\n\n".join(f"=== PAGE {p['page']} ===\n{_page_text(p)}" for p in batch)
        log(f"   батч {i}: страницы {', '.join(str(p['page']) for p in batch)}")
        try:
            res = llm.ask_json(SYSTEM, f"Company: {company}\nFile: {jsonl.stem}\n\n{body}", timeout=600)
        except llm.LlmUnavailable as e:
            log(f"   ! батч {i}: {str(e)[:160]}")
            failed += 1
            continue
        for f in res.get("facts", []):
            a = _accept(f, {p["page"]: by_page[p["page"]] for p in batch}, loose)
            if a and a["metric"] == "roace" and rolling_roace:
                match = re.fullmatch(r"Q([1-4]) (20\d\d)", a["period"])
                if match:
                    month = ("Mar", "Jun", "Sep", "Dec")[int(match[1]) - 1]
                    a["period"] = f"LTM {month} {match[2]}"
                    a["definition"] = "Current and previous three quarters; " + a["definition"]
            if a and a["page"] in ocr_pages:
                a["verified"] += " · OCR"  # цифры распознаны с картинки — сверьте по странице
            if a:
                facts.append({**a, "company": company, "file": jsonl.stem})
            else:
                rejected += 1
        time.sleep(delay)
    # Operational numbers in prose are easy to miss in financial table batches.
    for key in ("refinery_utilization", "refinery_throughput", "capital_employed"):
        if any(f["metric"] == key for f in facts):
            continue
        hints = next(m[4] for m in METRICS if m[0] == key)
        candidates = [p for p in pages if any(h in _page_text(p).lower() for h in hints)]
        for batch in _batches(candidates, pages_per_call, max_chars):
            log(f"   проверка пропусков: {key}, страницы {', '.join(str(p['page']) for p in batch)}")
            try:
                body = "\n\n".join(f"=== PAGE {p['page']} ===\n{_page_text(p)}" for p in batch)
                res = llm.ask_json(SYSTEM, f"Company: {company}. Extract ONLY {key}. Include numeric values in narrative sentences and comparatives.\n{body}", timeout=600)
            except llm.LlmUnavailable as e:
                log(f"   ! проверка пропусков: {str(e)[:160]}")
                failed += 1
                continue
            for f in res.get("facts", []):
                if f.get("metric") != key:
                    continue
                a = _accept(f, {p["page"]: by_page[p["page"]] for p in batch}, loose)
                if a:
                    facts.append({**a, "company": company, "file": jsonl.stem})
    if failed:  # неполный результат в кэш не пишем, иначе повторный запуск пропустит файл
        log(f"   {jsonl.stem[:50]}: {failed} батч(ей) не обработано — не сохраняю, повторите запуск")
        return facts
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"version": EXTRACTION_VERSION, "company": company, "file": jsonl.stem, "pages_sent": len(pages),
                               "rejected": rejected, "facts": facts}, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"   {jsonl.stem[:50]}: страниц {len(pages)}, фактов {len(facts)}, отклонено {rejected}")
    return facts


def extract_all(companies: list[str] | None = None, files: list[str] | None = None, force: bool = False,
                log=print, **kw) -> int:
    """Заполняет data/extracted/ из data/text/. companies — id компаний (None = все), files — подстроки имён."""
    n = 0
    for cid in companies or []:
        has_pdf = any((DATA / "raw" / cid / "reports").glob("*.pdf"))
        has_text = any((DATA / "text" / cid).glob("*.jsonl")) if (DATA / "text" / cid).exists() else False
        if not has_text:
            log(f"! {cid}: " + ("PDF есть, но не распознаны — сначала: python -m downstream_agent recognize --company " + cid if has_pdf
                              else "нет документов — сначала: python -m downstream_agent discover --company " + cid))
    for jsonl in sorted((DATA / "text").glob("*/*.jsonl")):
        company = jsonl.parent.name
        if companies and company not in companies:
            continue
        if files and not any(x.lower() in jsonl.stem.lower() for x in files):
            continue
        out = DATA / "extracted" / company / (jsonl.stem + ".json")
        if out.exists() and not force and json.loads(out.read_text(encoding="utf-8")).get("version") == EXTRACTION_VERSION:
            continue
        log(f"== {company}")
        n += len(extract_document(jsonl, company, out, log=log, **kw))
    return n


# ---------------------------------------------------------------- Excel
TYPES = ["Все", "Год", "Квартал", "Полугодие", "9 мес.", "LTM"]
_MON = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def period_info(p: str) -> tuple[float, str]:
    """(ключ сортировки, тип) по нормализованной метке: FY2025, Q1 2026, H1 2026, 9M 2025, LTM Jun 2026."""
    y = re.search(r"(20\d\d)", p)
    year = int(y.group(1)) if y else 9999
    t = p.lower()
    if "ltm" in t or "rolling" in t:
        m = next((n for k, n in _MON.items() if k in t), 12)
        return year * 1000 + m * 10 + 3, "LTM"
    if re.search(r"9\s?m|nine", t):
        return year * 1000 + 91, "9 мес."
    q = re.search(r"q\s?([1-4])|([1-4])\s?q", t)
    if q:
        return year * 1000 + int(q.group(1) or q.group(2)) * 30, "Квартал"
    h = re.search(r"h\s?([12])|([12])\s?h|half", t)
    if h:
        n = int(h.group(1) or h.group(2) or 1)
        return year * 1000 + n * 60 + 1, "Полугодие"
    if "fy" in t or "year" in t or re.fullmatch(r"\s*20\d\d\s*", t):
        return year * 1000 + 122, "Год"
    return year * 1000 + 999, "Прочее"


def _doc_priority(file: str) -> int:
    f = file.lower()
    if re.search(r"annual|ar[_.\-]|годов|年报|年度|20-?f|10-?k", f):
        return 0
    if re.search(r"results|press|interim|quarter|qr|q[1-4]|h[12]|мсфо|рсбу|季度|中期", f):
        return 1
    return 2


SCALE = {"billion": 1000.0, "bn": 1000.0, "thousand": 0.001, "trillion": 1e6, "million": 1.0, "none": 1.0, "": 1.0}


def norm_value(f: dict) -> float:
    """Денежные показатели приводим к «млн» механически по масштабу из отчёта (валюту не конвертируем)."""
    if f["metric"] == "dividend_per_share":
        return f["value"]
    if f.get("currency") and f["currency"].upper() != "":
        return f["value"] * SCALE.get(f.get("scale", "").lower(), 1.0)
    return f["value"]


def _conflicting(cands: list[dict]) -> bool:
    values: dict[str, set] = defaultdict(set)
    for f in cands:
        values[f.get("currency", "").upper()].add(round(norm_value(f), 6))
    return any(len(v) > 1 for v in values.values())


def _pick(cands: list[dict]) -> dict:
    """Лучший кандидат: годовой документ раньше квартального, USD раньше остальных валют, ранние страницы раньше."""
    return sorted(cands, key=lambda x: (_doc_priority(x["file"]), x.get("currency", "").upper() != "USD",
                                        x["currency"] == "", x["page"]))[0]


def load_facts(companies=None, years=None) -> list[dict]:
    facts = []
    for f in sorted((DATA / "extracted").glob("*/*.json")):
        if companies and f.parent.name not in companies:
            continue
        for x in json.loads(f.read_text(encoding="utf-8"))["facts"]:
            if years and not any(str(y) in x["period"] for y in years):
                continue
            facts.append(x)
    return facts


def build_excel(out: Path, companies=None, years=None, include_targets: bool = False, log=print) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    facts = [f for f in load_facts(companies, years)
             if f["metric"] in KEYS and f["segment"] != "upstream" and (include_targets or f["kind"] == "actual")]
    disp = _display_names()
    if not facts:
        log("! Фактов нет — файл не перезаписан (чтобы не затереть прежнюю таблицу).")
        return out
    names = {k: n for k, n, *_ in METRICS}
    groups = {k: g for k, _, _, g, _ in METRICS}
    default_unit = {k: u for k, _, u, _, _ in METRICS}

    cells: dict[tuple, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for f in facts:
        cells[(disp.get(f["company"], f["company"]), f["period"] or "—", f["segment"])][f["metric"]].append(f)

    wb = Workbook()
    ws = wb.active
    ws.title = "Сводная"
    head = ["Компания", "Период", "Сегмент"] + [f"{names[k]}" + (" (млн, USD в приоритете)" if default_unit[k] == "млн" else "") for k in KEYS]
    ws.append(head)
    sub = ["", "", ""] + [groups[k] for k in KEYS]
    ws.append(sub)
    conflicts = []
    cell_notes: dict[tuple[int, int], str] = {}
    for (company, period, segment), m in sorted(cells.items()):
        row = [company, period, segment]
        for k in KEYS:
            cand = m.get(k)
            if not cand:
                row.append(None)  # нет данных — пусто
                continue
            best = _pick(cand)
            row.append(None if _conflicting(cand) else norm_value(best))
            cell_notes[(ws.max_row + 1, len(row))] = (
                f"{best['value_text']} {best['unit']} {best['scale']} {best['currency']}\n"
                f"{best['file']}, стр. {best['page']}\n«{best['quote'][:150]}»" +
                (f"\nОпределение: {best['definition']}" if best["definition"] else "") +
                ("\nКОНФЛИКТ: значение не выбрано, см. лист Конфликты." if _conflicting(cand) else ""))
            by_cur: dict[str, set] = defaultdict(set)
            for c in cand:  # конфликт — разные значения в одной валюте
                by_cur[c["currency"].upper()].add(round(norm_value(c), 3))
            if any(len(v) > 1 for v in by_cur.values()):
                for c in cand:
                    conflicts.append([company, period, segment, names[k], c["value"], c["unit"], c["scale"],
                                      c["currency"], c["definition"], c["file"], c["page"], c["quote"]])
        ws.append(row)
    from openpyxl.comments import Comment
    for (r, c), text in cell_notes.items():
        ws.cell(row=r, column=c).comment = Comment(text, "downstream_agent", width=380, height=140)
    fill = PatternFill("solid", fgColor="DDEBF7")
    for c in ws[1] + ws[2]:
        c.font = Font(bold=True)
        c.fill = fill
        c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "D3"
    for i in range(1, len(head) + 1):
        ws.column_dimensions[get_column_letter(i)].width = 16 if i > 3 else 18

    wf = wb.create_sheet("Факты")
    wf.append(["Компания", "Период", "Сегмент", "Показатель", "Значение", "Как написано", "Единица", "Масштаб", "Валюта",
               "Определение", "Тип", "Файл", "Стр.", "Цитата", "Проверка"])
    for f in sorted(facts, key=lambda x: (x["company"], x["period"], x["segment"], x["metric"], x["file"], x["page"])):
        wf.append([f["company"], f["period"], f["segment"], names[f["metric"]], f["value"], f["value_text"], f["unit"],
                   f["scale"], f["currency"], f["definition"], f["kind"], f["file"], f["page"], f["quote"],
                   f.get("verified", "точная")])
    wf.freeze_panes = "A2"
    wf.auto_filter.ref = wf.dimensions

    wc = wb.create_sheet("Конфликты")
    wc.append(["Компания", "Период", "Сегмент", "Показатель", "Значение", "Единица", "Масштаб", "Валюта", "Определение", "Файл", "Стр.", "Цитата"])
    for r in conflicts:
        wc.append(r)

    wv = wb.create_sheet("Покрытие")
    comps = sorted({f["company"] for f in facts})
    wv.append(["Компания"] + [names[k] for k in KEYS])
    for c in comps:
        wv.append([c] + [sum(1 for f in facts if f["company"] == c and f["metric"] == k) or None for k in KEYS])

    _add_compare_sheet(wb, cells, names, disp)
    wb.move_sheet("Сравнение", offset=-(len(wb.sheetnames) - 2))
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    log(f"Excel: {out} — компаний {len(comps)}, фактов {len(facts)}, конфликтов {len(conflicts)}")
    return out


def _display_names() -> dict[str, str]:
    from downstream_agent.collector import load_config
    try:
        return {c["id"]: c["name"] for c in load_config(ROOT / "companies.toml")["companies"]}
    except Exception:
        return {}


def _add_compare_sheet(wb, cells, names: dict, disp: dict) -> None:
    """Лист «Сравнение»: выпадающие списки + формулы. Данные берутся с листа «Данные» (одна строка = один ключ)."""
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter as L
    from openpyxl.worksheet.datavalidation import DataValidation

    best: dict[str, tuple] = {}
    for (company, period, segment), m in cells.items():
        for k, cand in m.items():
            if _conflicting(cand):
                continue
            b = _pick(cand)
            best[f"{company}|{period}|{segment}|{names[k]}"] = (norm_value(b), b["unit"] or "", b["currency"], b["file"], b["page"])

    all_companies = list(dict.fromkeys(disp.values())) or sorted({k.split("|")[0] for k in best})
    with_data = {k.split("|")[0] for k in best}
    for c in sorted(with_data - set(all_companies)):
        all_companies.append(c)
    periods = sorted({k.split("|")[1] for k in best if k.split("|")[1] != "—"}, key=lambda p: period_info(p)[0]) or ["—"]
    segments = sorted({k.split("|")[2] for k in best} | {"group", "downstream"})
    M, N = len(all_companies), len(periods)
    PC = min(N, 16)  # столбцов периодов в блоке каждого показателя

    # --- Данные (ключ -> значение), Списки
    wd = wb.create_sheet("Данные")
    wd.append(["Ключ (компания|период|сегмент|показатель)", "Значение", "Единица", "Валюта", "Файл", "Стр."])
    for key, (v, u, cur, file, page) in sorted(best.items()):
        wd.append([key, v, u, cur, file, page])
    wl = wb.create_sheet("Списки")
    wl.append(["Показатели", "Сегменты", "Типы периодов", "Периоды", "Тип периода", "", "Компании", "Период в диапазоне", "Компания выбрана", "", "Индекс «от/до»"])
    mnames = [n for _, n, *_ in METRICS]
    for i in range(max(len(mnames), len(segments), len(TYPES), N, M)):
        wl.cell(row=i + 2, column=1, value=mnames[i] if i < len(mnames) else None)
        wl.cell(row=i + 2, column=2, value=segments[i] if i < len(segments) else None)
        wl.cell(row=i + 2, column=3, value=TYPES[i] if i < len(TYPES) else None)
        if i < N:
            wl.cell(row=i + 2, column=4, value=periods[i])
            wl.cell(row=i + 2, column=5, value=period_info(periods[i])[1])
        if i < M:
            wl.cell(row=i + 2, column=7, value=all_companies[i])
    SC = "'Сравнение'"
    wl["K2"] = f"=IFERROR(MATCH({SC}!$B$3,$D$2:$D${N + 1},0),1)"
    wl["K3"] = f"=IFERROR(MATCH({SC}!$B$4,$D$2:$D${N + 1},0),{N})"
    wl["J2"], wl["J3"] = "от", "до"
    for i in range(N):
        r = i + 2
        wl.cell(row=r, column=8, value=(
            f'=IF(AND(ROW()-1>=$K$2,ROW()-1<=$K$3,OR({SC}!$B$2="Все",E{r}={SC}!$B$2)),COUNT(H$1:H{r - 1})+1,"")'))
    # --- Сравнение: строки — компании (фильтр Excel), блоки столбцов — показатели, внутри периоды
    ws = wb.create_sheet("Сравнение")
    bold, head_fill = Font(bold=True), PatternFill("solid", fgColor="DDEBF7")
    group_fills = [PatternFill("solid", fgColor=c) for c in ("DDEBF7", "E2EFDA", "FCE4D6", "EDEDED", "FFF2CC")]
    input_fill = PatternFill("solid", fgColor="FFF2CC")
    ws["A1"] = "Выбор параметров (жёлтые ячейки — выпадающие списки)"
    ws["A1"].font = Font(bold=True, size=12)
    labels = ["Тип периода", "Период от", "Период до", "Показатель / сегмент →", "Показатель 1", "Показатель 2",
              "Показатель 3", "Показатель 4", "Показатель 5"]
    for i, lab in enumerate(labels):
        ws.cell(row=2 + i, column=1, value=lab).font = bold
        ws.cell(row=2 + i, column=2).fill = input_fill
    ws["B2"], ws["B3"], ws["B4"] = "Все", periods[0], periods[-1]
    ws["B5"], ws["C5"] = "показатель", "сегмент"
    ws["B5"].font = ws["C5"].font = bold
    ws["B6"], ws["B7"], ws["C6"], ws["C7"] = "ROACE", "Объём переработки", "group", "downstream"
    for r in range(6, 11):
        ws.cell(row=r, column=3).fill = input_fill
    ws["E2"] = "Таблица ниже: показатели идут блоками слева направо, внутри — периоды. Компании фильтруются стрелкой в «Компания»."
    ws["E3"] = "Значения в млн (USD в приоритете, масштаб приведён механически), проценты как в отчёте. Пусто = нет данных."
    ws["E4"] = "Сегмент задаётся для каждого показателя отдельно (group — в целом по компании)."
    for r in (2, 3, 4):
        ws.cell(row=r, column=5).font = Font(italic=True, size=9)

    def dv(rng: str, src: str):
        v = DataValidation(type="list", formula1=src, allow_blank=True)
        ws.add_data_validation(v)
        v.add(rng)

    dv("B2", f"='Списки'!$C$2:$C${len(TYPES) + 1}")
    dv("B3:B4", f"='Списки'!$D$2:$D${N + 1}")
    dv("C6:C10", f"='Списки'!$B$2:$B${len(segments) + 1}")
    dv("B6:B10", f"='Списки'!$A$2:$A${len(mnames) + 1}")

    top, hdr, first = 12, 13, 14  # строка названий показателей, строка шапки (с фильтром), первая компания
    ws.cell(row=hdr, column=1, value="Компания")
    ws.cell(row=hdr, column=2, value="Есть данные")
    for c in (1, 2):
        ws.cell(row=hdr, column=c).font = bold
        ws.cell(row=hdr, column=c).fill = head_fill
    for i, name in enumerate(all_companies):
        r = first + i
        ws.cell(row=r, column=1, value=name)
        ws.cell(row=r, column=2, value=f'=IF(COUNTIF(\'Данные\'!$A:$A,$A{r}&"|*")>0,"да","нет")')
        if name not in with_data:
            ws.row_dimensions[r].hidden = True  # по умолчанию показаны только компании с данными
    side = Side(style="medium", color="808080")
    for g in range(5):
        c0 = 3 + g * PC
        t = f"${L(c0)}${top}"
        cell = ws.cell(row=top, column=c0, value=f'=IF($B${6 + g}="","",$B${6 + g}&IF($C${6 + g}="",""," · "&$C${6 + g}))')
        cell.font = Font(bold=True, size=12)
        ws.merge_cells(start_row=top, start_column=c0, end_row=top, end_column=c0 + PC - 1)
        title_ref = f"$B${6 + g}"
        for j in range(PC):
            col = L(c0 + j)
            hc = ws.cell(row=hdr, column=c0 + j, value=(
                f'=IF({title_ref}="","",IFERROR(INDEX(\'Списки\'!$D$2:$D${N + 1},MATCH({j + 1},\'Списки\'!$H$2:$H${N + 1},0)),""))'))
            hc.font, hc.fill = bold, group_fills[g]
            ws.cell(row=top, column=c0 + j).fill = group_fills[g]
            for i in range(M):
                r = first + i
                ws.cell(row=r, column=c0 + j, value=(
                    f'=IF(OR({col}${hdr}="",{title_ref}=""),"",IFERROR(VLOOKUP($A{r}&"|"&{col}${hdr}&"|"&$C${6 + g}&"|"&{title_ref},\'Данные\'!$A:$B,2,FALSE),""))'))
            ws.column_dimensions[col].width = 11
        for r in range(top, first + M):  # разделитель между блоками показателей
            cl = ws.cell(row=r, column=c0)
            cl.border = Border(left=side)
    last_col = 2 + 5 * PC
    ws.auto_filter.ref = f"A{hdr}:{L(last_col)}{first + M - 1}"
    ws.auto_filter.add_filter_column(1, ["да"])  # Есть данные = да
    ws.freeze_panes = ws.cell(row=first, column=3)
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 14
    wd.column_dimensions["A"].width = 60
    wd.sheet_state, wl.sheet_state = "hidden", "hidden"
