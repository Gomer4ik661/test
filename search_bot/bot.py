"""Telegram-бот «текстовый браузер»: поиск, чтение сайтов текстом, скачивание файлов.
Запуск: python bot.py"""
from __future__ import annotations

import asyncio
import html
import itertools
import logging
import os
import re
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import web

BASE = Path(__file__).parent
CHUNK = 3500        # символов текста в одном сообщении (лимит Telegram — 4096)
LINKS_PER_PAGE = 10
SEARCH_WORDS = ("найди", "найти", "поиск", "ищи", "search")
DOWNLOAD_WORDS = ("скачай", "скачать", "download")
URL_RE = re.compile(r"^(https?://\S+|(?:[\w-]+\.)+[^\W\d_]{2,}(?:[/?#]\S*)?)$", re.IGNORECASE)


def load_dotenv(path: Path) -> None:
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and not line.lstrip().startswith("#"):
                os.environ.setdefault(key.strip(), value.strip())


load_dotenv(BASE / ".env")
ALLOWED = {int(x) for x in re.findall(r"\d+", os.getenv("ALLOWED_USERS", ""))}


class Registry:
    """Хранит адреса и страницы под короткими номерами: в кнопку Telegram влезает только 64 байта."""

    def __init__(self, size: int):
        self.size = size
        self.items: OrderedDict[int, object] = OrderedDict()
        self.ids = itertools.count(1)

    def add(self, item) -> int:
        key = next(self.ids)
        self.items[key] = item
        if len(self.items) > self.size:
            self.items.popitem(last=False)
        return key

    def get(self, key: int):
        return self.items.get(key)


urls = Registry(20000)
pages = Registry(100)

allowed = Router()
allowed.message.filter(F.from_user.id.in_(ALLOWED))
allowed.callback_query.filter(F.from_user.id.in_(ALLOWED))
stranger = Router()


# ---------- оформление ----------

def split_text(text: str) -> list[str]:
    """Режет текст на куски по строкам так, чтобы после html.escape кусок влез в сообщение."""
    chunks, cur = [], ""
    for line in text.splitlines():
        while len(html.escape(line)) > CHUNK:  # очень длинная строка
            cut = CHUNK
            while len(html.escape(line[:cut])) > CHUNK:
                cut -= 100
            chunks += [cur] if cur else []
            chunks.append(line[:cut])
            cur, line = "", line[cut:]
        if len(html.escape(cur + "\n" + line)) > CHUNK:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur or not chunks:
        chunks.append(cur)
    return chunks


def domain(url: str) -> str:
    return urlparse(url).netloc.removeprefix("www.")


def number_buttons(ids: list[int], start: int = 1) -> list[list[InlineKeyboardButton]]:
    buttons = [InlineKeyboardButton(text=str(start + i), callback_data=f"o:{uid}") for i, uid in enumerate(ids)]
    return [buttons[i : i + 5] for i in range(0, len(buttons), 5)]


def page_view(pid: int, n: int) -> tuple[str, InlineKeyboardMarkup]:
    page, chunks, lid = pages.get(pid)
    head = f"🌐 <b>{html.escape(page.title[:200])}</b>\n<i>{html.escape(domain(page.url))}</i>"
    if len(chunks) > 1:
        head += f" · стр. {n + 1}/{len(chunks)}"
    body = html.escape(chunks[n]) or "<i>(на странице нет текста — возможно, сайт работает только с JavaScript)</i>"

    nav = []
    if n > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"p:{pid}:{n - 1}"))
    if n < len(chunks) - 1:
        nav.append(InlineKeyboardButton(text="Дальше ▶️", callback_data=f"p:{pid}:{n + 1}"))
    tools = [InlineKeyboardButton(text="📥 Скачать файлом", callback_data=f"d:{lid}")]
    if page.links:
        tools.insert(0, InlineKeyboardButton(text=f"🔗 Ссылки ({len(page.links)})", callback_data=f"l:{pid}:0"))
    rows = [nav] if nav else []
    rows.append(tools)
    return f"{head}\n\n{body}", InlineKeyboardMarkup(inline_keyboard=rows)


