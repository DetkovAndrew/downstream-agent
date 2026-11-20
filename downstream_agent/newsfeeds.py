"""Источники новостей: Google News RSS и GDELT (бесплатные, без ключей), на языках компании.

Каждый провайдер возвращает [{title, url, published (ISO), source, summary, provider}].
Условия Google News RSS разрешают фид для личного некоммерческого чтения — учитывайте при корпоративном использовании.
"""
from __future__ import annotations

import json
import re
import time
from urllib.parse import quote_plus

from lxml import etree

GN_LANG = {"en": ("en-US", "US", "US:en"), "ru": ("ru", "RU", "RU:ru"), "zh": ("zh-CN", "CN", "CN:zh-Hans")}
WHEN = {"d": "1d", "w": "7d", "m": "30d"}
GDELT_SPAN = {"d": "1d", "w": "1w", "m": "1m"}
CONTEXT_EN = "(oil OR refinery OR petroleum OR fuel OR gas OR energy OR petrochemical)"
CYR, CJK = re.compile(r"[а-яё]", re.I), re.compile(r"[一-鿿]")
_last_gdelt = 0.0
_gdelt_fails = 0  # подряд неудач: после 2 GDELT на этот запуск отключается (лимит по IP прокси)


def names_for(c: dict, lang: str) -> list[str]:
    """Названия компании на нужном языке (en — латиница, ru — кириллица, zh — иероглифы)."""
    out = []
    for n in [c["name"], *c.get("aliases", [])]:
        ok = {"ru": bool(CYR.search(n)), "zh": bool(CJK.search(n)),
              "en": not CYR.search(n) and not CJK.search(n)}[lang]
        if ok and len(n.strip()) > 2 and n.lower() not in out:
            out.append(n.lower() if lang != "zh" else n)
    # осмысленный регистр для латиницы: исходное написание имени/псевдонима
    if lang == "en":
        orig = {x.lower(): x for x in [c["name"], *c.get("aliases", [])]}
        out = [orig.get(n, n) for n in out]
    return out[:4]


def _ambiguous(c: dict, names: list[str]) -> bool:
    return any(len(n) <= 4 or n.lower() in {"shell", "saras", "total", "mol group"} for n in names)


def _query(c: dict, lang: str) -> str | None:
    names = names_for(c, lang)
    if not names:
        return None
    core = " OR ".join(f'"{n}"' for n in names)
    core = f"({core})" if len(names) > 1 else core
    if lang == "en" and _ambiguous(c, names):
        core += " " + CONTEXT_EN
    return core


def google_news(collector, c: dict, period: str, log=print) -> list[dict]:
    out = []
    for lang in c.get("langs", ["en"]):
        q = _query(c, lang)
        if not q or lang not in GN_LANG:
            continue
        hl, gl, ceid = GN_LANG[lang]
        url = f"https://news.google.com/rss/search?q={quote_plus(q + ' when:' + WHEN[period])}&hl={hl}&gl={gl}&ceid={ceid}"
        try:
            root = etree.fromstring(collector._get(url).content)
        except Exception as e:
            log(f"   ! Google News ({lang}): {str(e)[:100]}")
            continue
        items = root.findall(".//item")
        for it in items:
            title = (it.findtext("title") or "").strip()
            src = it.find("source")
            source = (src.text or "").strip() if src is not None else ""
            if source and title.endswith(" - " + source):
                title = title[: -len(source) - 3].strip()
            out.append({"title": title, "url": (it.findtext("link") or "").strip(), "published": (it.findtext("pubDate") or "").strip(),
                        "source": source, "summary": "", "provider": f"gnews-{lang}"})
        log(f"   Google News ({lang}): {len(items)}")
    return out


def gdelt(collector, c: dict, period: str, log=print) -> list[dict]:
    global _last_gdelt, _gdelt_fails
    if _gdelt_fails >= 2:
        return []
    parts = []
    for lang in c.get("langs", ["en"]):
        q = _query(c, lang)
        if q:
            parts.append(q if lang != "en" else q)
    if not parts:
        return []
    query = " OR ".join(f"({p})" if " " in p and not p.startswith("(") else p for p in parts)
    url = ("https://api.gdeltproject.org/api/v2/doc/doc?query=" + quote_plus(query) +
           f"&mode=artlist&format=json&maxrecords=75&sort=datedesc&timespan={GDELT_SPAN[period]}")
    for attempt in range(3):
        time.sleep(max(0.0, 6.0 - (time.time() - _last_gdelt)))  # лимит GDELT: 1 запрос в 5 секунд
        _last_gdelt = time.time()
        try:
            r = collector.routes["proxy"].get(url, timeout=(10, 40))
            if r.status_code == 429:
                time.sleep(10)
                continue
            r.raise_for_status()
            arts = json.loads(r.text).get("articles", [])
            _gdelt_fails = 0
            break
        except Exception as e:  # битый JSON у GDELT случается
            if attempt == 2:
                _gdelt_fails += 1
                log(f"   ! GDELT: {str(e)[:100]}")
                return []
    else:
        _gdelt_fails += 1
        log("   ! GDELT: лимит запросов (429) — пропускаю" + (", дальше отключён на этот запуск" if _gdelt_fails >= 2 else ""))
        return []
    out = []
    for a in arts:
        d = a.get("seendate", "")
        iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}T{d[9:11]}:{d[11:13]}:00+00:00" if re.fullmatch(r"\d{8}T\d{6}Z", d) else ""
        out.append({"title": (a.get("title") or "").strip(), "url": a.get("url", ""), "published": iso,
                    "source": a.get("domain", ""), "summary": "", "provider": "gdelt"})
    log(f"   GDELT: {len(out)}")
    return out


