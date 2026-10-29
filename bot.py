"""Книжный год — Telegram-бот для учёта прочитанных книг в общем чате."""
import asyncio
import html
import logging
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, CallbackQuery, ForceReply, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ["BOT_TOKEN"]
DB_PATH = Path(os.getenv("DB_PATH", Path(__file__).with_name("books.db")))
GENRES = [g.strip() for g in os.getenv(
    "GENRES", "Роман,Драма,Нон-фикшн,Поэзия,Фантастика,Детектив,Другое").split(",") if g.strip()]
ALLOWED_CHAT_ID = os.getenv("ALLOWED_CHAT_ID", "").strip()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ---------- база ----------
def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS books (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                user_name  TEXT    NOT NULL,
                author     TEXT,
                title      TEXT    NOT NULL,
                genre      TEXT,
                recommend  INTEGER,          -- 1 стоит, 0 не стоит, NULL не указано
                created_at TEXT    NOT NULL
            )""")
        con.execute("""
            CREATE TABLE IF NOT EXISTS genres (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                key  TEXT NOT NULL UNIQUE   -- name.casefold(): SQLite не умеет без регистра для кириллицы
            )""")
        if con.execute("SELECT COUNT(*) FROM genres").fetchone()[0] == 0:
            con.executemany("INSERT INTO genres (name, key) VALUES (?, ?)",
                            [(g, g.casefold()) for g in GENRES])


def all_genres() -> list[sqlite3.Row]:
    with db() as con:
        # «Другое» всегда в конце, остальные — в порядке добавления
        return con.execute(
            "SELECT * FROM genres ORDER BY (name = 'Другое'), id").fetchall()


def add_genre(name: str) -> str:
    """Добавляет жанр (без учёта регистра) и возвращает его каноничное написание."""
    with db() as con:
        con.execute("INSERT OR IGNORE INTO genres (name, key) VALUES (?, ?)", (name, name.casefold()))
        return con.execute("SELECT name FROM genres WHERE key = ?", (name.casefold(),)).fetchone()[0]


def year_prefix() -> str:
    return f"{datetime.now().year}-"


def book_line(b: sqlite3.Row, with_user: bool = False) -> str:
    name = html.escape(b["title"])
    if b["author"]:
        name = f"{html.escape(b['author'])} — {name}"
    extra = []
    if b["genre"]:
        extra.append(html.escape(b["genre"]))
    if b["recommend"] is not None:
        extra.append("👍" if b["recommend"] else "👎")
    line = name + (f" <i>({', '.join(extra)})</i>" if extra else "")
    if with_user:
        line += f" — {html.escape(b['user_name'])}"
    return line


def parse_book(text: str) -> tuple[str | None, str]:
    """'Автор — Название' -> (author, title). Без тире — только название."""
    parts = re.split(r"\s+[—–-]\s+", text.strip(), maxsplit=1)
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0].strip(), parts[1].strip()
    return None, text.strip()


# ---------- клавиатуры ----------
def genre_kb(book_id: int):
    kb = InlineKeyboardBuilder()
    for g in all_genres():
        kb.button(text=g["name"], callback_data=f"g:{book_id}:{g['id']}")
    kb.button(text="➕ Новый жанр", callback_data=f"gn:{book_id}")
    kb.adjust(3)
    return kb.as_markup()


def rec_kb(book_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="👍 Стоит прочитать", callback_data=f"r:{book_id}:1")
    kb.button(text="👎 Не стоит", callback_data=f"r:{book_id}:0")
    kb.adjust(2)
    return kb.as_markup()


# ---------- обработчики ----------
open_router = Router()   # работает в любом чате
router = Router()        # только в разрешённом чате
if ALLOWED_CHAT_ID:
    router.message.filter(F.chat.id == int(ALLOWED_CHAT_ID))
    router.callback_query.filter(F.message.chat.id == int(ALLOWED_CHAT_ID))


@open_router.message(Command("chatid"))
async def cmd_chatid(m: Message):
    await m.reply(f"ID этого чата: <code>{m.chat.id}</code>")


@router.message(Command("start", "help"))
async def cmd_help(m: Message):
    await m.reply(
        "📚 <b>Книжный год</b>\n\n"
        "/add Автор — Название — записать прочитанную книгу\n"
        "/stats — сколько прочитано в этом году\n"
        "/list — все книги этого года\n"
        "/recommend — что стоит прочитать (👍)\n"
        "/undo — удалить свою последнюю запись")


@router.message(Command("add"))
async def cmd_add(m: Message, command: CommandObject):
    if not command.args:
        await m.reply("Напиши так: <code>/add Чехов — Вишнёвый сад</code>")
        return
    author, title = parse_book(command.args)
    with db() as con:
        cur = con.execute(
            "INSERT INTO books (chat_id, user_id, user_name, author, title, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (m.chat.id, m.from_user.id, m.from_user.first_name, author, title,
             datetime.now().isoformat(timespec="seconds")))
        book_id = cur.lastrowid
    await m.reply(f"📖 {html.escape(title)}\nКакой жанр?", reply_markup=genre_kb(book_id))


async def owned_book(cb: CallbackQuery, book_id: int) -> sqlite3.Row | None:
    with db() as con:
        b = con.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if b is None:
        await cb.answer("Запись не найдена", show_alert=True)
        return None
    if b["user_id"] != cb.from_user.id:
        await cb.answer("Это не твоя книга 🙂", show_alert=True)
        return None
    return b


@router.callback_query(F.data.startswith("g:"))
async def on_genre(cb: CallbackQuery):
    _, book_id, genre_id = cb.data.split(":")
    b = await owned_book(cb, int(book_id))
    if not b:
        return
    with db() as con:
        row = con.execute("SELECT name FROM genres WHERE id = ?", (int(genre_id),)).fetchone()
    genre = row["name"] if row else "Другое"
    await set_genre_and_ask_rec(cb.message, b, genre)
    await cb.answer()


async def set_genre_and_ask_rec(book_msg: Message, b: sqlite3.Row, genre: str) -> None:
    with db() as con:
        con.execute("UPDATE books SET genre = ? WHERE id = ?", (genre, b["id"]))
    await book_msg.edit_text(
        f"📖 {html.escape(b['title'])} — {html.escape(genre)}\nСтоит прочитать?",
        reply_markup=rec_kb(b["id"]))


# (chat_id, user_id) -> (book_id, сообщение с кнопками, сообщение-вопрос)
pending_genre: dict[tuple[int, int], tuple[int, Message, Message]] = {}


@router.callback_query(F.data.startswith("gn:"))
async def on_new_genre(cb: CallbackQuery):
    b = await owned_book(cb, int(cb.data.split(":")[1]))
    if not b:
        return
    prompt = await cb.message.answer(
        f"{html.escape(cb.from_user.first_name)}, напиши название нового жанра ответом на это сообщение.",
        reply_markup=ForceReply(selective=True, input_field_placeholder="Например: Пьеса"))
    pending_genre[(cb.message.chat.id, cb.from_user.id)] = (b["id"], cb.message, prompt)
    await cb.answer()


@router.message(F.text & ~F.text.startswith("/") & F.reply_to_message)
async def on_genre_text(m: Message):
    key = (m.chat.id, m.from_user.id)
    if key not in pending_genre:
        return
    book_id, book_msg, prompt = pending_genre[key]
    if m.reply_to_message.message_id != prompt.message_id:
        return
    name = m.text.strip()[:40]
    if not name:
        return
    del pending_genre[key]
    genre = add_genre(name)
    with db() as con:
        b = con.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if b is None:
        await m.reply("Книга уже удалена, но жанр сохранил.")
        return
    await set_genre_and_ask_rec(book_msg, b, genre)
    try:
        await prompt.delete()
    except Exception:
        pass


@router.callback_query(F.data.startswith("r:"))
async def on_rec(cb: CallbackQuery):
    _, book_id, val = cb.data.split(":")
    b = await owned_book(cb, int(book_id))
    if not b:
        return
    with db() as con:
        con.execute("UPDATE books SET recommend = ? WHERE id = ?", (int(val), b["id"]))
        b = con.execute("SELECT * FROM books WHERE id = ?", (b["id"],)).fetchone()
        n = con.execute(
            "SELECT COUNT(*) FROM books WHERE chat_id = ? AND user_id = ? AND created_at LIKE ?",
            (b["chat_id"], b["user_id"], year_prefix() + "%")).fetchone()[0]
    await cb.message.edit_text(f"✅ Записал: {book_line(b)}\nУ тебя {n}-я книга в этом году.")
    await cb.answer()


@router.message(Command("stats"))
async def cmd_stats(m: Message):
    y = year_prefix() + "%"
    with db() as con:
        people = con.execute(
            "SELECT MAX(user_name) AS name, COUNT(*) AS n FROM books "
            "WHERE chat_id = ? AND created_at LIKE ? GROUP BY user_id ORDER BY n DESC",
            (m.chat.id, y)).fetchall()
        genres = con.execute(
            "SELECT COALESCE(genre, 'без жанра') AS g, COUNT(*) AS n FROM books "
            "WHERE chat_id = ? AND created_at LIKE ? GROUP BY g ORDER BY n DESC",
            (m.chat.id, y)).fetchall()
    if not people:
        await m.reply("В этом году пока ни одной книги. Начни с /add 📚")
        return
    total = sum(p["n"] for p in people)
    lines = [f"📊 <b>{datetime.now().year}: всего {total}</b>"]
    lines += [f"• {html.escape(p['name'])} — {p['n']}" for p in people]
    lines.append("\n<b>По жанрам:</b>")
    lines += [f"• {html.escape(g['g'])} — {g['n']}" for g in genres]
    await m.reply("\n".join(lines))


@router.message(Command("list"))
async def cmd_list(m: Message):
    with db() as con:
        rows = con.execute(
            "SELECT * FROM books WHERE chat_id = ? AND created_at LIKE ? ORDER BY created_at",
            (m.chat.id, year_prefix() + "%")).fetchall()
    if not rows:
        await m.reply("Список пока пуст.")
        return
    text = "\n".join(f"{i}. {book_line(b, with_user=True)}" for i, b in enumerate(rows, 1))
    await m.reply(f"📚 <b>Прочитано в {datetime.now().year}:</b>\n{text}")


@router.message(Command("recommend"))
async def cmd_recommend(m: Message):
    with db() as con:
        rows = con.execute(
            "SELECT * FROM books WHERE chat_id = ? AND recommend = 1 ORDER BY created_at DESC",
            (m.chat.id,)).fetchall()
    if not rows:
        await m.reply("Пока никто ничего не посоветовал 👍")
        return
    text = "\n".join(f"• {book_line(b, with_user=True)}" for b in rows)
    await m.reply(f"👍 <b>Стоит прочитать:</b>\n{text}")


@router.message(Command("undo"))
async def cmd_undo(m: Message):
    with db() as con:
        b = con.execute(
            "SELECT * FROM books WHERE chat_id = ? AND user_id = ? ORDER BY id DESC LIMIT 1",
            (m.chat.id, m.from_user.id)).fetchone()
        if b:
            con.execute("DELETE FROM books WHERE id = ?", (b["id"],))
    await m.reply(f"🗑 Удалил: {book_line(b)}" if b else "У тебя нет записей.")


async def main():
    init_db()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    await bot.set_my_commands([
        BotCommand(command="add", description="Записать книгу: Автор — Название"),
        BotCommand(command="stats", description="Сколько прочитано в этом году"),
        BotCommand(command="list", description="Все книги этого года"),
        BotCommand(command="recommend", description="Что стоит прочитать"),
        BotCommand(command="undo", description="Удалить свою последнюю запись"),
    ])
    dp = Dispatcher()
    dp.include_routers(open_router, router)
    # drop_pending_updates=False — подхватываем сообщения, пришедшие пока ПК был выключен
    await dp.start_polling(bot, drop_pending_updates=False)


if __name__ == "__main__":
    asyncio.run(main())
