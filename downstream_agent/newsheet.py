"""Таблица новостей Excel: лист «Все новости» (Дата, Компании, Событие, Источник) и «Сводная» (фильтр по датам и компании).

Gemini читает сохранённые новости (data/raw/*/news/*.json) пачками, выделяет событие и упомянутые компании
из нашего списка. Результат кэшируется в data/news_events/, чтобы не тратить запросы повторно.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

from downstream_agent import llm
from downstream_agent.collector import DATA, ROOT, load_config

EVENTS_DIR = DATA / "news_events"
CLUSTERS_DIR = DATA / "news_clusters"
MODE = "all"

EVENT_TYPES = ["Ввод в эксплуатацию", "Завершение строительства", "Начало строительства", "Модернизация / расширение",
                "Инвестиционное решение", "Сделка (покупка/продажа актива)", "Контракт / поставка оборудования",
                "Остановка / авария / ремонт", "Запуск продукта / сервиса", "Прочее"]
SEGMENTS = ["НПЗ / нефтепереработка", "Нефтехимия", "Розница (АЗС)", "Смазочные материалы", "Газопереработка", "Логистика и хранение",
            "Прочее downstream"]

DOWNSTREAM_SYSTEM = (
    "You process news items for a DOWNSTREAM competitor-monitoring table of oil companies. DOWNSTREAM means: oil refining, "
    "petrochemicals, fuel retail (gas stations), lubricants, gas processing plants, product logistics/storage. "
    "For EACH item return relevant=true ONLY if the item reports a CONCRETE downstream business event: construction started or "
    "completed, a unit/plant/station commissioned or launched, modernization or capacity expansion, an investment decision, "
    "an acquisition/sale/JV of a downstream asset, an equipment/engineering contract, a refinery outage/fire/shutdown/repair, "
    "a new product or service launch. Set relevant=false for: statements, interviews or opinions of officials/executives, politics, "
    "sanctions commentary, prices/markets/stock-price news, analyst ratings, upstream (exploration, production, reserves), "
    "macro news, and vague plans without a concrete project. "
    "Also return: companies (names EXACTLY as in the provided list, parties to the event), event (ONE factual sentence in Russian: "
    "who built/launched/started what, where, key numbers; only facts from the text), type (EXACTLY one of: "
    + "; ".join(EVENT_TYPES) + "), segment (EXACTLY one of: " + "; ".join(SEGMENTS) + "), date (YYYY-MM-DD of the event if stated, else empty). "
    "Reply JSON: {\"items\": [{\"id\": number, \"relevant\": bool, \"companies\": [str], \"event\": str, \"type\": str, "
    "\"segment\": str, \"date\": str}]}."
)


def set_mode(mode: str) -> None:
    """mode: 'all' (все значимые события) или 'downstream' (только строительство/ввод/запуск и т.п. в downstream; отдельный кэш)."""
    global EVENTS_DIR, CLUSTERS_DIR, MODE, SYSTEM
    MODE = mode
    if mode == "downstream":
        EVENTS_DIR, CLUSTERS_DIR, SYSTEM = DATA / "news_events_ds", DATA / "news_clusters_ds", DOWNSTREAM_SYSTEM
    else:
        EVENTS_DIR, CLUSTERS_DIR, SYSTEM = DATA / "news_events", DATA / "news_clusters", _ALL_SYSTEM


SYSTEM = (
    "You process news items about oil companies for a competitor-monitoring table. For EACH item return: "
    "relevant (false only for clear junk: stock-price targets, analyst ratings, ads, unrelated pages), "
    "companies (list of names EXACTLY as in the provided company list that the event is about; include every listed "
    "company that is a party to the event, not companies that are merely mentioned in passing), "
    "event (ONE factual sentence in Russian: what happened, with key numbers/names; only facts stated in the text, "
    "no opinions, no guesses), date (YYYY-MM-DD of the event if the text states it, else empty). "
    "Reply JSON: {\"items\": [{\"id\": number, \"relevant\": bool, \"companies\": [str], \"event\": str, \"date\": str}]}."
)


_ALL_SYSTEM = SYSTEM


def _names() -> dict[str, str]:
    return {c["id"]: c["name"] for c in load_config(ROOT / "companies.toml")["companies"]}


def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    s = str(s).strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(s).date()
    except (TypeError, ValueError):
        m = re.search(r"(20\d\d)-(\d\d)-(\d\d)", s)
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _load_items(companies: list[str] | None, since: date | None) -> list[dict]:
    items = []
    for f in sorted((DATA / "raw").glob("*/news/*.json")):
        cid = f.parent.parent.name
        if companies and cid not in companies:
            continue
        try:
            n = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        d = parse_date(n.get("published"))
        if since and (not d or d < since):
            continue
        body = re.sub(r"\s+", " ", n.get("text") or n.get("summary") or "")[:1500]
        items.append({"file": f, "cid": cid, "date": d, "title": n.get("title") or "", "url": n.get("url") or "",
                      "source": n.get("source") or urlparse(n.get("url") or "").netloc.removeprefix("www."), "body": body})
    return items


def _cache_path(it: dict) -> Path:
    return EVENTS_DIR / it["cid"] / it["file"].name


def build_archive(out: Path, log=print) -> Path:
    """Сохранить все найденные публикации до отбора по тематике."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    names = _names()
    items = _load_items(None, None)
    items.sort(key=lambda it: (it["date"] is None, -(it["date"].toordinal() if it["date"] else 0)))
    wb = Workbook()
    ws = wb.active
    ws.title = "Все найденные новости"
    ws.append(["Дата публикации", "Компания", "Заголовок", "Источник", "Ссылка", "Описание"])
    for row, it in enumerate(items, start=2):
        ws.append([it["date"], names.get(it["cid"], it["cid"]), it["title"], it["source"], it["url"], it["body"]])
        ws.cell(row, 1).number_format = "DD.MM.YYYY"
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="DDEBF7")
    for col, width in zip("ABCDEF", (18, 25, 70, 25, 60, 80)):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    wb.save(tmp)
    tmp.replace(out)
    log(f"Архив: {out} — публикаций {len(items)}")
    return out


