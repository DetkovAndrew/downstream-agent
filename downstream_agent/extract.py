"""Извлечение показателей (ROACE и др.) из распознанных страниц локальным Qwen.

Модель не считает и не додумывает: она копирует значение вместе с точной цитатой.
Факт принимается, только если цитата дословно есть на странице и число есть в цитате.
"""
from __future__ import annotations

import json
import re

from downstream_agent import llm
from downstream_agent.collector import DATA

METRICS = ["ROACE", "ROCE", "ROE", "capital employed", "refinery throughput", "refining margin",
           "downstream EBITDA", "downstream capex", "utilization"]
KEYWORDS = ["roace", "roce", "return on average capital", "return on capital employed", "capital employed",
            "refinery throughput", "refining margin", "downstream", "refining", "marketing",
            "净资产收益率", "已动用资本", "炼油", "加工量", "рентабельность", "переработка", "нефтепереработка",
            "капитал"]

SYSTEM = (
    "You extract financial KPIs from a page of an oil company report. Allowed metrics: "
    + ", ".join(METRICS) + ". Copy only values explicitly stated on the page; never compute or guess. "
    "Reply JSON: {\"facts\": [{\"metric\": str, \"value\": str as written, \"unit\": str, "
    "\"period\": str, \"segment\": str or \"group\", \"quote\": exact text copied from the page containing the value}]}. "
    "If nothing relevant: {\"facts\": []}."
)


# --- Шаг 1: правила (мгновенно, без модели) -------------------------------------------------
NUM = r"-?\d{1,3}(?:[,\u00a0 ]\d{3})*(?:[.,]\d+)?"
def _pct(label: str) -> str:
    # «ROACE ... 19.8%» (до 60 символов, в т.ч. через перенос) или таблица «ROACE* (%) 19.8»
    return rf"(?:{label})(?:[\s\S]{{0,60}}?({NUM})\s?%|[^\d\n]{{0,15}}\(%\)\s*({NUM}))"


RULES = {
    "ROACE": _pct(r"\bROACE\b|return on average capital employed|рентабельность (?:среднего )?(?:задействованного|используемого|инвестированного) капитала"),
    "ROCE": _pct(r"\bROCE\b|return on capital employed|已动用资本回报率|资本回报率"),
    "ROE": _pct(r"\bROE\b|return on (?:average )?equity|рентабельность (?:среднего )?собственного капитала|净资产收益率|加权平均净资产收益率"),
    "utilization": _pct(r"(?:refinery |capacity )?utili[sz]ation|загрузка (?:мощностей|НПЗ)|开工率|产能利用率"),
    "refinery throughput_ru": rf"(?:(?:объем|объём) переработки[^\n]{{0,120}}?({NUM})\s?(?:млн\s?т|млн тонн|тыс\.?\s?т)|({NUM})\s?(?:млн\s?т|млн тонн)\s+(?:объем|объём) переработки)",
    "refinery throughput_zh": rf"(?:原油加工量|加工原油)[^\n，。,;；]{{0,15}}?({NUM})\s?(?:万吨|百万吨|亿吨|百万桶|万桶)",
    "ebitda margin_ru": rf"({NUM})\s?%\s+рентабельность по (?:скорректированной )?EBITDA",
    "capital employed": rf"(?:average )?capital employed[^\n]{{0,120}}?({NUM})",
}
# --- Шаг 2: проверка смысла для фактов от LLM ------------------------------------------------
_MONEY = r"[$€¥£]|usd|sar|rub|rmb|cny|eur|gbp|million|billion|bn|mn|руб|млн|млрд"
UNIT_OK = {
    "ROACE": r"%", "ROCE": r"%", "ROE": r"%", "utilization": r"%",
    "refining margin": r"/\s?(bbl|barrel|b\b|t\b|tonne)|per (bbl|barrel|tonne)",
    "refinery throughput": r"mb/d|kb/d|mbd|bbl|barrel|tonne|tons|mmt|\bmt\b",
    "refinery throughput_ru": r"млн|тыс", "refinery throughput_zh": r"万吨|百万吨|亿吨|百万桶|万桶",
    "ebitda margin_ru": r"%",
    "capital employed": _MONEY, "downstream EBITDA": _MONEY, "downstream capex": _MONEY,
}
KEY_IN_QUOTE = {
    "ROACE": r"roace|return on average capital|рентабельность.*капитала", "ROCE": r"roce|return on capital|资本回报率",
    "ROE": r"roe\b|return on (average )?equity|рентабельность.*собственного|净资产收益率", "refining margin": r"refining.*margin|margin.*refin",
    "refinery throughput": r"throughput|переработки|加工",
    "refinery throughput_ru": r"переработки", "refinery throughput_zh": r"加工", "ebitda margin_ru": r"рентабельность по", "utilization": r"utili[sz]ation|загрузка|开工率|利用率",
    "capital employed": r"capital employed", "downstream EBITDA": r"ebitda", "downstream capex": r"capex|capital expenditure",
}


