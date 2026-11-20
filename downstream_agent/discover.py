"""Агентный поиск: сам находит отчёты и пресс-релизы компании без заранее заданных ссылок.

Цикл: поисковые запросы -> кандидаты -> LLM отбирает релевантные -> скачивание ->
проверка по тексту PDF, что документ действительно этой компании.
Если LLM недоступна, работает эвристика (имя компании в заголовке/ссылке).
"""
from __future__ import annotations

import io
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from ddgs import DDGS
from lxml import html
from pypdf import PdfReader

logging.getLogger('pypdf').setLevel(logging.ERROR)
from downstream_agent import llm
from downstream_agent.collector import Collector, Report

DOC_TYPES = {
    "annual": "{name} annual report {year} pdf",
    "results": "{name} {year} full year results financial statements pdf",
    "quarterly": "{name} quarterly results {year} pdf",
    "release": "{name} press release {year} refining downstream",
}

DOC_TYPES_LANG = {
    "ru": {
        "annual": "{name} годовой отчет {year} pdf",
        "results": "{name} финансовые результаты МСФО {year} pdf",
        "quarterly": "{name} результаты квартал МСФО {year} pdf",
        "release": "{name} пресс-релиз {year} нефтепереработка",
    },
    "zh": {
        "annual": "{name} {year}年年度报告 pdf",
        "results": "{name} {year}年 业绩公告 pdf",
        "quarterly": "{name} {year}年 季度报告 中期报告 pdf",
        "release": "{name} {year}年 新闻稿 炼油",
    },
}
NEWS_TOPICS_LANG = {
    "ru": ["{name} нефтепереработка НПЗ", "{name} финансовые результаты прибыль", "{name} инвестиции санкции сделка"],
    "zh": ["{name} 炼油 成品油", "{name} 业绩 利润", "{name} 投资 收购 制裁"],
}

SYSTEM = (
    "You select search results that are OFFICIAL financial documents (annual report, "
    "quarterly/interim results, financial statements, results presentation, press release) "
    "published by the given company itself. Reject third-party sites, aggregators, "
    "other companies and unrelated pages. Reply JSON: {\"picks\": [result numbers]}."
)


