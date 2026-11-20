"""LLM-клиент: Google Gemini API (основной) или локальный Ollama (запасной).

Настройки читаются из .env рядом с проектом или из переменных окружения.
Запросы к Gemini идут по системному маршруту (прокси), при сбое — напрямую.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

import requests

from downstream_agent.paths import app_dir, resource_dir

ROOT = app_dir()


def _load_env() -> None:
    """Сначала .env рядом с программой (приоритет), затем вшитый в .exe — для первого запуска."""
    for f in (ROOT / ".env", resource_dir() / ".env"):
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                m = re.match(r"\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$", line)
                if m and not line.lstrip().startswith("#") and m.group(2):
                    os.environ.setdefault(m.group(1), m.group(2).strip("'\""))


_load_env()
HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
MODEL = os.environ.get("AGENT_MODEL", "qwen3:4b")  # модель Ollama
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"


class LlmUnavailable(RuntimeError):
    pass


def gemini_keys() -> list[str]:
    """Ключи из GEMINI_API_KEYS (через запятую) и GEMINI_API_KEY; дубли убираются."""
    raw = ",".join(filter(None, [os.environ.get("GEMINI_API_KEYS"), os.environ.get("GEMINI_API_KEY"),
                                 os.environ.get("GOOGLE_API_KEY")]))
    return list(dict.fromkeys(k.strip() for k in raw.split(",") if k.strip()))


def gemini_key() -> str | None:
    keys = gemini_keys()
    return keys[0] if keys else None


_exhausted: set[tuple[int, str]] = set()  # (номер ключа, модель) — суточный лимит исчерпан


def gemini_models() -> list[str]:
    """Основная модель и запасные (GEMINI_FALLBACK через запятую) — на случай суточного лимита."""
    main = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
    extra = [m.strip() for m in os.environ.get("GEMINI_FALLBACK", "gemini-3.1-flash-lite").split(",") if m.strip()]
    return [main] + [m for m in extra if m != main]


def gemini_model() -> str:
    for m in gemini_models():
        if any((i, m) not in _exhausted for i in range(len(gemini_keys()))):
            return m
    return gemini_models()[0]


def backend() -> str:
    b = os.environ.get("AGENT_BACKEND", "auto").lower()
    if b == "auto":
        return "gemini" if gemini_key() else "ollama"
    return b


def _sessions() -> list[requests.Session]:
    proxied, direct = requests.Session(), requests.Session()
    direct.trust_env = False
    return [proxied, direct]


CANCEL = threading.Event()  # выставляется интерфейсом по кнопке «Остановить»: прерывает ожидание ответа модели
_cool: dict[tuple[int, str], float] = {}  # (ключ, модель) -> до какого момента не трогаем после 503/таймаута
BATCH_BUDGET = 240.0  # секунд на один запрос (все ключи и повторы): дальше пачка пропускается, а не висит часами


def _gemini_post(path: str, payload: dict, timeout: int = 300, retries: int = 4) -> dict:
    """path вида 'models/<имя>:generateContent'.

    Перебирает пары (модель, ключ): суточный лимит помечает пару исчерпанной, а 503/таймаут — «остывающей» на 45 с;
    минутный лимит переключает на следующий ключ. Общий бюджет времени — BATCH_BUDGET, остановка пользователем — CANCEL.
    """
    keys = gemini_keys()
    if not keys:
        raise LlmUnavailable("нет GEMINI_API_KEY (см. .env.example)")
    cur = path.split("/")[1].split(":")[0]
    models = [cur] + [m for m in gemini_models() if m != cur]
    deadline = time.time() + BATCH_BUDGET
    last: Exception | None = None
    while True:
        if CANCEL.is_set():
            raise LlmUnavailable("остановлено пользователем")
        now = time.time()
        if now > deadline:
            raise LlmUnavailable(f"Gemini не ответил за {int(BATCH_BUDGET)} с (перегрузка): {str(last)[:100]}")
        live = [(m, i, k) for m in models for i, k in enumerate(keys) if (i, m) not in _exhausted]
        if not live:
            raise LlmUnavailable("суточные лимиты исчерпаны у всех ключей и моделей: " + ", ".join(models))
        ready = [c for c in live if _cool.get((c[1], c[0]), 0) <= now]
        if not ready:  # все «остывают» — ждём ближайшего, но не дольше бюджета
            wait = min(_cool.get((c[1], c[0]), 0) for c in live) - now
            time.sleep(min(max(wait, 1.0), 5.0))
            continue
        for model, i, key in ready:
            if CANCEL.is_set() or time.time() > deadline:
                break
            try:
                res = _gemini_post_once(path.replace(cur, model), payload, min(timeout, 75), key)
                _cool.pop((i, model), None)
                return res
            except _RateLimited as e:
                last = e
                if e.daily:
                    _exhausted.add((i, model))
                else:
                    _cool[(i, model)] = time.time() + 45
            except LlmUnavailable as e:
                last = e
                if "HTTP 4" in str(e):  # ошибка запроса (не лимит): другой ключ не поможет
                    raise
                _cool[(i, model)] = time.time() + 45  # 503 / таймаут / обрыв — пару не трогаем 45 с


class _RateLimited(LlmUnavailable):
    def __init__(self, msg: str, daily: bool):
        super().__init__(msg)
        self.daily = daily


def _gemini_post_once(path: str, payload: dict, timeout: int, key: str) -> dict:
    last: Exception | None = None
    for s in _sessions():
        try:
            r = s.post(f"{GEMINI_URL}/{path}", params={"key": key}, json=payload, timeout=(10, timeout))
        except requests.RequestException as e:
            last = e
            continue
        if r.status_code == 429:
            raise _RateLimited(f"HTTP 429: {r.text[:200]}", daily="PerDay" in r.text)
        if r.status_code >= 500:
            last = LlmUnavailable(f"HTTP {r.status_code}: {r.text[:200]}")
            break  # перегрузка на стороне Google: другой маршрут не поможет, пробуем другой ключ
        if r.status_code == 400 and "location is not supported" in r.text:
            last = LlmUnavailable("маршрут: регион не поддерживается Google, нужен рабочий прокси")
            continue
        if r.status_code >= 400:
            raise LlmUnavailable(f"HTTP {r.status_code}: {r.text[:300]}")
        return r.json()
    raise LlmUnavailable(str(last))


def _gemini_text(data: dict) -> str:
    try:
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError) as e:
        raise LlmUnavailable(f"пустой ответ Gemini: {str(data)[:300]}") from e


def list_models() -> list[str]:
    key = gemini_key()
    if not key:
        raise LlmUnavailable("нет GEMINI_API_KEY")
    for s in _sessions():
        try:
            r = s.get(f"{GEMINI_URL}/models", params={"key": key, "pageSize": 100}, timeout=30)
            r.raise_for_status()
            return [m["name"].removeprefix("models/") for m in r.json().get("models", [])
                    if "generateContent" in m.get("supportedGenerationMethods", [])]
        except requests.RequestException:
            continue
    raise LlmUnavailable("Gemini API недоступен")


def _parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            return json.loads(m.group(0))
        raise


YANDEX_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"


def _deepseek_complete(system: str, user: str, timeout: int, json_mode: bool = True, max_tokens: int = 8000) -> str:
    """DeepSeek API напрямую (OpenAI-совместимый). Модель — DEEPSEEK_MODEL (по умолчанию deepseek-flash)."""
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise LlmUnavailable("нет DEEPSEEK_API_KEY")
    body = {"model": os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"), "temperature": 0, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    effort = os.environ.get("DEEPSEEK_EFFORT", "low")  # low/high/max: чем ниже, тем быстрее и дешевле
    if effort:
        body["reasoning_effort"] = effort
    last: Exception | None = None
    for attempt in range(3):
        for s in _sessions():
            try:
                r = s.post("https://api.deepseek.com/chat/completions", json=body, timeout=timeout,
                           headers={"Authorization": f"Bearer {key}"})
            except requests.RequestException as e:
                last = e
                continue
            if r.status_code == 400 and "reasoning_effort" in r.text and "reasoning_effort" in body:
                body.pop("reasoning_effort")  # эта модель/версия API параметр не знает
                break
            if r.status_code == 429 or r.status_code >= 500:
                last = LlmUnavailable(f"DeepSeek HTTP {r.status_code}: {r.text[:150]}")
                time.sleep(5 * (attempt + 1))
                break
            if r.status_code >= 400:
                raise LlmUnavailable(f"DeepSeek HTTP {r.status_code}: {r.text[:250]}")
            try:
                return r.json()["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, ValueError) as e:
                raise LlmUnavailable(f"пустой ответ DeepSeek: {r.text[:200]}") from e
    raise LlmUnavailable(str(last))


_giga = {"token": "", "exp": 0.0}


def _giga_verify():
    """Сертификат Минцифры нужен для gigachat.devices.sberbank.ru; собранный набор лежит в certs/ca_bundle.pem."""
    for base in (resource_dir(), ROOT):
        b = base / "certs" / "ca_bundle.pem"
        if b.exists():
            return str(b)
    return True


def _giga_token(force: bool = False) -> str:
    import uuid
    key = os.environ.get("GIGACHAT_AUTH_KEY")
    if not key:
        raise LlmUnavailable("нет GIGACHAT_AUTH_KEY")
    if not force and _giga["token"] and time.time() < _giga["exp"] - 60:
        return _giga["token"]
    last: Exception | None = None
    for s in _sessions():
        try:
            r = s.post("https://ngw.devices.sberbank.ru:9443/api/v2/oauth", timeout=30, verify=_giga_verify(),
                       data={"scope": os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")},
                       headers={"Authorization": "Basic " + key, "RqUID": str(uuid.uuid4()),
                                "Content-Type": "application/x-www-form-urlencoded"})
        except requests.RequestException as e:
            last = e
            continue
        if r.status_code != 200:
            raise LlmUnavailable(f"GigaChat OAuth HTTP {r.status_code}: {r.text[:150]}")
        j = r.json()
        _giga["token"], _giga["exp"] = j["access_token"], j["expires_at"] / 1000.0
        return _giga["token"]
    raise LlmUnavailable(f"GigaChat OAuth недоступен: {str(last)[:120]}")


def _gigachat_complete(system: str, user: str, timeout: int, json_mode: bool = True, max_tokens: int = 4000) -> str:
    model = os.environ.get("GIGACHAT_MODEL", "GigaChat-2-Pro")
    sys_text = system + ("\nReply with a single valid JSON object only, no markdown, no comments." if json_mode else "")
    body = {"model": model, "temperature": 0.01, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": sys_text}, {"role": "user", "content": user}]}
    last: Exception | None = None
    for attempt in range(3):
        token = _giga_token()
        for s in _sessions():
            try:
                r = s.post("https://gigachat.devices.sberbank.ru/api/v1/chat/completions", json=body, timeout=timeout,
                           verify=_giga_verify(), headers={"Authorization": f"Bearer {token}"})
            except requests.RequestException as e:
                last = e
                continue
            if r.status_code == 401:
                _giga_token(force=True)
                break
            if r.status_code == 402:
                raise LlmUnavailable("GigaChat: бесплатные токены этой модели закончились (402)")
            if r.status_code == 429 or r.status_code >= 500:
                last = LlmUnavailable(f"GigaChat HTTP {r.status_code}: {r.text[:120]}")
                time.sleep(4 * (attempt + 1))
                break
            if r.status_code >= 400:
                raise LlmUnavailable(f"GigaChat HTTP {r.status_code}: {r.text[:250]}")
            try:
                ch = r.json()["choices"][0]
                if ch.get("finish_reason") == "blacklist":
                    raise LlmUnavailable("GigaChat: ответ заблокирован фильтром (blacklist)")
                return ch["message"]["content"] or ""
            except (KeyError, IndexError, ValueError) as e:
                raise LlmUnavailable(f"пустой ответ GigaChat: {r.text[:200]}") from e
    raise LlmUnavailable(str(last))


def gigachat_balance() -> dict:
    tok = _giga_token()
    for s in _sessions():
        try:
            r = s.get("https://gigachat.devices.sberbank.ru/api/v1/balance", timeout=30, verify=_giga_verify(),
                      headers={"Authorization": f"Bearer {tok}"})
            if r.status_code == 200:
                return {b.get("usage"): b.get("value") for b in r.json().get("balance", [])}
            return {"ошибка": f"HTTP {r.status_code}: {r.text[:100]}"}
        except requests.RequestException:
            continue
    return {"ошибка": "нет связи"}


def yandex_ready() -> bool:
    return bool(os.environ.get("YANDEX_API_KEY") and os.environ.get("YANDEX_FOLDER_ID"))


def _yandex_complete(system: str, user: str, timeout: int, json_mode: bool = True, max_tokens: int = 4000) -> str:
    """YandexGPT (Foundation Models API). Достучаться можно только напрямую с российского IP, не через зарубежный прокси."""
    model = os.environ.get("YANDEX_MODEL", "yandexgpt/latest")
    if not model.startswith("yandexgpt"):  # DeepSeek, Qwen, gpt-oss и др. — через OpenAI-совместимый интерфейс
        return _yandex_openai(model, system, user, timeout, json_mode, max_tokens)
    body = {"modelUri": f"gpt://{os.environ.get('YANDEX_FOLDER_ID', '')}/{model}",
            "completionOptions": {"stream": False, "temperature": 0, "maxTokens": str(max_tokens)},
            "messages": [{"role": "system", "text": system}, {"role": "user", "text": user}]}
    if json_mode:
        body["jsonObject"] = True
    s = requests.Session()
    s.trust_env = False  # напрямую
    last: Exception | None = None
    for attempt in range(3):
        try:
            r = s.post(YANDEX_URL, json=body, timeout=timeout,
                       headers={"Authorization": f"Api-Key {os.environ.get('YANDEX_API_KEY', '')}"})
        except requests.RequestException as e:
            last = e
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            last = LlmUnavailable(f"Yandex HTTP {r.status_code}: {r.text[:150]}")
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 403:
            raise LlmUnavailable("Yandex 403: у сервисного аккаунта нет роли ai.languageModels.user на каталог "
                                 "(или у ключа нет области yc.ai.languageModels.execute)")
        if r.status_code >= 400:
            raise LlmUnavailable(f"Yandex HTTP {r.status_code}: {r.text[:200]}")
        try:
            return r.json()["result"]["alternatives"][0]["message"]["text"]
        except (KeyError, IndexError, ValueError) as e:
            raise LlmUnavailable(f"пустой ответ Yandex: {r.text[:200]}") from e
    raise LlmUnavailable(str(last))


def _yandex_openai(model: str, system: str, user: str, timeout: int, json_mode: bool, max_tokens: int) -> str:
    if "/" not in model:
        model += "/latest"
    body = {"model": f"gpt://{os.environ.get('YANDEX_FOLDER_ID', '')}/{model}", "temperature": 0,
            "max_tokens": max(max_tokens, 6000),  # у «думающих» моделей рассуждения тоже тратят лимит
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    s = requests.Session()
    s.trust_env = False
    last: Exception | None = None
    for attempt in range(3):
        try:
            r = s.post("https://llm.api.cloud.yandex.net/v1/chat/completions", json=body, timeout=timeout,
                       headers={"Authorization": f"Api-Key {os.environ.get('YANDEX_API_KEY', '')}",
                                "x-folder-id": os.environ.get("YANDEX_FOLDER_ID", "")})
        except requests.RequestException as e:
            last = e
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            last = LlmUnavailable(f"Yandex HTTP {r.status_code}: {r.text[:150]}")
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code >= 400:
            raise LlmUnavailable(f"Yandex HTTP {r.status_code}: {r.text[:250]}")
        try:
            ch = r.json()["choices"][0]
            if ch.get("finish_reason") == "content_filter":
                raise LlmUnavailable("Yandex: сработал контент-фильтр")
            return ch["message"]["content"] or ""
        except (KeyError, IndexError, ValueError) as e:
            raise LlmUnavailable(f"пустой ответ Yandex: {r.text[:200]}") from e
    raise LlmUnavailable(str(last))


def check_yandex() -> str:
    try:
        _yandex_complete("Отвечай одним словом.", "Скажи: ок", 40, json_mode=False, max_tokens=8)
        return "OK"
    except LlmUnavailable as e:
        return str(e)[:160]


def ask_json(system: str, user: str, model: str | None = None, timeout: int = 300, num_ctx: int = 4096,
             images: list[bytes] | None = None) -> dict:
    """Ответ строго JSON. model — только для Ollama; для Gemini берётся GEMINI_MODEL."""
    if backend() == "gigachat":
        try:
            return _parse_json(_gigachat_complete(system, user, timeout))
        except json.JSONDecodeError as e:
            raise LlmUnavailable(f"GigaChat вернул не JSON: {e}") from e
    if backend() == "deepseek":
        try:
            return _parse_json(_deepseek_complete(system, user, timeout))
        except json.JSONDecodeError as e:
            raise LlmUnavailable(f"DeepSeek вернул не JSON: {e}") from e
    if backend() == "yandex":
        try:
            return _parse_json(_yandex_complete(system, user, timeout))
        except json.JSONDecodeError as e:
            raise LlmUnavailable(f"Yandex вернул не JSON: {e}") from e
    if backend() == "gemini":
        import base64
        parts = [{"text": user}] + [{"inline_data": {"mime_type": "image/png", "data": base64.b64encode(i).decode()}}
                                    for i in images or []]
        data = _gemini_post(f"models/{gemini_model()}:generateContent", {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        }, timeout)
        try:
            return _parse_json(_gemini_text(data))
        except json.JSONDecodeError as e:
            raise LlmUnavailable(f"Gemini вернул не JSON: {e}") from e
    try:
        s = requests.Session()
        s.trust_env = False  # локальный Ollama не должен ходить через системный прокси
        r = s.post(f"{HOST}/api/chat", timeout=timeout, json={
            "model": model or MODEL, "stream": False, "format": "json", "think": False,
            "options": {"temperature": 0, "num_ctx": num_ctx},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        })
        r.raise_for_status()
        return json.loads(r.json()["message"]["content"])
    except (requests.RequestException, KeyError, json.JSONDecodeError) as e:
        raise LlmUnavailable(str(e)) from e


def web_search(query: str, n: int = 8) -> list[dict]:
    """Веб-поиск через Gemini + Google Search grounding.

    Возвращает [{title, href, body}] — тот же формат, что у ddgs. Ссылки Google-редиректа
    раскрываются до реального адреса.
    """
    global _search_ok
    if not _search_ok:
        return []
    try:
        data = _web_search_call(query)
    except LlmUnavailable as e:
        if "429" in str(e) or "quota" in str(e).lower():
            _search_ok = False  # поиск Google не входит в бесплатную квоту — дальше не пробуем
        return []
    return _grounded_results(data, n)


_search_ok = True


def _web_search_call(query: str) -> dict:
    return _gemini_post(f"models/{gemini_model()}:generateContent", {
        "contents": [{"role": "user", "parts": [{"text": f"Find official sources (pdf reports, press releases, news) for: {query}"}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0},
    }, timeout=60, retries=1)


def _grounded_results(data: dict, n: int) -> list[dict]:
    cand = data.get("candidates", [{}])[0]
    chunks = cand.get("groundingMetadata", {}).get("groundingChunks", [])
    summary = _gemini_text(data) if cand.get("content") else ""
    out, seen = [], set()
    for c in chunks:
        w = c.get("web") or {}
        url = w.get("uri")
        if not url:
            continue
        real = _resolve(url)
        if real in seen:
            continue
        seen.add(real)
        out.append({"title": w.get("title", ""), "href": real, "body": summary[:200]})
        if len(out) >= n:
            break
    return out


def _resolve(url: str) -> str:
    if "vertexaisearch.cloud.google.com" not in url and "grounding-api-redirect" not in url:
        return url
    for s in _sessions():
        try:
            r = s.get(url, allow_redirects=False, timeout=15)
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                return r.headers["location"]
        except requests.RequestException:
            continue
    return url


def check_keys(model: str | None = None) -> list[dict]:
    """Один лёгкий запрос на каждый ключ: жив ли ключ и есть ли остаток лимита на модели."""
    model = model or gemini_models()[0]
    out = []
    for i, key in enumerate(gemini_keys()):
        t = time.time()
        row = {"n": i + 1, "tail": key[-4:], "model": model}
        try:
            _gemini_post_once(f"models/{model}:generateContent", {
                "contents": [{"parts": [{"text": "ok"}]}],
                "generationConfig": {"maxOutputTokens": 4}}, 60, key)
            row["status"] = "OK"
        except _RateLimited as e:
            row["status"] = "лимит в сутки исчерпан" if e.daily else "минутный лимит (подождите)"
        except LlmUnavailable as e:
            msg = str(e)
            row["status"] = ("ключ недействителен или заблокирован" if "API key" in msg or "HTTP 400" in msg
                             or "HTTP 403" in msg else f"ошибка: {msg[:80]}")
        row["sec"] = round(time.time() - t, 1)
        out.append(row)
    return out


# ------------------------------------------------------------------ выбор модели (для интерфейса)
CATALOG = [
    # (бэкенд, модель, подпись, нужен ли VPN/прокси)
    ("gigachat", "GigaChat-2-Pro", "GigaChat 2 Pro (Сбер) — без VPN", False),
    ("gigachat", "GigaChat-2", "GigaChat 2 (быстрый) — без VPN", False),
    ("gigachat", "GigaChat-2-Max", "GigaChat 2 Max — без VPN", False),
    ("yandex", "deepseek-v4.1-flash", "DeepSeek V4.1 Flash (Яндекс) — без VPN, платно", False),
    ("yandex", "qwen3.6-35b-a3b", "Qwen 3.6 35B (Яндекс) — без VPN, платно", False),
    ("yandex", "gpt-oss-120b", "gpt-oss 120B (Яндекс) — без VPN, платно", False),
    ("yandex", "yandexgpt/latest", "YandexGPT Pro — без VPN, платно", False),
    ("gemini", "gemini-3.1-flash-lite", "Gemini 3.1 Flash Lite — нужен прокси/VPN", True),
    ("gemini", "gemini-3.5-flash", "Gemini 3.5 Flash — нужен прокси/VPN", True),
    ("deepseek", "deepseek-flash", "DeepSeek V4.1 Flash (напрямую) — платно", False),
    ("ollama", "qwen3:4b", "Qwen3 4B (локально) — без интернета, медленно", False),
]
_MODEL_ENV = {"gemini": "GEMINI_MODEL", "gigachat": "GIGACHAT_MODEL", "yandex": "YANDEX_MODEL",
              "deepseek": "DEEPSEEK_MODEL", "ollama": "AGENT_MODEL"}


def configure(backend_name: str, model: str) -> None:
    """Переключает бэкенд и модель для всех последующих вызовов (без перезапуска)."""
    global MODEL
    os.environ["AGENT_BACKEND"] = backend_name
    os.environ[_MODEL_ENV[backend_name]] = model
    if backend_name == "ollama":
        MODEL = model


def configured(backend_name: str) -> bool:
    """Есть ли для бэкенда всё нужное (ключи)."""
    return {"gemini": bool(gemini_keys()), "gigachat": bool(os.environ.get("GIGACHAT_AUTH_KEY")),
            "yandex": yandex_ready(), "deepseek": bool(os.environ.get("DEEPSEEK_API_KEY")),
            "ollama": True}.get(backend_name, False)


def current() -> tuple[str, str]:
    b = backend()
    model = {"gemini": gemini_models()[0], "gigachat": os.environ.get("GIGACHAT_MODEL", "GigaChat-2-Pro"),
             "yandex": os.environ.get("YANDEX_MODEL", "yandexgpt/latest"),
             "deepseek": os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"), "ollama": MODEL}.get(b, "")
    return b, model