_newsapi_calls = 0
_newsapi_off = False
NEWSAPI_LANG = {"en": "en", "ru": "ru", "zh": "zh"}
NEWSAPI_DAILY_BUDGET = 90  # бесплатный тариф — 100 запросов в сутки; запас на ручные проверки


def newsapi(collector, c: dict, period: str, log=print) -> list[dict]:
    """NewsAPI.org (everything). Ключ — NEWSAPI_KEY в .env, передаётся заголовком, в адрес и логи не попадает."""
    import os
    from datetime import datetime, timedelta, timezone
    global _newsapi_calls, _newsapi_off
    key = os.environ.get("NEWSAPI_KEY")
    if not key or _newsapi_off:
        return []
    days = {"d": 2, "w": 7, "m": 30}[period]  # бесплатный тариф отдаёт с задержкой ~сутки и не старше месяца
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    out = []
    for lang in c.get("langs", ["en"]):
        q = _query(c, lang)
        if not q or lang not in NEWSAPI_LANG:
            continue
        if _newsapi_calls >= NEWSAPI_DAILY_BUDGET:
            log(f"   ! NewsAPI: достигнут лимит {NEWSAPI_DAILY_BUDGET} запросов за запуск")
            _newsapi_off = True
            break
        params = {"q": q[:480], "language": NEWSAPI_LANG[lang], "from": since, "sortBy": "publishedAt", "pageSize": 100}
        data = None
        for name in ("proxy", "direct"):
            try:
                r = collector.routes[name].get("https://newsapi.org/v2/everything", params=params,
                                               headers={"X-Api-Key": key}, timeout=(10, 40))
            except Exception:
                continue
            _newsapi_calls += 1
            if r.status_code == 429:
                log("   ! NewsAPI: суточный лимит исчерпан (429) — источник отключён до конца запуска")
                _newsapi_off = True
                return out
            try:
                data = r.json()
            except ValueError:
                continue
            break
        if not data:
            log(f"   ! NewsAPI ({lang}): нет ответа")
            continue
        if data.get("status") != "ok":
            log(f"   ! NewsAPI ({lang}): {data.get('code')}: {str(data.get('message'))[:120]}")
            if data.get("code") in ("apiKeyInvalid", "apiKeyDisabled", "apiKeyExhausted", "rateLimited"):
                _newsapi_off = True
                return out
            continue
        arts = data.get("articles", [])
        for a in arts:
            out.append({"title": (a.get("title") or "").strip(), "url": a.get("url", ""), "published": a.get("publishedAt") or "",
                        "source": (a.get("source") or {}).get("name") or "", "summary": (a.get("description") or "")[:400],
                        "provider": f"newsapi-{lang}"})
        log(f"   NewsAPI ({lang}): {len(arts)} из {data.get('totalResults', '?')}")
    return out


_gnewsio_off = False


def gnewsio(collector, c: dict, period: str, log=print) -> list[dict]:
    """GNews API (gnews.io). Ключ GNEWS_API_KEY; бесплатный тариф даёт мало запросов и не более 10 статей за запрос."""
    import os
    from datetime import datetime, timedelta, timezone
    global _gnewsio_off
    key = os.environ.get("GNEWS_API_KEY")
    if not key or _gnewsio_off:
        return []
    since = (datetime.now(timezone.utc) - timedelta(days={"d": 2, "w": 7, "m": 30}[period])).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = []
    for lang in c.get("langs", ["en"]):
        q = _query(c, lang)
        if not q or lang not in ("en", "ru", "zh"):
            continue
        params = {"q": q[:200], "lang": lang, "max": 10, "from": since, "sortby": "publishedAt", "apikey": key}
        data = None
        for name in ("proxy", "direct"):
            try:
                r = collector.routes[name].get("https://gnews.io/api/v4/search", params=params, timeout=(10, 40))
            except Exception:
                continue  # текст исключения может содержать адрес с ключом — не выводим
            if r.status_code in (401, 403):
                log("   ! GNews: ключ отклонён — источник отключён")
                _gnewsio_off = True
                return out
            if r.status_code == 429:
                log("   ! GNews: лимит запросов (429) — источник отключён до конца запуска")
                _gnewsio_off = True
                return out
            try:
                data = r.json()
            except ValueError:
                continue
            break
        if not data or "articles" not in data:
            err = (data or {}).get("errors")
            log(f"   ! GNews ({lang}): {str(err)[:100] if err else 'нет ответа'}")
            continue
        for a in data["articles"]:
            out.append({"title": (a.get("title") or "").strip(), "url": a.get("url", ""), "published": a.get("publishedAt") or "",
                        "source": (a.get("source") or {}).get("name") or "", "summary": (a.get("description") or "")[:400],
                        "provider": f"gnewsio-{lang}"})
        log(f"   GNews ({lang}): {len(data['articles'])} из {data.get('totalArticles', '?')}")
    return out


def norm_title(t: str) -> str:
    return re.sub(r"\W+", "", t.lower())[:80]