def links_view(pid: int, n: int) -> tuple[str, InlineKeyboardMarkup]:
    page, _, _ = pages.get(pid)
    part = page.links[n * LINKS_PER_PAGE : (n + 1) * LINKS_PER_PAGE]
    total = (len(page.links) + LINKS_PER_PAGE - 1) // LINKS_PER_PAGE
    lines = [f"🔗 <b>Ссылки:</b> {html.escape(page.title[:200])} · {n + 1}/{total}\n"]
    start = n * LINKS_PER_PAGE + 1
    for i, (text, url) in enumerate(part, start):
        lines.append(f"{i}. {html.escape(text[:100])} <i>({html.escape(domain(url))})</i>")
    rows = number_buttons([urls.add(url) for _, url in part], start)
    nav = []
    if n > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"l:{pid}:{n - 1}"))
    nav.append(InlineKeyboardButton(text="📄 К тексту", callback_data=f"p:{pid}:0"))
    if n < total - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"l:{pid}:{n + 1}"))
    rows.append(nav)
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def edit(cb: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:  # сообщение не изменилось или слишком старое
        await cb.message.answer(text, reply_markup=markup)


# ---------- действия ----------

async def do_search(message: Message, http: aiohttp.ClientSession, query: str) -> None:
    status = await message.answer(f"🔎 Ищу: <i>{html.escape(query)}</i>…")
    results = await web.search(http, query)
    if not results:
        await status.edit_text("Ничего не нашлось (или поисковик не ответил). Попробуйте другой запрос.")
        return
    lines = [f"🔎 <b>{html.escape(query)}</b>\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"<b>{i}. {html.escape(r.title)}</b>\n<i>{html.escape(domain(r.url))}</i>")
        if r.snippet:
            lines.append(html.escape(r.snippet[:200]))
        lines.append("")
    lines.append("Нажмите номер, чтобы открыть.")
    rows = number_buttons([urls.add(r.url) for r in results])
    await status.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def do_open(message: Message, http: aiohttp.ClientSession, url: str, as_file: bool = False) -> None:
    status = await message.answer(f"⏳ {'Скачиваю' if as_file else 'Открываю'} {html.escape(domain(url))}…")
    try:
        result = await web.fetch(http, url, as_file)
    except web.TooBig as e:
        size = e.args[0] / 1024 / 1024
        await status.edit_text(f"Файл слишком большой ({size:.0f} МБ). Бот может прислать максимум 50 МБ.\n{html.escape(url)}")
        return
    except aiohttp.ClientResponseError as e:
        await status.edit_text(f"Сайт ответил ошибкой {e.status}.")
        return
    except (aiohttp.ClientError, TimeoutError, ValueError) as e:
        await status.edit_text(f"Не получилось открыть: {html.escape(type(e).__name__)}.")
        return

    if isinstance(result, web.File):
        await status.edit_text(f"📤 Отправляю {html.escape(result.name)} ({len(result.data) / 1024:.0f} КБ)…")
        await message.answer_document(BufferedInputFile(result.data, result.name), caption=html.escape(result.url[:1000]))
        await status.delete()
        return

    pid = pages.add((result, split_text(result.text), urls.add(result.url)))
    text, markup = page_view(pid, 0)
    await status.edit_text(text, reply_markup=markup)


# ---------- обработчики ----------

HELP = (
    "📟 <b>Текстовый браузер</b>\n\n"
    "• <code>найди нокиа 6300</code> — поиск (или просто напишите запрос)\n"
    "• <code>example.com</code> или полная ссылка — открыть сайт текстом\n"
    "• <code>скачай https://…/game.jar</code> — прислать файлом\n\n"
    "Под страницей есть кнопки: листать текст, список ссылок, скачать.\n"
    "Файлы (jar, pdf, zip, картинки…) бот присылает сам, до 50 МБ."
)


@allowed.message(CommandStart())
@allowed.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


@allowed.message(F.text)
async def on_text(message: Message, http: aiohttp.ClientSession) -> None:
    text = message.text.strip()
    first, _, rest = text.partition(" ")
    word = first.lower().rstrip(":,")
    as_file = word in DOWNLOAD_WORDS
    if word in SEARCH_WORDS or as_file:
        text = rest.strip()
    if not text:
        await message.answer(HELP)
    elif URL_RE.match(text):
        url = text if text.lower().startswith(("http://", "https://")) else "https://" + text
        await do_open(message, http, url, as_file)
    elif as_file:
        await message.answer("Чтобы скачать, нужна ссылка: <code>скачай https://…</code>")
    else:
        await do_search(message, http, text)


@allowed.message()
async def on_other(message: Message) -> None:
    await message.answer("Пришлите текстом запрос или ссылку.")


@allowed.callback_query(F.data.startswith("o:") | F.data.startswith("d:"))
async def on_open(cb: CallbackQuery, http: aiohttp.ClientSession) -> None:
    action, key = cb.data.split(":")
    url = urls.get(int(key))
    if url is None:
        await cb.answer("Ссылка устарела, повторите поиск.", show_alert=True)
        return
    await cb.answer()
    await do_open(cb.message, http, url, as_file=action == "d")


@allowed.callback_query(F.data.startswith("p:") | F.data.startswith("l:"))
async def on_page(cb: CallbackQuery) -> None:
    action, pid, n = cb.data.split(":")
    if pages.get(int(pid)) is None:
        await cb.answer("Страница устарела, откройте её заново.", show_alert=True)
        return
    await cb.answer()
    view = page_view if action == "p" else links_view
    await edit(cb, *view(int(pid), int(n)))


@stranger.message()
async def on_stranger(message: Message) -> None:
    await message.answer(
        "⛔ Это личный бот.\n"
        f"Если вы владелец, впишите свой ID в файл .env: <code>ALLOWED_USERS={message.from_user.id}</code>\n"
        "и перезапустите бота."
    )


@stranger.callback_query()
async def on_stranger_cb(cb: CallbackQuery) -> None:
    await cb.answer("⛔ Это личный бот.", show_alert=True)


async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Не задан BOT_TOKEN: создайте файл .env по образцу .env.example")
    if not ALLOWED:
        logging.warning("ALLOWED_USERS пуст: бот никому не отвечает. Напишите ему, он подскажет ваш ID.")
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    dp = Dispatcher()
    dp.include_routers(allowed, stranger)
    async with web.new_session() as http:
        await dp.start_polling(bot, http=http)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
