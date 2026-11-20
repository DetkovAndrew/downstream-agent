"""Локальное приложение: HTTP-сервер + веб-интерфейс (откроется в браузере). Запускается как .exe или `python -m downstream_agent.app`.

Без внешних зависимостей (только стандартная библиотека + модули агента). Все данные — в папке рядом с программой.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import shutil
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from datetime import date, timedelta
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

if sys.stdout is None:  # .exe без консоли
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

from downstream_agent import llm, newsheet, recognize, sheet  # noqa: E402
from downstream_agent.collector import DATA, ROOT, Collector, load_config  # noqa: E402
from downstream_agent.paths import resource_dir  # noqa: E402

PORT = int(os.environ.get("AGENT_PORT", "8765"))  # другой порт — для отдельной копии/тестов, чтобы не задеть рабочую
KEYS = [("GEMINI_API_KEYS", "Gemini API (несколько ключей через запятую; нужен прокси/VPN)"),
        ("GIGACHAT_AUTH_KEY", "GigaChat — ключ авторизации (Base64)"),
        ("YANDEX_API_KEY", "Яндекс AI Studio — API-ключ"), ("YANDEX_FOLDER_ID", "Яндекс AI Studio — ID каталога"),
        ("DEEPSEEK_API_KEY", "DeepSeek API-ключ"), ("NEWSAPI_KEY", "NewsAPI.org ключ"), ("GNEWS_API_KEY", "GNews API ключ")]
FILES = {"news": ("news_downstream.xlsx", "Новости downstream"), "archive": ("news_archive.xlsx", "Все найденные новости"), "reports": ("summary.xlsx", "Показатели из отчётов"), "assertions": ("downstream_assertions.xlsx", "Выводы по downstream")}
JOB_LOCK = threading.Lock()
JOBS: dict[str, "Job"] = {}


HOST = os.environ.get("AGENT_HOST", "127.0.0.1")  # на сервере: 0.0.0.0 (или адрес внутри сети)
PASSWORD = os.environ.get("AGENT_PASSWORD", "")  # пусто = вход не нужен (локальный режим, как в .exe)
COOKIE = "da_session"
_fails: dict[str, list[float]] = {}  # ip -> времена неудачных попыток (защита от подбора)

LOGIN_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Downstream Agent — вход</title><style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0b0f14;color:#e6edf3;font-family:-apple-system,"Segoe UI",Arial,sans-serif}
form{background:#131820;border:1px solid #30363d;border-radius:10px;padding:28px;width:320px}
h1{font-size:18px;margin:0 0 4px}p{color:#8b949e;margin:0 0 18px;font-size:13px}
input{width:100%;box-sizing:border-box;padding:10px;border-radius:6px;border:1px solid #30363d;background:#1c2530;color:#e6edf3;font:inherit;margin-bottom:12px}
button{width:100%;padding:10px;border-radius:6px;border:1px solid #2ea043;background:#238636;color:#fff;font:inherit;cursor:pointer}
.err{color:#f85149;font-size:13px;margin-bottom:10px}</style></head><body>
<form method="post" action="/login"><h1>Downstream Agent</h1><p>Введите пароль для входа</p>
%ERR%<input type="password" name="password" placeholder="Пароль" autofocus autocomplete="current-password"><button type="submit">Войти</button></form></body></html>"""


def _session_token() -> str:
    return hmac.new(PASSWORD.encode(), b"downstream-agent-session", hashlib.sha256).hexdigest()


class Cancelled(Exception):
    pass


class Job:
    def __init__(self, kind: str):
        self.id, self.kind = uuid.uuid4().hex[:8], kind
        self.lines: list[str] = []
        self.status = "running"
        self.cancel = False
        self.started = time.time()
        self.outputs: list[str] = []

    def log(self, *a) -> None:
        if self.cancel:
            raise Cancelled()
        self.lines.append(" ".join(str(x) for x in a))


def config_path() -> Path:
    p = ROOT / "companies.toml"
    if not p.exists():
        src = resource_dir() / "companies.toml"
        if src.exists():
            shutil.copy(src, p)
    return p


def companies() -> list[dict]:
    return load_config(config_path())["companies"]


def count(pattern: str) -> int:
    return sum(1 for _ in DATA.glob(pattern))


