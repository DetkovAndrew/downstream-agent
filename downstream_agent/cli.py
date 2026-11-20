import argparse
import time
from pathlib import Path

from downstream_agent.collector import ROOT, Collector, load_config
from downstream_agent import llm
from downstream_agent import newsheet, sheet
from downstream_agent.ask import ask
from downstream_agent.extract import extract_all
from downstream_agent.recognize import recognize_all
from downstream_agent.discover import DOC_TYPES, Discoverer, discover_news


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="downstream_agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect", help="скачать отчёты и новости в data/raw")
    c.add_argument("--config", type=Path, default=ROOT / "companies.toml")
    c.add_argument("--company", action="append", help="id компании (можно несколько)")
    c.add_argument("--browser", action="store_true", help="страницы без PDF-ссылок открывать в Chromium (Playwright)")
    c.add_argument("--no-news", action="store_true")
    c.add_argument("--no-reports", action="store_true")
    d = sub.add_parser("discover", help="агент сам ищет отчёты в интернете (LLM + поиск)")
    d.add_argument("--config", type=Path, default=ROOT / "companies.toml")
    d.add_argument("--company", action="append")
    d.add_argument("--years", type=int, nargs="+", default=[time.localtime().tm_year - 1, time.localtime().tm_year])
    d.add_argument("--min-year", type=int, help="старше этого года документы отбрасываются (по умолчанию: прошлый год)")
    d.add_argument("--types", nargs="+", default=["annual", "quarterly"], choices=list(DOC_TYPES))
    d.add_argument("--browser", action="store_true", help="если на странице нет PDF-ссылок — открыть её в Chromium (Playwright)")
    d.add_argument("--no-llm", action="store_true", help="без LLM, только эвристика")
    n = sub.add_parser("news", help="агент сам ищет свежие новости по компаниям")
    n.add_argument("--config", type=Path, default=ROOT / "companies.toml")
    n.add_argument("--company", action="append")
    n.add_argument("--period", choices=["d", "w", "m"], default="m", help="за день/неделю/месяц")
    n.add_argument("--sources", default="gnews,gdelt", help="через запятую: gnews (Google News), gnewsio (GNews API), newsapi, gdelt, ddg (ddg часто даёт таймауты)")
    n.add_argument("--cap", type=int, default=60, help="не больше новостей на компанию за запуск")
    n.add_argument("--fulltext", action="store_true", help="скачивать полный текст статей (медленно)")
    n.add_argument("--llm-filter", action="store_true", help="предварительный фильтр Gemini (тратит запросы)")
    q = sub.add_parser("ask", help="задать вопрос агенту по скачанным отчётам и новостям")
    q.add_argument("question")
    q.add_argument("--model", default=llm.MODEL)
    q.add_argument("--pages", type=int, default=3)
    k = sub.add_parser("keys", help="проверить все ключи Gemini (1 лёгкий запрос на ключ)")
    k.add_argument("--model", help="по умолчанию основная модель из .env")
    ns = sub.add_parser("newsheet", help="таблица новостей Excel (дата, компании, событие, источник) через Gemini")
    ns.add_argument("--company", action="append", help="id компании (папка новостей); можно несколько")
    ns.add_argument("--since", help="не раньше даты YYYY-MM-DD")
    ns.add_argument("--out", type=Path, default=ROOT / "data" / "news.xlsx")
    ns.add_argument("--force", action="store_true", help="переобработать уже разобранные новости")
    ns.add_argument("--build-only", action="store_true", help="не звать LLM, только пересобрать Excel")
    ns.add_argument("--downstream", action="store_true", help="только конкретные downstream-события (ввод, строительство, запуск...)")
    ns.add_argument("--no-dedupe", action="store_true", help="не склеивать дубли (не звать Gemini для группировки)")
    ns.add_argument("--dedupe-only", action="store_true", help="только склейка дублей и сборка Excel")
    ns.add_argument("--batch", type=int, default=8, help="новостей в одном запросе к Gemini")
    sub.add_parser("ui", help="запустить веб-интерфейс (откроется в браузере)")
    sub.add_parser("models", help="проверить ключ и показать доступные модели Gemini")
    t = sub.add_parser("sheet", help="вытянуть показатели из PDF (Gemini) и собрать сводную таблицу Excel")
    t.add_argument("--company", action="append", help="какие компании извлекать (можно несколько); в Excel попадает всё накопленное")
    t.add_argument("--file", action="append", help="только файлы, в имени которых есть эта подстрока")
    t.add_argument("--year", type=int, action="append", help="в таблицу только периоды с этим годом")
    t.add_argument("--out", type=Path, default=ROOT / "data" / "summary.xlsx")
    t.add_argument("--force", action="store_true", help="перезаполнить, даже если файл уже обработан")
    t.add_argument("--build-only", action="store_true", help="не звать LLM, только пересобрать Excel из уже извлечённого")
    t.add_argument("--no-extract", action="store_true", help="то же, что --build-only")
    t.add_argument("--strict", action="store_true", help="только дословные цитаты (без мягкой проверки числа рядом с подписью)")
    t.add_argument("--all-pages", action="store_true", help="не отбирать страницы по ключевым словам (дороже по лимитам)")
    t.add_argument("--pages-per-call", type=int, default=10)
    t.add_argument("--max-pages", type=int, help="не больше N страниц на файл (для пробы)")
    t.add_argument("--delay", type=float, default=3.0, help="пауза между запросами, сек (лимиты бесплатного тарифа)")
    t.add_argument("--with-targets", action="store_true", help="включить цели и прогнозы (по умолчанию только факты)")
    r = sub.add_parser("recognize", help="распознать скачанные PDF (текст, таблицы, OCR)")
    r.add_argument("--company", action="append")
    r.add_argument("--force", action="store_true")
    r.add_argument("--no-ocr", action="store_true")
    e = sub.add_parser("extract", help="достать ROACE и др. показатели локальным Qwen")
    e.add_argument("--company", action="append")
    e.add_argument("--model", default=llm.MODEL)
    e.add_argument("--llm", action="store_true", help="после правил добить Qwen (медленно, на CPU ~1 мин/страница)")
    e.add_argument("--force", action="store_true")
    e.add_argument("--pages", type=int, default=10, help="сколько самых релевантных страниц на файл")
    args = p.parse_args(argv)

    if args.cmd == "sheet":
        if not (args.build_only or args.no_extract):
            n = sheet.extract_all(args.company, args.file, args.force, pages_per_call=args.pages_per_call,
                                  prefilter=not args.all_pages, delay=args.delay, max_pages=args.max_pages,
                                  loose=not args.strict)
            print(f"\nНовых фактов извлечено: {n}")
        sheet.build_excel(args.out, None, args.year, include_targets=args.with_targets)  # в таблице всё накопленное; --company ограничивает только извлечение
        return 0
    if args.cmd == "newsheet":
        newsheet.set_mode("downstream" if args.downstream else "all")
        since = newsheet.parse_date(args.since)
        if not args.build_only and not args.dedupe_only:
            print(f"Обработано новостей: {newsheet.extract_events(args.company, since, args.force, args.batch)}")
        if not args.build_only and not args.no_dedupe:
            print(f"Компаний с обновлённой склейкой: {newsheet.dedupe_events(args.company, since, args.force)}")
        newsheet.build_excel(args.out, args.company, since)
        return 0
    if args.cmd == "ui":
        import subprocess, sys
        return subprocess.call([sys.executable, "-m", "streamlit", "run", str(ROOT / "ui.py"),
                                "--server.address", "127.0.0.1", "--server.headless", "true",
                                "--browser.gatherUsageStats", "false"], cwd=ROOT)
    if args.cmd == "keys":
        rows = llm.check_keys(args.model)
        print(f"Модель: {rows[0]['model'] if rows else args.model or '—'}; ключей: {len(rows)}")
        for r in rows:
            print(f"  #{r['n']} …{r['tail']}  {r['status']}  ({r['sec']} с)")
        ok = sum(r["status"] == "OK" for r in rows)
        print(f"Рабочих: {ok} из {len(rows)}")
        if llm.yandex_ready():
            print(f"YandexGPT: {llm.check_yandex()}")
        return 0 if ok else 1
    if args.cmd == "models":
        print(f"Бэкенд: {llm.backend()}, модель: {llm.gemini_model() if llm.backend() == 'gemini' else llm.MODEL}")
        if llm.gemini_key():
            print("\n".join(llm.list_models()))
        else:
            print("GEMINI_API_KEY не задан: создайте ключ на https://aistudio.google.com/apikey и положите в .env")
        return 0
    if args.cmd == "ask":
        res = ask(args.question, args.model, args.pages)
        print(f"\nОтвет: {res['answer']}")
        for e in res["evidence"]:
            print(f"  [{e['company']} / {e['file'][:40]} / стр. {e['page']}] «{e['quote']}»")
        if not res["evidence"]:
            print("  (ответ без проверенной цитаты — не доверяйте)")
        return 0
    if args.cmd == "news":
        reps = discover_news(Collector(load_config(args.config)), args.company, args.period, use_llm=args.llm_filter,
                              sources=tuple(args.sources.split(',')), fulltext=args.fulltext, cap=args.cap)
        print(f"\nНовостей сохранено: {sum(len(r.downloaded) for r in reps)}")
        return 0
    if args.cmd == "recognize":
        print(f"\nРаспознано файлов: {recognize_all(args.company, args.force, ocr=not args.no_ocr)}")
        return 0
    if args.cmd == "extract":
        print(f"\nИзвлечено фактов: {extract_all(args.company, args.model, args.pages, use_llm=args.llm, force=args.force)}")
        return 0

    if args.cmd == "discover":
        col = Collector(load_config(args.config))
        reps = Discoverer(col, use_llm=not args.no_llm, min_year=args.min_year, browser=args.browser).run(args.years, args.types, args.company)
        print(f"\nИтого скачано: {sum(len(r.downloaded) for r in reps)}")
        return 0

    collector = Collector(load_config(args.config))
    collector.browser = args.browser
    reports = collector.run(
        args.company, reports=not args.no_reports, news=not args.no_news)
    total = sum(len(r.downloaded) for r in reports)
    errors = sum(len(r.errors) for r in reports)
    print(f"\nИтого скачано: {total}, ошибок: {errors}. Папка: {ROOT / 'data' / 'raw'}")
    return 0 if total or not errors else 1