def extract_events(companies=None, since: date | None = None, force=False, batch=8, delay=3.0, log=print) -> int:
    names = _names()
    todo = [it for it in _load_items(companies, since) if force or not _cache_path(it).exists()]
    log(f"Новостей к обработке: {len(todo)}")
    done = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        text = "\n\n".join(f"[{k}] ({it['date'] or 'дата неизвестна'}) {it['title']}\n{it['body']}" for k, it in enumerate(chunk))
        try:
            res = llm.ask_json(SYSTEM, f"Company list: {list(names.values())}\n\nItems:\n{text}", timeout=300)
        except llm.LlmUnavailable as e:
            log(f"   ! пачка {i // batch + 1}: {str(e)[:150]} — не сохранено, повторите запуск")
            continue
        got = {int(r["id"]): r for r in res.get("items", []) if str(r.get("id", "")).isdigit()}
        for k, it in enumerate(chunk):
            r = got.get(k)
            if not r:
                continue  # модель пропустила новость — обработается при следующем запуске
            comps = [c for c in r.get("companies", []) if c in names.values()] or [names.get(it["cid"], it["cid"])]
            d = it["date"] or parse_date(r.get("date"))
            out = _cache_path(it)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps({"relevant": bool(r.get("relevant", True)), "companies": comps,
                                       "event": str(r.get("event", "")).strip(), "date": d.isoformat() if d else "",
                                       "type": str(r.get("type", "")).strip(), "segment": str(r.get("segment", "")).strip(),
                                       "title": it["title"], "url": it["url"], "source": it["source"]},
                                      ensure_ascii=False, indent=1), encoding="utf-8")
            done += 1
        log(f"   пачка {i // batch + 1}: обработано {len(got)} из {len(chunk)}")
        time.sleep(delay)
    return done


CLUSTER_SYSTEM = (
    "You group news items that describe the SAME real-world event reported by different outlets "
    "(same announcement/deal/incident/results release). Items about different developments, different dates of a "
    "continuing story, or merely the same topic are NOT the same event. Reply JSON: {\"groups\": [[ids], ...]} "
    "listing ONLY groups with 2 or more items; every id at most once."
)


def _all_events(companies=None, since=None) -> list[dict]:
    rows = []
    for f in sorted(EVENTS_DIR.glob("*/*.json")):
        r = json.loads(f.read_text(encoding="utf-8"))
        if not r["relevant"] or not r["event"]:
            continue
        d = parse_date(r["date"])
        if since and (not d or d < since):
            continue
        if companies and f.parent.name not in companies:
            continue
        rows.append({**r, "d": d, "id": f"{f.parent.name}/{f.name}"})
    return rows


