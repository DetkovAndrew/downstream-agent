"""Сбор отчётов, пресс-релизов и новостей в локальную папку.

Раскладка: data/raw/<company>/{reports,news}/..., data/manifest.json — что уже скачано.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
from lxml import etree, html

from downstream_agent.paths import app_dir

ROOT = app_dir()
DATA = ROOT / "data"


@dataclass
class Report:
    company: str
    downloaded: list[str] = field(default_factory=list)
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def load_config(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


class Manifest:
    def __init__(self, path: Path):
        self.path = path
        self.items: dict[str, dict] = {}
        if path.exists():
            self.items = json.loads(path.read_text(encoding="utf-8"))

    def has(self, url: str) -> bool:
        return url in self.items

    def add(self, url: str, **info) -> None:
        self.items[url] = info
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.items, ensure_ascii=False, indent=1), encoding="utf-8")


def _safe_name(url: str) -> str:
    name = unquote(Path(urlparse(url).path).name) or "document"
    name = re.sub(r"[^\w.\-]+", "_", name, flags=re.UNICODE)[:120]
    digest = hashlib.sha1(url.encode()).hexdigest()[:8]
    stem, dot, ext = name.rpartition(".")
    return f"{stem}_{digest}.{ext}" if dot else f"{name}_{digest}"


class Collector:
    def __init__(self, config: dict, data_dir: Path = DATA, log=print):
        self.cfg = config.get("collector", {})
        self.companies = config["companies"]
        self.data = data_dir
        self.manifest = Manifest(data_dir / "manifest.json")
        self.log = log
        # Два маршрута: через системный прокси (trust_env) и напрямую. Какой сайт открывается
        # с какого маршрута, зависит от выходного IP: российские сайты — напрямую,
        # западные — через прокси. Запоминаем рабочий маршрут по хосту.
        ua = self.cfg.get("user_agent", "Downstream-Agent/0.1")
        self.routes = {"proxy": requests.Session(), "direct": requests.Session()}
        self.routes["direct"].trust_env = False
        for s_ in self.routes.values():
            s_.headers["User-Agent"] = ua
        self.http = self.routes["proxy"]
        self.host_route: dict[str, str] = {}
        self.timeout = self.cfg.get("timeout", 30)
        self.delay = self.cfg.get("delay_sec", 1.0)
        self.keywords = [k.lower() for k in self.cfg.get("pdf_keywords", [])]
        self.max_bytes = self.cfg.get("max_pdf_mb", 80) * 1024 * 1024

    def run(self, only: list[str] | None = None, reports=True, news=True) -> list[Report]:
        out = []
        for c in self.companies:
            if only and c["id"] not in only:
                continue
            rep = Report(c["id"])
            self.log(f"== {c['name']} ({c['id']})")
            if reports:
                for page in c.get("pages", []):
                    self._scan_page(c, page, rep)
                for url in c.get("pdfs", []):
                    self._download(c, url, "reports", rep)
            if news:
                for feed in c.get("feeds", []):
                    self._read_feed(c, feed, rep)
            self.log(f"   скачано {len(rep.downloaded)}, пропущено {rep.skipped}, ошибок {len(rep.errors)}")
            out.append(rep)
        return out

    def _get(self, url: str, **kw) -> requests.Response:
        time.sleep(self.delay)
        host = urlparse(url).netloc
        first = self.host_route.get(host, "proxy")
        order = [first, "direct" if first == "proxy" else "proxy"]
        last: Exception | None = None
        for name in order:
            try:
                r = self.routes[name].get(url, timeout=(10, self.timeout), **kw)
                if r.status_code in (403, 412, 429) or r.status_code >= 500:
                    last = requests.HTTPError(f"{r.status_code} via {name}", response=r)
                    continue
                r.raise_for_status()
                self.host_route[host] = name
                return r
            except requests.ConnectionError as e:
                last = e
            except requests.Timeout as e:
                last = e
        assert last is not None
        raise last

    def _scan_page(self, c: dict, page: str, rep: Report) -> None:
        try:
            r = self._get(page)
            doc = html.fromstring(r.content, base_url=page)
        except Exception as e:
            rep.errors.append(f"{page}: {e}")
            self.log(f"   ! {page}: {e}")
            return
        found = {}
        for a in doc.iter("a"):
            href = a.get("href")
            if not href or href.startswith(("#", "mailto:", "javascript:")):
                continue
            url = urljoin(page, href).split("#")[0]
            if not urlparse(url).path.lower().endswith(".pdf"):
                continue
            label = f"{(a.text_content() or '').strip()} {unquote(url)}".lower()
            if not self.keywords or any(k in label for k in self.keywords):
                found[url] = (a.text_content() or "").strip()
        if not found and getattr(self, "browser", False):
            from downstream_agent import browser
            try:
                for href, text in browser.render_links(page):
                    if urlparse(href).path.lower().endswith(".pdf"):
                        label = f"{text} {unquote(href)}".lower()
                        if not self.keywords or any(k in label for k in self.keywords):
                            found[href.split("#")[0]] = text
            except browser.BrowserUnavailable as e:
                self.log(f"   ! браузер: {e}")
                self.browser = False
        self.log(f"   {page}: PDF-ссылок {len(found)}")
        for url, title in found.items():
            self._download(c, url, "reports", rep, title=title, source_page=page)

    def _download(self, c: dict, url: str, kind: str, rep: Report, **meta) -> None:
        if self.manifest.has(url):
            rep.skipped += 1
            return
        try:
            r = self._get(url, stream=True)
            ctype = r.headers.get("content-type", "")
            if int(r.headers.get("content-length", 0)) > self.max_bytes:
                raise ValueError("файл больше лимита max_pdf_mb")
            body = b""
            for chunk in r.iter_content(1 << 16):
                body += chunk
                if len(body) > self.max_bytes:
                    raise ValueError("файл больше лимита max_pdf_mb")
            if url.lower().endswith(".pdf") and not body.startswith(b"%PDF"):
                raise ValueError(f"не PDF (content-type {ctype})")
        except Exception as e:
            rep.errors.append(f"{url}: {e}")
            self.log(f"   ! {url}: {e}")
            return
        digest = hashlib.sha256(body).hexdigest()
        if any(v.get("sha256") == digest for v in self.manifest.items.values()):
            rep.skipped += 1  # тот же файл по другому URL
            return
        folder = self.data / "raw" / c["id"] / kind
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / _safe_name(url)
        path.write_bytes(body)
        self.manifest.add(url, company=c["id"], kind=kind, file=str(path.relative_to(self.data)),
                          sha256=digest, fetched=time.strftime("%Y-%m-%dT%H:%M:%S"), **meta)
        rep.downloaded.append(str(path))
        self.log(f"   + {path.name} ({len(body) // 1024} КБ)")

    def _read_feed(self, c: dict, feed: str, rep: Report) -> None:
        try:
            root = etree.fromstring(self._get(feed).content)
        except Exception as e:
            rep.errors.append(f"{feed}: {e}")
            self.log(f"   ! {feed}: {e}")
            return
        ns = {"a": "http://www.w3.org/2005/Atom"}
        entries = root.findall(".//item") or root.findall(".//a:entry", ns)
        self.log(f"   {feed}: записей {len(entries)}")
        for e in entries:
            def tx(tag):
                el = e.find(tag) if e.find(tag) is not None else e.find(f"a:{tag}", ns)
                return (el.text or "").strip() if el is not None else ""
            link = tx("link")
            if not link:
                l = e.find("a:link", ns)
                link = l.get("href", "") if l is not None else ""
            if not link or self.manifest.has(link):
                rep.skipped += 1
                continue
            item = {"title": tx("title"), "published": tx("pubDate") or tx("published") or tx("updated"),
                    "summary": tx("description") or tx("summary"), "url": link}
            try:
                body = self._get(link).text
                item["text"] = html.fromstring(body).text_content()
            except Exception as ex:
                item["fetch_error"] = str(ex)
            folder = self.data / "raw" / c["id"] / "news"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / (_safe_name(link) + ".json")
            path.write_text(json.dumps(item, ensure_ascii=False, indent=1), encoding="utf-8")
            self.manifest.add(link, company=c["id"], kind="news", file=str(path.relative_to(self.data)))
            rep.downloaded.append(str(path))
            self.log(f"   + {item['title'][:70]}")
