"""Агент-аналитик: вопрос на естественном языке -> план поиска (LLM) -> выбор страниц ->
ответ (LLM) с цитатами, проверенными по тексту. Правил под конкретные метрики нет.

Корпус: распознанные PDF (data/text) и сохранённые новости (data/raw/*/news).
"""
from __future__ import annotations

import json
import re

from downstream_agent import llm
from downstream_agent.collector import DATA

PLAN = (
    "You plan a search over a corpus of oil-company reports and news. Given the user's question, "
    "reply JSON: {\"companies\": [company ids from the list that matter, empty = all], "
    "\"terms\": [up to 10 short words or abbreviations that would literally appear on the page that "
    "contains the answer: translations of the question's key words into English (and Russian/Chinese "
    "if the company is from Russia/China), standard abbreviations (ROACE, EBITDA, capex), table row labels. "
    "NEVER invent company names or numbers], "
    "\"sources\": [\"reports\" and/or \"news\"]}."
)
ANSWER = (
    "Answer the question using ONLY the numbered pages below. Never compute or guess numbers; "
    "if the pages do not contain the answer, say so. Reply JSON: {\"answer\": str (same language as the question), "
    "\"evidence\": [{\"page_ref\": number of the page, \"quote\": text copied exactly from that page}]}."
)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def load_corpus(sources=("reports", "news")) -> list[dict]:
    docs = []
    if "reports" in sources:
        for f in sorted((DATA / "text").glob("*/*.jsonl")):
            for line in open(f, encoding="utf-8"):
                p = json.loads(line)
                tbl = "\n".join(" | ".join(str(c or "") for c in r) for t in p["tables"] for r in t)
                docs.append({"company": f.parent.name, "file": f.stem, "page": p["page"],
                             "text": p["text"] + ("\n" + tbl if tbl else ""), "kind": "report"})
    if "news" in sources:
        for f in sorted((DATA / "raw").glob("*/news/*.json")):
            n = json.loads(f.read_text(encoding="utf-8"))
            body = re.sub(r"\s+", " ", n.get("text") or n.get("summary") or "")[:3000]
            docs.append({"company": f.parent.parent.name, "file": f.stem, "page": 0, "kind": "news",
                         "text": f"{n.get('title','')} ({n.get('published','')}) {body}"})
    return docs


def question_terms(q: str) -> list[str]:
    """Слова самого вопроса: аббревиатуры и числа/годы (то, что в тексте совпадёт буквально)."""
    return [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z&/\-]{2,}|20[12]\d", q)]


def rank(docs: list[dict], terms: list[str], k: int) -> list[dict]:
    import math
    terms = list(dict.fromkeys(t for t in terms if t))
    low = [d["text"].lower() for d in docs]
    idf = {t: math.log(1 + len(docs) / (1 + sum(t in x for x in low))) for t in terms}
    scored, seen = [], set()
    for d, t in zip(docs, low):
        key = _norm(t)[:400]
        if key in seen:  # одинаковые страницы из дублей файлов
            continue
        seen.add(key)
        sc = sum(idf[x] * min(t.count(x), 3) for x in terms)
        if sc > 0:
            scored.append((sc, d))
    return [d for _, d in sorted(scored, key=lambda x: -x[0])[:k]]


def ask(question: str, model: str = llm.MODEL, k: int = 3, log=print) -> dict:
    companies = sorted({d.name for d in (DATA / "raw").glob("*") if d.is_dir()})
    plan = llm.ask_json(PLAN, f"Company ids: {companies}\nQuestion: {question}", model=model, timeout=600)
    terms = question_terms(question) * 2 + [str(x).lower() for x in plan.get("terms", [])]
    wanted = [c for c in plan.get("companies", []) if c in companies] or None
    sources = [s for s in plan.get("sources", ["reports", "news"]) if s in ("reports", "news")] or ["reports", "news"]
    log(f"план: компании={wanted or 'все'} источники={sources}\n      термины={terms}")
    docs = [d for d in load_corpus(sources) if not wanted or d["company"] in wanted]
    top = rank(docs, terms, k)
    if not top:
        return {"answer": "В корпусе не найдено подходящих страниц.", "evidence": []}
    for d in top:
        log(f"  страница: {d['company']} / {d['file'][:40]} / стр. {d['page']}")
    pages = "\n\n".join(f"[{i}] {d['company']} {d['file']} p.{d['page']}\n{d['text'][:2500]}" for i, d in enumerate(top))
    out = llm.ask_json(ANSWER, f"Question: {question}\n\nPages:\n{pages}", model=model, timeout=900, num_ctx=8192)
    ev = []
    for e in out.get("evidence", []):
        try:
            d = top[int(e["page_ref"])]
        except (KeyError, ValueError, IndexError):
            continue
        quote = str(e.get("quote", ""))
        nums = re.findall(r"\d[\d.,]*", out.get("answer", ""))
        # цитата должна быть дословной, содержательной и содержать число из ответа
        if len(quote) >= 20 and _norm(quote) in _norm(d["text"]) and (not nums or any(n in quote for n in nums)):
            ev.append({"company": d["company"], "file": d["file"], "page": d["page"], "quote": e["quote"]})
    return {"answer": out.get("answer", ""), "evidence": ev, "verified": bool(ev)}