def state() -> dict:
    cur_backend, cur_model = llm.current()
    models = [{"value": f"{b}|{m}", "label": lab, "ready": llm.configured(b), "vpn": vpn, "backend": b}
              for b, m, lab, vpn in llm.CATALOG]
    files = {}
    for key, (name, label) in FILES.items():
        f = DATA / name
        files[key] = {"name": name, "label": label, "exists": f.exists(), "size": f.stat().st_size if f.exists() else 0,
                      "mtime": time.strftime("%d.%m.%Y %H:%M", time.localtime(f.stat().st_mtime)) if f.exists() else ""}
    comps = [{"id": c["id"], "name": c["name"], "country": c.get("country", ""),
              "pdf": count(f"raw/{c['id']}/reports/*.pdf"), "news": count(f"raw/{c['id']}/news/*.json"),
              "recognized": count(f"text/{c['id']}/*.jsonl"), "extracted": count(f"extracted/{c['id']}/*.json")} for c in companies()]
    busy = next((j.id for j in JOBS.values() if j.status == "running"), None)
    return {"companies": comps, "models": models, "current": f"{cur_backend}|{cur_model}", "files": files, "busy": busy,
            "types": newsheet.EVENT_TYPES, "news_sources": os.environ.get("AGENT_NEWS_SOURCES", "gnews").split(","), "frozen": bool(getattr(sys, "frozen", False)), "data_dir": str(DATA)}


def keys_state() -> list[dict]:
    out = []
    for name, label in KEYS:
        v = os.environ.get(name, "")
        out.append({"name": name, "label": label, "set": bool(v), "tail": ("…" + v[-4:]) if len(v) > 8 else ""})
    return out


def save_keys(values: dict) -> None:
    env = ROOT / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    for name, val in values.items():
        if name not in {k for k, _ in KEYS} or not str(val).strip():
            continue
        val = str(val).strip()
        os.environ[name] = val
        for i, ln in enumerate(lines):
            if ln.startswith(name + "="):
                lines[i] = f"{name}={val}"
                break
        else:
            lines.append(f"{name}={val}")
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(env, 0o600)
    except OSError:
        pass


# ------------------------------------------------------------------ задачи
def run_job(job: Job, fn, params: dict) -> None:
    def target():
        try:
            fn(job, params)
            job.status = "done"
            job.lines.append("✔ Готово")
        except Cancelled:
            job.status = "cancelled"
            job.lines.append("■ Остановлено пользователем")
        except Exception as e:  # noqa: BLE001
            job.status = "error"
            job.lines.append(f"✘ Ошибка: {e}")
            job.lines.append(traceback.format_exc(limit=3))
        finally:
            JOB_LOCK.release()
    threading.Thread(target=target, daemon=True).start()


def job_news(job: Job, p: dict) -> None:
    from downstream_agent.discover import discover_news
    ids = p.get("companies") or [c["id"] for c in companies()]
    period = p.get("period", "w")
    days = {"d": 1, "w": 7, "m": 30}[period]
    job.log(f"Модель: {llm.current()[1]} ({llm.current()[0]}). Компаний: {len(ids)}, период: {period}")
    newsheet.set_mode("downstream")
    job.log("— Шаг 1/4: поиск новостей")
    col = Collector(load_config(config_path()), log=job.log)
    try:
        discover_news(col, ids, period, use_llm=False, log=job.log, sources=tuple(p.get("sources") or ["gnews"]),
                      cap=int(p.get("cap") or 60))
    finally:
        try:
            newsheet.build_archive(DATA / FILES["archive"][0], log=job.lines.append)
        except Exception as e:
            job.log(f"! Не удалось обновить архив новостей: {e}")
    job.log("— Шаг 2/4: выделение downstream-событий (конкретные проекты: построили, ввели, начали)")
    since = date.today() - timedelta(days=days - 1)
    all_period = bool(p.get("all"))
    new_ids = {f"{it['cid']}/{it['file'].name}" for it in newsheet._load_items(ids, since)
               if not newsheet._cache_path(it).exists()}
    force = bool(p.get("force")) and all_period
    job.log("   режим: все новости за период" if all_period else f"   режим: ещё не разобранные новости за период ({len(new_ids)})")
    if force:
        job.log("   (режим «разобрать заново»: кэш прежних разборов игнорируется)")
    n = newsheet.extract_events(ids, since, force, 8, 2.0, job.log)
    job.log(f"   обработано новостей: {n}")
    job.log("— Шаг 3/4: склейка дублей из разных источников")
    newsheet.dedupe_events(ids, since, force, 2.0, 60, job.log)
    job.log("— Шаг 4/4: запись в Excel")
    out = DATA / FILES["news"][0]
    job.log(f"   в таблице выбранные компании ({len(ids)}) за выбранный период")
    newsheet.build_excel(out, ids, since, job.log, item_ids=None if all_period else new_ids)
    job.outputs.append("news")


def job_find_reports(job: Job, p: dict) -> None:
    from downstream_agent.discover import Discoverer
    ids = p.get("companies") or [c["id"] for c in companies()]
    years = [int(y) for y in p.get("years") or [date.today().year - 1, date.today().year]]
    types = p.get("types") or ["annual", "quarterly"]
    job.log(f"Поиск отчётов: компаний {len(ids)}, годы {years}, типы {types}")
    col = Collector(load_config(config_path()), log=job.log)
    Discoverer(col, use_llm=True, log=job.log, min_year=min(years), browser=True).run(years, types, ids)


