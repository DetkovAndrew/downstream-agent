"""Локальный интерфейс агента: streamlit run ui.py  (или: python -m downstream_agent ui)."""
from __future__ import annotations

import os
import subprocess
import sys
import time

import streamlit as st

from downstream_agent import llm
from downstream_agent.collector import DATA, ROOT, load_config

st.set_page_config(page_title="Downstream Agent", page_icon="⛽", layout="wide")
CFG = load_config(ROOT / "companies.toml")
COMPANIES = CFG["companies"]
NAMES = {c["id"]: c["name"] for c in COMPANIES}
IDS = list(NAMES)


def run_cli(args: list[str], title: str) -> int:
    """Запускает команду и показывает вывод по мере появления."""
    st.write(f"**{title}**")
    box, lines = st.empty(), []
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen([sys.executable, "-m", "downstream_agent", *args], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    for line in proc.stdout:
        if "fonttools" in line.lower():
            continue
        lines.append(line.rstrip())
        box.code("\n".join(lines[-200:]), language=None)
    code = proc.wait()
    (st.success if code == 0 else st.error)(f"Готово (код {code})" if code == 0 else f"Завершилось с ошибкой (код {code})")
    return code


def flags(ids: list[str]) -> list[str]:
    return [x for i in ids for x in ("--company", i)]


def pick_companies(key: str, default_all=False) -> list[str]:
    return st.multiselect("Компании", IDS, default=IDS if default_all else [], format_func=lambda i: NAMES[i], key=key,
                          help="Пусто = все компании из списка")


@st.cache_data(ttl=5)
def stats() -> list[dict]:
    import json
    rows = []
    for c in COMPANIES:
        i = c["id"]
        ext = list((DATA / "extracted" / i).glob("*.json"))
        facts = sum(len(json.loads(f.read_text(encoding="utf-8")).get("facts", [])) for f in ext)
        rows.append({"Компания": c["name"], "Страна": c.get("country", ""),
                     "PDF": len(list((DATA / "raw" / i / "reports").glob("*.pdf"))),
                     "Новости": len(list((DATA / "raw" / i / "news").glob("*.json"))),
                     "Распознано": len(list((DATA / "text" / i).glob("*.jsonl"))),
                     "Обработано в таблицу": len(ext), "Фактов": facts})
    return rows


page = st.sidebar.radio("Раздел", ["Обзор", "1. Поиск документов", "2. Таблица Excel", "Вопрос агенту", "Настройки и ключи"])
st.sidebar.caption(f"Модель: {llm.gemini_models()[0] if llm.backend() == 'gemini' else llm.MODEL} · ключей: {len(llm.gemini_keys())}")

if page == "Обзор":
    st.title("Сравнение нефтяных компаний")
    st.caption("Агент ищет отчёты и новости, распознаёт PDF и собирает сводную таблицу показателей.")
    rows = stats()
    st.dataframe(rows, width='stretch', hide_index=True)
    c1, c2, c3 = st.columns(3)
    c1.metric("Документов (PDF)", sum(r["PDF"] for r in rows))
    c2.metric("Новостей", sum(r["Новости"] for r in rows))
    c3.metric("Фактов в таблице", sum(r["Фактов"] for r in rows))
    xl = DATA / "summary.xlsx"
    if xl.exists():
        st.download_button(f"Скачать summary.xlsx (обновлён {time.strftime('%d.%m %H:%M', time.localtime(xl.stat().st_mtime))})",
                           xl.read_bytes(), "summary.xlsx",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    else:
        st.info("Таблица ещё не собрана: раздел «2. Таблица Excel».")

elif page == "1. Поиск документов":
    st.title("Поиск документов в сети")
    ids = pick_companies("find")
    left, right = st.columns(2)
    with left:
        st.subheader("Отчёты и пресс-релизы")
        years = st.multiselect("Годы", [2023, 2024, 2025, 2026, 2027], default=[time.localtime().tm_year - 1, time.localtime().tm_year])
        types = st.multiselect("Типы", ["annual", "quarterly", "results", "release"], default=["annual", "quarterly"],
                               help="annual — годовой, quarterly — квартальный, results — финрезультаты, release — пресс-релиз")
        min_year = st.number_input("Не старше года", 2015, 2030, time.localtime().tm_year - 1)
        if st.button("Искать отчёты", type="primary", disabled=not (years and types)):
            run_cli(["discover", *flags(ids), "--years", *map(str, years), "--types", *types, "--min-year", str(int(min_year))],
                    "Поиск отчётов")
    with right:
        st.subheader("Новости")
        period = st.selectbox("За период", ["d", "w", "m"], index=2, format_func={"d": "день", "w": "неделю", "m": "месяц"}.get)
        if st.button("Искать новости"):
            run_cli(["news", *flags(ids), "--period", period], "Поиск новостей")

elif page == "2. Таблица Excel":
    st.title("Распознавание и сводная таблица")
    ids = pick_companies("sheet")
    st.subheader("Шаг 1. Распознать PDF")
    st.caption("Текст, таблицы и OCR для сканов. Делается один раз на файл.")
    if st.button("Распознать PDF"):
        run_cli(["recognize", *flags(ids)], "Распознавание")
    st.subheader("Шаг 2. Извлечь показатели и собрать Excel")
    c1, c2, c3 = st.columns(3)
    file_filter = c1.text_input("Только файлы, в имени которых есть…", "")
    max_pages = c2.number_input("Макс. страниц на файл (0 = все)", 0, 1000, 0, help="Для пробы поставьте 30–40: экономит лимиты.")
    year = c3.number_input("Только периоды года (0 = все)", 0, 2100, 0)
    o1, o2 = st.columns(2)
    force = o1.checkbox("Перезаполнить уже обработанные")
    all_pages = o2.checkbox("Не отбирать страницы по ключевым словам (дороже по лимитам)")
    a, b = st.columns(2)
    args = ["sheet", *flags(ids)]
    if file_filter:
        args += ["--file", file_filter]
    if year:
        args += ["--year", str(int(year))]
    if a.button("Извлечь и собрать Excel", type="primary"):
        ex = list(args)
        if max_pages:
            ex += ["--max-pages", str(int(max_pages))]
        if force:
            ex.append("--force")
        if all_pages:
            ex.append("--all-pages")
        run_cli(ex, "Извлечение через Gemini")
    if b.button("Только пересобрать Excel (без LLM)"):
        run_cli([*args, "--build-only"], "Сборка Excel")
    xl = DATA / "summary.xlsx"
    if xl.exists():
        st.download_button("Скачать summary.xlsx", xl.read_bytes(), "summary.xlsx",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

elif page == "Вопрос агенту":
    st.title("Вопрос агенту")
    st.caption("Агент сам выберет компании и страницы и ответит с проверенной цитатой.")
    q = st.text_input("Вопрос", placeholder="Какой ROACE был у Aramco за 2025 год?")
    pages = st.slider("Сколько страниц читать", 1, 6, 3)
    if st.button("Спросить", type="primary", disabled=not q):
        from downstream_agent.ask import ask
        with st.spinner("Думаю…"):
            try:
                res = ask(q, llm.MODEL, pages, log=lambda *_: None)
            except Exception as e:
                st.error(f"Ошибка: {e}")
                st.stop()
        st.markdown(f"### {res['answer']}")
        if res.get("evidence"):
            for e in res["evidence"]:
                st.info(f"«{e['quote']}»\n\n— {NAMES.get(e['company'], e['company'])}, {e['file']}, стр. {e['page']}")
        else:
            st.warning("Ответ без проверенной цитаты — не доверяйте.")

else:
    st.title("Настройки и ключи")
    st.write(f"Бэкенд: **{llm.backend()}** · основная модель: **{llm.gemini_models()[0]}** · запасные: {', '.join(llm.gemini_models()[1:]) or '—'}")
    st.write(f"Ключей загружено: **{len(llm.gemini_keys())}** (список в файле `.env`)")
    if st.button("Проверить все ключи (по 1 запросу на ключ)"):
        with st.spinner("Проверяю…"):
            res = llm.check_keys()
        st.dataframe([{"№": r["n"], "Ключ": f"…{r['tail']}", "Статус": r["status"], "Секунд": r["sec"]} for r in res],
                     hide_index=True, width='stretch')
    st.caption("Квота Gemini считается на проект и модель; ключи из одного проекта делят её.")