class Discoverer:
    def __init__(self, collector: Collector, use_llm: bool = True, log=print, min_year: int | None = None,
                 browser: bool = False):
        self.browser = browser
        self.now_year = time.localtime().tm_year
        # свежее: по умолчанию последний завершённый год и текущий
        self.min_year = min_year or (self.now_year - 1)
        self.c = collector
        self.use_llm = use_llm
        self.log = log

    def _search(self, query: str, n: int = 8) -> list[dict]:
        try:
            if llm.backend() == "gemini":
                found = llm.web_search(query, n)
                if found:
                    return found
            return DDGS().text(query, max_results=n) or []
        except Exception as e:
            self.log(f"   ! поиск '{query}': {e}")
            return []

    @staticmethod
    def _query_name(c: dict, tpl: str) -> str:
        """Для запроса на китайском/русском берём имя на этом языке (первый подходящий алиас)."""
        zh = re.search(r"[\u4e00-\u9fff]", tpl)
        ru = re.search(r"[а-яё]", tpl, re.I)
        if not zh and not ru:  # английский шаблон — нужно латинское имя (иначе «Роснефть refinery»)
            for a in [c["name"], *c.get("aliases", [])]:
                if a.isascii():
                    return a
            return c["name"]
        for a in [c["name"], *c.get("aliases", [])]:
            if zh and re.search(r"[\u4e00-\u9fff]", a) or ru and re.search(r"[а-яё]", a, re.I):
                return a
        return c["name"]

    def _aliases(self, c: dict) -> list[str]:
        names = [c["name"], *c.get("aliases", [])]
        return [n.lower() for n in names]

    def _heuristic(self, c: dict, results: list[dict]) -> list[int]:
        al = self._aliases(c)
        bad = [x.lower() for x in c.get("exclude", [])]
        def text(r):
            return f"{r.get('title','')} {r['href']} {r.get('body','')}".lower()
        return [i for i, r in enumerate(results)
                if any(a in text(r) for a in al) and not any(b in text(r) for b in bad)]

    def _official(self, c: dict, results: list[dict]) -> list[int]:
        """Результаты с домена самой компании (shell.com, bp.com...) — всегда кандидаты, что бы ни выбрала модель."""
        names = [re.sub(r"\W", "", a) for a in self._aliases(c) if re.fullmatch(r"[a-z0-9 .&\-]+", a)]
        out = []
        for i, r in enumerate(results):
            host = urlparse(r["href"]).netloc.lower().split(":")[0]
            sld = re.sub(r"\W", "", host.split(".")[-2] if host.count(".") >= 1 else host)
            bad = [b.lower() for b in c.get("exclude", [])]
            if any(b and (b in sld or b.replace(" ", "") in sld) for b in bad):
                continue
            if any(n == sld or (len(n) >= 4 and n in sld) for n in names):
                out.append(i)
        return out

    def _pick(self, c: dict, results: list[dict], doc_type: str, year: int) -> list[int]:
        if not results:
            return []
        picks = self._pick_llm(c, results, doc_type, year)
        official = [i for i in self._official(c, results) if i not in picks]
        if official:
            self.log(f"   официальный сайт компании: добавляю {len(official)} рез. к выбору модели")
        return picks + official

    def _pick_llm(self, c: dict, results: list[dict], doc_type: str, year: int) -> list[int]:
        if self.use_llm:
            listing = "\n".join(f"{i}. {r.get('title','')} | {r['href']} | {r.get('body','')[:120]}"
                                for i, r in enumerate(results))
            try:
                out = llm.ask_json(SYSTEM, f"Company: {c['name']}\nWanted: {doc_type} {year}\nResults:\n{listing}")
                picks = [int(i) for i in out.get("picks", []) if str(i).isdigit() and int(i) < len(results)]
                # LLM маленькая: пересекаем с эвристикой, чтобы не брать чужое
                return [i for i in picks if i in set(self._heuristic(c, results))]
            except llm.LlmUnavailable as e:
                self.log(f"   ! LLM недоступна ({e}), использую эвристику")
                self.use_llm = False
        return self._heuristic(c, results)

    def _pdf_links(self, url: str) -> list[str]:
        links: list[str] = []
        try:
            doc = html.fromstring(self.c._get(url).content, base_url=url)
            doc.make_links_absolute(url)
            links = [a.get("href").split("#")[0] for a in doc.iter("a")
                     if (a.get("href") or "").lower().split("?")[0].endswith(".pdf")]
        except Exception:
            pass
        if not links and self.browser:  # ссылки на PDF рисуются скриптами — смотрим глазами браузера
            from downstream_agent import browser
            try:
                found = browser.render_links(url)
                links = list(dict.fromkeys(h.split("#")[0] for h, _ in found if h.lower().split("?")[0].endswith(".pdf")))
                self.log(f"   браузер: {url[:70]} → PDF-ссылок {len(links)}")
            except browser.BrowserUnavailable as e:
                self.log(f"   ! браузер: {e}")
                self.browser = False  # дальше не пробуем в этот запуск
        if not links:
            self.log(f"   страница без PDF-ссылок: {url[:85]}" + ("" if self.browser else " (браузерный режим выключен)"))
        return links

    def _verify(self, c: dict, path, url: str = "") -> tuple[bool, str]:
        """(годен, причина). Проверяет, что документ этой компании и достаточно свежий."""
        try:
            r = PdfReader(io.BytesIO(path.read_bytes()))
            head = " ".join((r.pages[i].extract_text() or "") for i in range(min(3, len(r.pages)))).lower()
        except Exception:
            return True, ""  # скан без текста: решит OCR позже
        if head.strip() and not any(a in head for a in self._aliases(c)):
            return False, "не этой компании"
        years = [int(y) for y in re.findall(r"(?<!\d)(20[12]\d)(?!\d)", f"{head} {url}")]
        years = [y for y in years if y <= self.now_year + 1]
        if years and max(years) < self.min_year:
            return False, f"старый документ ({max(years)})"
        return True, ""

    def run(self, years: list[int], types: list[str], only: list[str] | None = None) -> list[Report]:
        out = []
        for c in self.c.companies:
            if only and c["id"] not in only:
                continue
            rep = Report(c["id"])
            self.log(f"== поиск: {c['name']}")
            seen: set[str] = set()
            for year in years:
                for t in types:
                    queries = [DOC_TYPES[t]] if "en" in c.get("langs", ["en"]) else []
                    queries += [DOC_TYPES_LANG[l][t] for l in c.get("langs", []) if l in DOC_TYPES_LANG]
                    results = []
                    for tpl in queries:
                        results += self._search(tpl.format(name=self._query_name(c, tpl), year=year))
                    if results and not self._heuristic(c, results):
                        # выдача есть, но к компании не относится (бывает мусор) — повторяем в другой формулировке
                        alt = f"{self._query_name(c, 'en')} investors results and reporting {year} {t}"
                        self.log(f"   выдача не про {c['name']} — повторяю запрос: {alt!r}")
                        results += self._search(alt)
                    for i in self._pick(c, results, t, year):
                        url = results[i]["href"]
                        links = [url] if urlparse(url).path.lower().endswith(".pdf") else self._pdf_links(url)[:5]
                        for link in links:
                            if link in seen:
                                continue
                            old = [int(y) for y in re.findall(r"(?<!\d)(20[12]\d)(?!\d)", link)]
                            if old and max(old) < self.min_year:
                                continue
                            seen.add(link)
                            before = len(rep.downloaded)
                            self.c._download(c, link, "reports", rep, doc_type=t, year=year, via="search")
                            if len(rep.downloaded) > before:
                                p = Path(rep.downloaded[-1])
                                ok, why = self._verify(c, p, link)
                                if not ok:
                                    self.log(f"   - {why}, удаляю {p.name}")
                                    p.unlink()
                                    rep.downloaded.pop()
                                    self.c.manifest.items.pop(link, None)
            self.log(f"   скачано {len(rep.downloaded)}, уже было {rep.skipped}, ошибок {len(rep.errors)}")
            out.append(rep)
        return out