def job_extract_reports(job: Job, p: dict) -> None:
    ids = p.get("companies") or None
    job.log(f"Модель: {llm.current()[1]} ({llm.current()[0]})")
    job.log("— Шаг 1/3: распознавание PDF (текст, таблицы, OCR сканов)")
    n = recognize.recognize_all(ids, log=job.log)
    job.log(f"   распознано файлов: {n}")
    job.log("— Шаг 2/3: извлечение показателей")
    per_call = 3
    facts = sheet.extract_all(ids, [p["file"]] if p.get("file") else None, bool(p.get("force")), log=job.log,
                              pages_per_call=per_call, prefilter=True, delay=1.0 if llm.current()[0] != "gemini" else 3.0,
                              max_pages=int(p["max_pages"]) if p.get("max_pages") else None, loose=not p.get("strict"))
    job.log(f"   новых фактов: {facts}")
    job.log("— Шаг 3/3: запись в Excel")
    sheet.build_excel(DATA / FILES["reports"][0], None, None, log=job.log)
    job.outputs.append("reports")


def job_assertions(job: Job, p: dict) -> None:
    from downstream_agent import assertions
    ids = p.get("companies") or None
    job.log("— Распознавание новых PDF при необходимости")
    recognize.recognize_all(ids, log=job.log)
    names = {c["id"]: c["name"] for c in companies()}
    rows = assertions.extract(names, ids, log=job.log, delay=1.0 if llm.current()[0] != "gemini" else 2.0)
    out = DATA / FILES["assertions"][0]
    assertions.build_excel(rows, out)
    job.log(f"Готово: утверждений {len(rows)}. Файл: {out}")
    job.outputs.append("assertions")


JOB_KINDS = {"news": job_news, "find_reports": job_find_reports, "extract_reports": job_extract_reports, "assertions": job_assertions}