def plausible(f: dict) -> bool:
    """Отсекает факты, где единица или формулировка не соответствуют метрике."""
    m, quote = f.get("metric"), _norm(str(f.get("quote", "")))
    if m not in UNIT_OK:
        return False
    unit = _norm(f"{f.get('value', '')} {f.get('unit', '')} {quote}")
    if not re.search(KEY_IN_QUOTE[m], quote) or not re.search(UNIT_OK[m], unit):
        return False
    if m in ("ROACE", "ROCE", "ROE", "utilization", "ebitda margin_ru"):
        try:
            v = float(re.sub(r"[^\d.\-]", "", str(f["value"]).replace(",", ".")))
        except ValueError:
            return False
        low, high = (10, 100) if m == "utilization" else (-30, 60)
        return low <= v <= high
    return True


TARGET = re.compile(r"target|ambition|expect|aim|outlook|guidance|forecast|to around|to over|>|≥|end of 20(?:2[6-9]|3\d)|weighting|\(\d+%\s*$", re.I)


def rule_facts(p: dict, company: str, file: str) -> list[dict]:
    text, out, seen = _page_text(p), [], set()
    year = re.findall(r"(?<!\d)(20[12]\d)(?!\d)", text[:3000])
    for metric, rx in RULES.items():
        for m in re.finditer(rx, text, re.I):
            quote = re.sub(r"\s+", " ", m.group(0)).strip()[:300]
            value = next(g for g in m.groups() if g)
            period = " ".join(sorted(set(re.findall(r"20[12]\d", quote))) or year[:1])
            f = {"metric": metric.split("_")[0], "value": value, "unit": "", "period": period,
                 "segment": "downstream" if re.search("downstream|refin", quote, re.I) else "group",
                 "quote": quote, "company": company, "file": file, "page": p["page"], "method": "rule"}
            key = (f["metric"], f["value"], f["period"])
            f["kind"] = "target" if TARGET.search(quote) else "actual"
            if plausible({**f, "metric": metric, "unit": m.group(0)}) and key not in seen:
                seen.add(key)
                out.append(f)
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _page_text(p: dict) -> str:
    tbl = "\n".join(" | ".join(str(c or "") for c in row) for t in p.get("tables", []) for row in t)
    return f"{p['text']}\n{tbl}" if tbl else p["text"]


def rank_pages(pages: list[dict], top: int) -> list[dict]:
    def score(p):
        t = p["text"].lower()
        return sum(t.count(k) for k in KEYWORDS)
    return [p for p in sorted(pages, key=score, reverse=True)[:top] if score(p) > 0]


def extract_file(jsonl, company: str, top: int = 10, model: str = llm.MODEL, log=print, use_llm: bool = False) -> list[dict]:
    pages = [json.loads(l) for l in open(jsonl, encoding="utf-8")]
    facts = [f for p in pages for f in rule_facts(p, company, jsonl.name)]
    if not use_llm:
        return facts
    have = {(f["page"], f["metric"]) for f in facts}
    for p in sorted(rank_pages(pages, top), key=lambda x: x["page"]):
        text = _page_text(p)[:5000]
        try:
            out = llm.ask_json(SYSTEM, f"Page {p['page']}:\n{text}", model=model, timeout=900)
        except llm.LlmUnavailable as e:
            log(f"   ! LLM: {e}")
            break
        hay = _norm(text)
        for f in out.get("facts", []):
            q, v = _norm(str(f.get("quote", ""))), _norm(str(f.get("value", "")))
            if q and v and q in hay and v in q and plausible(f) and (p["page"], f.get("metric")) not in have:
                facts.append({**f, "company": company, "file": jsonl.name, "page": p["page"], "method": "llm"})
    return facts


def extract_all(only: list[str] | None = None, model: str = llm.MODEL, top: int = 10, log=print,
                use_llm: bool = False, force: bool = False) -> int:
    n = 0
    for jsonl in sorted((DATA / "text").glob("*/*.jsonl")):
        company = jsonl.parent.name
        if only and company not in only:
            continue
        out = DATA / "metrics" / company / jsonl.name
        if out.exists() and not force:
            continue
        log(f"   {jsonl.name}")
        facts = extract_file(jsonl, company, top, model, log, use_llm)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("".join(json.dumps(f, ensure_ascii=False) + "\n" for f in facts), encoding="utf-8")
        log(f"     фактов с проверенной цитатой: {len(facts)}")
        n += len(facts)
    return n