NEWS_TOPICS = [
    "{name} refinery downstream",
    "{name} results profit",
    "{name} capex investment refining petrochemicals",
    "{name} sanctions OR acquisition OR outage",
]

NEWS_SYSTEM = (
    "You decide if a news item is relevant for comparing oil-company downstream business "
    "(refining, marketing, petrochemicals, financial results, investments, incidents, sanctions) "
    "of the given company. Reply JSON: {\"picks\": [item numbers]}."
)


def _ddg_news(topic: str, period: str, n: int, log) -> list[dict]:
    for attempt in range(2):  # DuckDuckGo часто даёт таймаут — один повтор
        try:
            return DDGS(timeout=15).news(topic, timelimit=period, max_results=n) or []
        except Exception as e:
            if "No results" in str(e):
                return []
            if attempt:
                log(f"   ! DuckDuckGo: {str(e)[:60]} — пропускаю остальные темы")
                return None
    return []


def discover_news(collector: Collector, only=None, period="m", per_query=8, use_llm=False, log=print,
                  sources=("gnews", "gdelt"), fulltext=False, cap=60) -> list[Report]:
    """Свежие новости по компаниям из нескольких источников (DuckDuckGo, Google News RSS, GDELT).

    Дубли по ссылке и заголовку отбрасываются. Релевантность окончательно решает `newsheet`;
    use_llm=True добавляет предварительный фильтр Gemini (тратит запросы).
    """
    import json
    from downstream_agent import newsfeeds
    from downstream_agent.newsheet import parse_date

    out = []
    disc = Discoverer(collector, use_llm=use_llm, log=log)
    for c in collector.companies:
        if only and c["id"] not in only:
            continue
        rep = Report(c["id"])
        log(f"== новости: {c['name']}")
        cands: list[dict] = []
        if "gnews" in sources:
            cands += newsfeeds.google_news(collector, c, period, log)
        if "gdelt" in sources:
            cands += newsfeeds.gdelt(collector, c, period, log)
        if "gnewsio" in sources:
            cands += newsfeeds.gnewsio(collector, c, period, log)
        if "newsapi" in sources:
            cands += newsfeeds.newsapi(collector, c, period, log)
        if "ddg" in sources:
            topics = (NEWS_TOPICS if "en" in c.get("langs", ["en"]) else []) + \
                [t for l in c.get("langs", []) for t in NEWS_TOPICS_LANG.get(l, [])]
            found = 0
            for topic in topics:
                batch = _ddg_news(topic.format(name=Discoverer._query_name(c, topic)), period, per_query, log)
                if batch is None:  # таймаут DuckDuckGo: остальные темы этой компании пропускаем
                    break
                for r in batch:
                    res = [{"href": r["url"], "title": r.get("title", ""), "body": r.get("body", "")}]
                    if disc._heuristic(c, res):
                        cands.append({"title": r.get("title", ""), "url": r["url"], "published": r.get("date", ""),
                                      "source": r.get("source", ""), "summary": r.get("body", ""), "provider": "ddg"})
                        found += 1
            log(f"   DuckDuckGo: {found}")
        seen_t, fresh = set(), []
        for it in cands:
            key = newsfeeds.norm_title(it["title"])
            if not it["url"] or not key or key in seen_t or collector.manifest.has(it["url"]):
                continue
            seen_t.add(key)
            fresh.append(it)
        fresh.sort(key=lambda x: parse_date(x["published"]) or parse_date("1970-01-01"), reverse=True)
        fresh = fresh[:cap]
        if use_llm and fresh:
            listing = "\n".join(f"{i}. {x['title']}" for i, x in enumerate(fresh))
            try:
                picks = llm.ask_json(NEWS_SYSTEM, f"Company: {c['name']}\nItems:\n{listing}").get("picks", [])
                keep = {int(i) for i in picks if str(i).isdigit()}
                fresh = [x for i, x in enumerate(fresh) if i in keep]
            except llm.LlmUnavailable as e:
                log(f"   ! LLM недоступна ({str(e)[:80]}), без фильтра")
        for it in fresh:
            if fulltext:
                try:
                    it["text"] = html.fromstring(collector._get(it["url"]).content).text_content()
                except Exception as e:
                    it["fetch_error"] = str(e)[:100]
            folder = collector.data / "raw" / c["id"] / "news"
            folder.mkdir(parents=True, exist_ok=True)
            import hashlib
            path = folder / (re.sub(r"\W+", "_", it["title"])[:50] + "_" + hashlib.sha1(it["url"].encode()).hexdigest()[:6] + ".json")
            path.write_text(json.dumps(it, ensure_ascii=False, indent=1), encoding="utf-8")
            collector.manifest.add(it["url"], company=c["id"], kind="news", file=str(path.relative_to(collector.data)))
            rep.downloaded.append(str(path))
        log(f"   новых: {len(rep.downloaded)} (из {len(cands)} найденных)")
        out.append(rep)
    return out
