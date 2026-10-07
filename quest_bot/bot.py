"""Telegram-бот с текстовым квестом. Запуск: python bot.py"""
from __future__ import annotations

import asyncio
import html
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from engine import Story
from storage import Storage

BASE = Path(__file__).parent


def load_dotenv(path: Path) -> None:
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and not line.lstrip().startswith("#"):
                os.environ.setdefault(key.strip(), value.strip())


load_dotenv(BASE / ".env")
story = Story(BASE / "story.yaml")
storage = Storage(os.getenv("DB_PATH", str(BASE / "quest.db")))
dp = Dispatcher()


def render(user_id: int, scene_id: str, flags: set[str]) -> tuple[str, InlineKeyboardMarkup]:
    scene = story.scenes[scene_id]
    text = scene.text
    if scene.ending:
        opened = storage.endings(user_id)
        text += (
            f"\n\n🏁 <b>Концовка: {html.escape(scene.ending)}</b>\n"
            f"Открыто концовок: {len(opened)}/{len(story.endings)}"
        )
        rows = [
            [InlineKeyboardButton(text="🔄 Играть заново", callback_data="restart")],
            [InlineKeyboardButton(text="📜 Мои концовки", callback_data="endings")],
        ]
    else:
        rows = [
            [InlineKeyboardButton(text=c.text, callback_data=f"go:{scene_id}:{i}")]
            for i, c in story.visible_choices(scene_id, flags)
        ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def send_scene(message: Message, user_id: int, scene_id: str, flags: set[str]) -> None:
    text, markup = render(user_id, scene_id, flags)
    await message.answer(text, reply_markup=markup)


async def new_game(message: Message, user_id: int) -> None:
    storage.save(user_id, story.start, set())
    await message.answer(f"🕯 <b>{html.escape(story.title)}</b>")
    await send_scene(message, user_id, story.start, set())


def endings_text(user_id: int) -> str:
    opened = storage.endings(user_id)
    lines = [f"📜 <b>Концовки: {len(opened)}/{len(story.endings)}</b>"]
    for sid, name in story.endings.items():
        lines.append(f"✅ {html.escape(name)}" if sid in opened else "🔒 ???")
    return "\n".join(lines)


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    uid = message.from_user.id
    state = storage.get(uid)
    if state is None or story.scenes.get(state[0]) is None or story.scenes[state[0]].ending:
        await new_game(message, uid)
    else:
        await message.answer("Продолжаем с того места, где вы остановились… (/restart — начать заново)")
        await send_scene(message, uid, *state)


@dp.message(Command("restart"))
async def cmd_restart(message: Message) -> None:
    await new_game(message, message.from_user.id)


@dp.message(Command("endings"))
async def cmd_endings(message: Message) -> None:
    await message.answer(endings_text(message.from_user.id))


@dp.callback_query(F.data.startswith("go:"))
async def on_choice(cb: CallbackQuery) -> None:
    uid = cb.from_user.id
    _, scene_id, idx = cb.data.split(":")
    state = storage.get(uid)
    result = story.choose(scene_id, state[1], int(idx)) if state and state[0] == scene_id else None
    if result is None:
        await cb.answer("Этот выбор уже в прошлом…")
        return

    choice = story.scenes[scene_id].choices[int(idx)]
    new_scene, new_flags = result
    storage.save(uid, new_scene, new_flags)
    if story.scenes[new_scene].ending:
        storage.add_ending(uid, new_scene)

    # Убираем кнопки у старого сообщения и дописываем, что выбрал игрок.
    try:
        await cb.message.edit_text(
            f"{cb.message.html_text}\n\n<i>➤ {html.escape(choice.text)}</i>", reply_markup=None
        )
    except TelegramBadRequest:
        pass
    await cb.answer()
    await send_scene(cb.message, uid, new_scene, new_flags)


@dp.callback_query(F.data == "restart")
async def on_restart(cb: CallbackQuery) -> None:
    await cb.answer()
    await new_game(cb.message, cb.from_user.id)


@dp.callback_query(F.data == "endings")
async def on_endings(cb: CallbackQuery) -> None:
    await cb.answer()
    await cb.message.answer(endings_text(cb.from_user.id))


async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Не задан BOT_TOKEN: создайте файл .env по образцу .env.example")
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