def dedupe_events(companies=None, since=None, force=False, delay=3.0, chunk=60, log=print) -> int:
    """Gemini группирует дубли по каждой компании (событие может попасть в списки нескольких компаний).

    Кэш: data/news_clusters/<компания>.json (пересчитывается, если набор событий компании изменился).
    """
    import hashlib
    events = _all_events(companies, since)
    by_company: dict[str, list[dict]] = {}
    for e in events:
        for c in e["companies"]:
            by_company.setdefault(c, []).append(e)
    done = 0
    out_dir = CLUSTERS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    for company, items in sorted(by_company.items()):
        items.sort(key=lambda e: (e["d"] is None, e["d"] or date.min))
        key = hashlib.sha1("|".join(sorted(e["id"] for e in items)).encode()).hexdigest()
        path = out_dir / (re.sub(r"\W+", "_", company) + ".json")
        if not force and path.exists() and json.loads(path.read_text(encoding="utf-8")).get("key") == key:
            continue
        groups = []
        failed = False
        for i in range(0, len(items), chunk):
            part = items[i:i + chunk]
            if len(part) < 2:
                continue
            text = "\n".join(f"[{k}] {e['d'] or '?'} ({e['source'][:20]}) {e['event']}" for k, e in enumerate(part))
            try:
                res = llm.ask_json(CLUSTER_SYSTEM, f"Company: {company}\n\n{text}", timeout=300)
            except llm.LlmUnavailable as e:
                log(f"   ! {company}: {str(e)[:120]} — не сохранено, повторите запуск")
                failed = True
                break
            for g in res.get("groups", []):
                ids = [part[int(k)]["id"] for k in g if str(k).isdigit() and int(k) < len(part)]
                if len(set(ids)) >= 2:
                    groups.append(sorted(set(ids)))
            time.sleep(delay)
        if failed:
            continue
        path.write_text(json.dumps({"key": key, "groups": groups}, ensure_ascii=False, indent=1), encoding="utf-8")
        merged = sum(len(g) - 1 for g in groups)
        log(f"   {company}: событий {len(items)}, групп дублей {len(groups)}, склеено {merged}")
        done += 1
    return done


