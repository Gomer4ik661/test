"""Поиск через DuckDuckGo и загрузка страниц/файлов. Ничего не знает о Telegram."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Mobile Safari/537.36",
    "Accept-Language": "ru,en;q=0.8",
}
TIMEOUT = aiohttp.ClientTimeout(total=60, sock_connect=15)
MAX_PAGE = 3 * 1024 * 1024    # больше этого HTML не читаем
MAX_FILE = 50 * 1024 * 1024   # предел Telegram для файлов от бота
HTML_TYPES = {"text/html", "application/xhtml+xml"}
JUNK_TAGS = ["script", "style", "noscript", "svg", "iframe", "template", "nav", "footer", "aside", "form", "button"]


class TooBig(Exception):
    """Файл больше MAX_FILE: Telegram его не примет."""


@dataclass
class Result:
    title: str
    url: str
    snippet: str


@dataclass
class Page:
    url: str
    title: str
    text: str
    links: list[tuple[str, str]] = field(default_factory=list)  # (текст ссылки, адрес)


@dataclass
class File:
    url: str
    name: str
    data: bytes


def new_session() -> aiohttp.ClientSession:
    # trust_env: учитывать HTTPS_PROXY из окружения, если он задан
    return aiohttp.ClientSession(headers=HEADERS, timeout=TIMEOUT, trust_env=True)


# ---------- поиск ----------

async def search(session: aiohttp.ClientSession, query: str, limit: int = 8) -> list[Result]:
    """Ищет в html-версии DuckDuckGo, при неудаче — в lite-версии."""
    for url, parser in (
        ("https://html.duckduckgo.com/html/", parse_ddg_html),
        ("https://lite.duckduckgo.com/lite/", parse_ddg_lite),
    ):
        try:
            async with session.post(url, data={"q": query, "kl": "ru-ru"}) as r:
                results = parser(await r.text())
        except (aiohttp.ClientError, TimeoutError):
            continue
        if results:
            return results[:limit]
    return []


def unwrap_ddg(href: str) -> str | None:
    """Достаёт настоящий адрес из ссылки-редиректа DuckDuckGo. Рекламу отбрасывает."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com"):
        if parsed.path == "/l/":
            target = parse_qs(parsed.query).get("uddg")
            return target[0] if target else None
        return None  # y.js и прочее — реклама
    return href if parsed.scheme in ("http", "https") else None


def parse_ddg_html(markup: str) -> list[Result]:
    soup = BeautifulSoup(markup, "html.parser")
    results = []
    for block in soup.select("div.result"):
        if "result--ad" in block.get("class", []):
            continue
        a = block.select_one("a.result__a")
        url = a and unwrap_ddg(a.get("href", ""))
        if url:
            snippet = block.select_one(".result__snippet")
            results.append(Result(clean(a.get_text()), url, clean(snippet.get_text()) if snippet else ""))
    return results


def parse_ddg_lite(markup: str) -> list[Result]:
    soup = BeautifulSoup(markup, "html.parser")
    results = []
    for a in soup.select("a.result-link"):
        url = unwrap_ddg(a.get("href", ""))
        if url:
            snippet = a.find_next("td", class_="result-snippet")
            results.append(Result(clean(a.get_text()), url, clean(snippet.get_text()) if snippet else ""))
    return results


# ---------- страницы и файлы ----------

async def fetch(session: aiohttp.ClientSession, url: str, as_file: bool = False) -> Page | File:
    """HTML и простой текст превращает в Page, всё остальное (или as_file=True) — в File."""
    async with session.get(url) as r:
        r.raise_for_status()
        final = str(r.url)
        ctype = r.content_type
        if not as_file and (ctype in HTML_TYPES or ctype == "text/plain"):
            body = await read_limited(r, MAX_PAGE, cut=True)
            if ctype == "text/plain":
                text = body.decode(r.charset or "utf-8", errors="replace")
                return Page(final, file_name(r, final), text.strip())
            return parse_page(body, final, r.charset)
        if r.content_length and r.content_length > MAX_FILE:
            raise TooBig(r.content_length)
        data = await read_limited(r, MAX_FILE)
        return File(final, file_name(r, final), data)


async def read_limited(r: aiohttp.ClientResponse, limit: int, cut: bool = False) -> bytes:
    """Читает ответ, но не больше limit байт: обрезает (cut) или бросает TooBig."""
    chunks, size = [], 0
    async for chunk in r.content.iter_chunked(64 * 1024):
        size += len(chunk)
        if size > limit:
            if cut:
                chunks.append(chunk[: limit - (size - len(chunk))])
                break
            raise TooBig(size)
        chunks.append(chunk)
    return b"".join(chunks)


def file_name(r: aiohttp.ClientResponse, url: str) -> str:
    if r.content_disposition and r.content_disposition.filename:
        return r.content_disposition.filename
    name = unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
    if not name:
        name = urlparse(url).netloc or "file"
    if "." not in name and r.content_type in HTML_TYPES:
        name += ".html"
    return name


def parse_page(body: bytes, url: str, charset: str | None = None) -> Page:
    soup = BeautifulSoup(body, "html.parser", from_encoding=charset)
    title = clean(soup.title.get_text()) if soup.title else urlparse(url).netloc

    # Ссылки собираем до чистки, чтобы меню сайта тоже было доступно.
    links, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"]).split("#")[0]
        if urlparse(href).scheme in ("http", "https") and href not in seen and href != url:
            seen.add(href)
            links.append((clean(a.get_text()) or href, href))

    for tag in soup(JUNK_TAGS):
        tag.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup
    lines = (clean(line) for line in root.get_text("\n").splitlines())
    text = "\n".join(line for line in lines if line)
    return Page(url, title, text, links)


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