def news_preview(n: int = 30) -> list[dict]:
    newsheet.set_mode("downstream")
    rows = newsheet.merge_duplicates(newsheet._all_events(None, None))
    rows.sort(key=lambda r: (r["d"] is None, -(r["d"].toordinal() if r["d"] else 0)))
    return [{"date": r["d"].strftime("%d.%m.%Y") if r["d"] else "", "companies": "; ".join(r["companies"]), "event": r["event"],
             "type": r.get("type", ""), "segment": r.get("segment", ""), "sources": len(r["sources"]) or 1,
             "url": r["urls"][0] if r["urls"] else ""} for r in rows[:n]]


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "DownstreamAgent"

    def log_message(self, *a):  # тишина в консоли
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8", extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}

    def _authed(self) -> bool:
        if not PASSWORD:
            return True
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return COOKIE in c and hmac.compare_digest(c[COOKIE].value, _session_token())

    def _ip(self) -> str:
        return self.client_address[0]

    def _blocked(self) -> bool:
        now = time.time()
        recent = [t for t in _fails.get(self._ip(), []) if now - t < 300]
        _fails[self._ip()] = recent
        return len(recent) >= 10  # 10 неудач за 5 минут — пауза

    def _login_ok(self, pwd: str) -> bool:
        if self._blocked():
            return False
        if hmac.compare_digest(pwd.encode(), PASSWORD.encode()):
            _fails.pop(self._ip(), None)
            return True
        _fails.setdefault(self._ip(), []).append(time.time())
        return False

    def _redirect_with_cookie(self):
        self.send_response(303)
        self.send_header("Set-Cookie", f"{COOKIE}={_session_token()}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000")
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _login_page(self, err: str = "", code: int = 200):
        html = LOGIN_PAGE.replace("%ERR%", f'<div class="err">{err}</div>' if err else "")
        self._send(code, html.encode("utf-8"), "text/html; charset=utf-8")

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if PASSWORD and u.path == "/login":  # «ссылка-вход»: /login?key=<пароль> — вошёл по одному клику
            if self._login_ok((q.get("key") or [""])[0]):
                return self._redirect_with_cookie()
            return self._login_page("Неверный или просроченный ключ" if not self._blocked() else "Слишком много попыток, подождите 5 минут", 401)
        if u.path == "/api/ping":
            return self._json({"ok": True})
        if not self._authed():
            return self._login_page() if u.path in ("/", "/index.html") else self._json({"error": "нужен вход"}, 401)
        if u.path in ("/", "/index.html"):
            f = resource_dir() / "web" / "index.html"
            if not f.exists():
                f = Path(__file__).parent / "web" / "index.html"
            return self._send(200, f.read_bytes(), "text/html; charset=utf-8")
        if u.path == "/api/ping":
            return self._json({"ok": True})
        if u.path == "/api/state":
            return self._json(state())
        if u.path == "/api/keys":
            return self._json(keys_state())
        if u.path == "/api/news/preview":
            return self._json(news_preview(int((q.get("n") or ["30"])[0])))
        if u.path.startswith("/api/jobs/"):
            job = JOBS.get(u.path.rsplit("/", 1)[-1])
            if not job:
                return self._json({"error": "нет такой задачи"}, 404)
            since = int((q.get("since") or ["0"])[0])
            return self._json({"status": job.status, "lines": job.lines[since:], "next": len(job.lines), "outputs": job.outputs})
        if u.path.startswith("/download/"):
            key = u.path.rsplit("/", 1)[-1]
            name = FILES.get(key, ("",))[0]
            f = DATA / name
            if not name or not f.exists():
                return self._send(404, "Файл ещё не создан".encode("utf-8"), "text/plain; charset=utf-8")
            return self._send(200, f.read_bytes(), mimetypes.guess_type(name)[0] or "application/octet-stream",
                              {"Content-Disposition": f'attachment; filename="{name}"'})
        self._send(404, b"not found", "text/plain")

    def do_POST(self):  # noqa: N802
        u = urlparse(self.path)
        if PASSWORD and u.path == "/login":
            n = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(n).decode("utf-8")) if n else {}
            if self._login_ok((form.get("password") or [""])[0]):
                return self._redirect_with_cookie()
            return self._login_page("Неверный пароль" if not self._blocked() else "Слишком много попыток, подождите 5 минут", 401)
        if not self._authed():
            return self._json({"error": "нужен вход"}, 401)
        try:
            body = self._body()
        except ValueError:
            return self._json({"error": "bad json"}, 400)
        if u.path == "/api/model":
            b, _, m = str(body.get("value", "")).partition("|")
            if (b, m) not in {(x[0], x[1]) for x in llm.CATALOG}:
                return self._json({"error": "неизвестная модель"}, 400)
            llm.configure(b, m)
            return self._json({"ok": True, "current": f"{b}|{m}", "ready": llm.configured(b)})
        if u.path == "/api/keys":
            save_keys(body)
            return self._json(keys_state())
        if u.path == "/api/jobs":
            kind = body.get("kind")
            if kind not in JOB_KINDS:
                return self._json({"error": "неизвестная задача"}, 400)
            if not JOB_LOCK.acquire(blocking=False):
                return self._json({"error": "уже выполняется другая задача — дождитесь или остановите её"}, 409)
            job = Job(kind)
            llm.CANCEL.clear()
            JOBS[job.id] = job
            run_job(job, JOB_KINDS[kind], body.get("params") or {})
            return self._json({"id": job.id})
        if u.path.startswith("/api/jobs/") and u.path.endswith("/cancel"):
            job = JOBS.get(u.path.split("/")[-2])
            if job:
                job.cancel = True
                llm.CANCEL.set()  # прерывает ожидание ответа модели, а не только ближайшую строку журнала
            return self._json({"ok": True})
        if u.path == "/api/ask":
            from downstream_agent.ask import ask
            q = str(body.get("question", "")).strip()
            if not q:
                return self._json({"error": "пустой вопрос"}, 400)
            try:
                res = ask(q, None, 3, log=lambda *_: None)
                for e in res.get("evidence", []):
                    pass
                return self._json(res)
            except Exception as e:  # noqa: BLE001
                return self._json({"error": str(e)}, 500)
        if u.path == "/api/shutdown":
            self._json({"ok": True})
            threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()
            return
        self._json({"error": "not found"}, 404)


def _already_running(port: int) -> bool:
    import urllib.request
    try:
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/api/ping"), timeout=1.5)
        return True
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    env = ROOT / ".env"
    bundled = resource_dir() / ".env"
    if not env.exists() and bundled.exists() and bundled != env:
        shutil.copy(bundled, env)  # первый запуск: вшитые ключи становятся редактируемым .env
    config_path()
    DATA.mkdir(parents=True, exist_ok=True)
    if _already_running(PORT):  # уже запущено — просто открываем интерфейс
        webbrowser.open(f"http://127.0.0.1:{PORT}/")
        return 0
    try:
        newsheet.build_archive(DATA / FILES["archive"][0])
    except Exception as e:
        print(f"Не удалось обновить архив новостей: {e}")
    srv = None
    for port in range(PORT, PORT + 20):
        try:
            srv = ThreadingHTTPServer((HOST, port), Handler)
            break
        except OSError:
            continue
    if srv is None:
        print("Не удалось занять порт")
        return 1
    if not os.environ.get("AGENT_NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{port}/")).start()
    print(f"Downstream Agent: http://127.0.0.1:{port}/  (данные: {DATA})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