def merge_duplicates(rows: list[dict]) -> list[dict]:
    """Склеивает события по сохранённым группам (union-find); без кэша групп возвращает как есть."""
    parent = {r["id"]: r["id"] for r in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for f in CLUSTERS_DIR.glob("*.json") if CLUSTERS_DIR.exists() else []:
        for g in json.loads(f.read_text(encoding="utf-8")).get("groups", []):
            ids = [i for i in g if i in parent]
            for other in ids[1:]:
                parent[find(other)] = find(ids[0])
    clusters: dict[str, list[dict]] = {}
    for r in rows:
        clusters.setdefault(find(r["id"]), []).append(r)
    merged = []
    for members in clusters.values():
        members.sort(key=lambda r: (r["d"] is None, r["d"] or date.min))
        best = max(members, key=lambda r: (bool(re.search(r"\d", r["event"])), min(len(r["event"]), 260)))
        names = list(dict.fromkeys(m["source"] for m in members if m["source"]))
        urls = list(dict.fromkeys(m["url"] for m in members if m["url"]))
        merged.append({"type": best.get("type", ""), "segment": best.get("segment", ""),
                       "d": members[0]["d"], "companies": list(dict.fromkeys(c for m in members for c in m["companies"])),
                       "event": best["event"], "sources": names, "urls": urls, "n": len(urls)})
    return merged


def build_excel(out: Path, companies=None, since: date | None = None, log=print, item_ids: set[str] | None = None) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation

    raw = _all_events(companies, since)
    if item_ids is not None:
        raw = [row for row in raw if row["id"] in item_ids]
    rows = merge_duplicates(raw)
    if not rows:
        log("! Событий за выбранные условия нет — создаётся пустая таблица.")
    rows.sort(key=lambda r: (r["d"] is None, -(r["d"].toordinal() if r["d"] else 0)))
    n = len(rows)
    all_names = sorted({c for r in rows for c in r["companies"]})
    all_types = sorted({r["type"] for r in rows if r.get("type")})
    dates = [r["d"] for r in rows if r["d"]]

    def src_label(r):
        s = r["sources"][:4]
        return ", ".join(s) + (f" и ещё {len(r['sources']) - 4}" if len(r["sources"]) > 4 else "")

    # колонки листа «Все новости»: A Дата, B Компании, C Событие, D Тип, E Сегмент, F Источники, G Источников, H Ссылки;
    # служебные (скрыты): I ключ компаний, J № в сводной, K первая ссылка, L список компаний, M список типов
    wb = Workbook()
    ws = wb.active
    ws.title = "Все новости"
    ws.append(["Дата", "Компании", "Событие", "Тип события", "Сегмент", "Источники", "Источников", "Ссылки",
               "Ключ компаний", "№ в сводной", "Первая ссылка", "Список компаний", "Список типов"])
    for r in rows:
        ws.append([r["d"], "; ".join(r["companies"]), r["event"], r.get("type") or "—", r.get("segment") or "—",
                   src_label(r), len(r["sources"]) or 1, "\n".join(r["urls"]), None, None, r["urls"][0] if r["urls"] else ""])
    SV = "'Сводная'"
    for i in range(2, n + 2):
        ws.cell(row=i, column=1).number_format = "DD.MM.YYYY"
        for col in (2, 3, 4, 5, 6, 8):
            ws.cell(row=i, column=col).alignment = Alignment(wrap_text=True, vertical="top")
        c = ws.cell(row=i, column=6)
        if ws.cell(row=i, column=11).value:
            c.hyperlink = ws.cell(row=i, column=11).value
            c.font = Font(color="0563C1", underline="single")
        ws.cell(row=i, column=9, value=f'="; "&B{i}&";"')
        ws.cell(row=i, column=10, value=(
            f'=IF(AND(ISNUMBER(A{i}),A{i}>={SV}!$B$2,A{i}<={SV}!$B$3,'
            f'OR({SV}!$B$4="Все",ISNUMBER(SEARCH("; "&{SV}!$B$4&";",I{i}))),'
            f'OR({SV}!$B$5="Все",D{i}={SV}!$B$5)),COUNT(J$1:J{i - 1})+1,"")'))
    for col, w in zip("ABCDEFGH", (12, 26, 80, 24, 22, 30, 11, 46)):
        ws.column_dimensions[col].width = w
    for col in "IJKLM":
        ws.column_dimensions[col].hidden = True
    for c in ws[1][:8]:
        c.font, c.fill = Font(bold=True), PatternFill("solid", fgColor="DDEBF7")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:H{n + 1}"
    for k, name in enumerate(["Все"] + all_names):
        ws.cell(row=2 + k, column=12, value=name)
    for k, name in enumerate(["Все"] + all_types):
        ws.cell(row=2 + k, column=13, value=name)

    sv = wb.create_sheet("Сводная")
    inp = PatternFill("solid", fgColor="FFF2CC")
    sv["A1"] = "Новости за период (дубли из разных источников склеены)"
    sv["A1"].font = Font(bold=True, size=12)
    for r, lab in ((2, "Дата от"), (3, "Дата до"), (4, "Компания"), (5, "Тип события")):
        sv.cell(row=r, column=1, value=lab).font = Font(bold=True)
        sv.cell(row=r, column=2).fill = inp
    sv["B2"], sv["B3"], sv["B4"], sv["B5"] = (min(dates) if dates else None), (max(dates) if dates else None), "Все", "Все"
    for c in ("B2", "B3"):
        sv[c].number_format = "DD.MM.YYYY"
    sv["A6"], sv["B6"] = "Найдено", f"=COUNT('Все новости'!J2:J{n + 1})" if n else 0
    dv = DataValidation(type="list", formula1=f"='Все новости'!$L$2:$L${len(all_names) + 2}", allow_blank=False)
    dt = DataValidation(type="list", formula1=f"='Все новости'!$M$2:$M${len(all_types) + 2}", allow_blank=False)
    dd = DataValidation(type="date", operator="greaterThan", formula1="36526", allow_blank=False)
    for v in (dv, dt, dd):
        sv.add_data_validation(v)
    dv.add("B4")
    dt.add("B5")
    dd.add("B2:B3")
    sv.append([])
    sv.append(["Дата", "Компании", "Событие", "Тип события", "Сегмент", "Источники", "Источников", "Ссылки"])
    for c in sv[8]:
        c.font, c.fill = Font(bold=True), PatternFill("solid", fgColor="DDEBF7")
    for k in range(1, n + 1):
        r = 8 + k
        m = f"MATCH({k},'Все новости'!$J$1:$J${n + 1},0)"

        def pick(col):
            return f"INDEX('Все новости'!${col}$1:${col}${n + 1},{m})"
        sv.cell(row=r, column=1, value=f'=IFERROR({pick("A")},"")').number_format = "DD.MM.YYYY"
        for col_idx, src in ((2, "B"), (3, "C"), (4, "D"), (5, "E"), (7, "G"), (8, "H")):
            sv.cell(row=r, column=col_idx, value=f'=IFERROR({pick(src)},"")')
        sv.cell(row=r, column=6, value=f'=IFERROR(HYPERLINK({pick("K")},{pick("F")}),"")')
        sv.cell(row=r, column=6).font = Font(color="0563C1", underline="single")
        for c in (2, 3, 4, 5, 6, 8):
            sv.cell(row=r, column=c).alignment = Alignment(wrap_text=True, vertical="top")
    for col, w in zip("ABCDEFGH", (14, 26, 80, 24, 22, 30, 11, 46)):
        sv.column_dimensions[col].width = w
    sv.freeze_panes = "A9"
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    multi = sum(1 for r in rows if len(r["sources"]) > 1)
    log(f"Excel: {out} — событий {n} (из {len(raw)} новостей; склеено дублей: {len(raw) - n}, "
        f"с несколькими источниками: {multi}), компаний {len(all_names)}")
    return out
