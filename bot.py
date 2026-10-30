"""Книжный год — Telegram-бот для учёта прочитанных книг в общем чате."""
import asyncio
import html
import logging
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import JOIN_TRANSITION, LEAVE_TRANSITION, ChatMemberUpdatedFilter, Command, CommandObject
from aiogram.types import BotCommand, CallbackQuery, ChatMemberUpdated, ForceReply, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ["BOT_TOKEN"]
DB_PATH = Path(os.getenv("DB_PATH", Path(__file__).with_name("books.db")))
GENRES = [g.strip() for g in os.getenv(
    "GENRES", "Роман,Драма,Нон-фикшн,Поэзия,Фантастика,Детектив,Другое").split(",") if g.strip()]
# Список разрешённых чатов через запятую. Пусто — бот работает в любом чате.
# (ALLOWED_CHAT_ID — старое имя, поддерживается для совместимости)
GOOGLE_BOOKS_KEY = os.getenv("GOOGLE_BOOKS_KEY", "").strip()   # без ключа Google быстро отвечает 429
ADMIN_ID = int(os.getenv("ADMIN_ID") or 0)   # твой Telegram user id — узнать командой /myid
ALLOWED_CHAT_IDS = {int(x) for x in (os.getenv("ALLOWED_CHAT_IDS") or os.getenv("ALLOWED_CHAT_ID") or "")
                    .replace(" ", "").split(",") if x}

# ---------- релизы ----------
# Новый релиз — новая запись В НАЧАЛО списка. После перезапуска бот один раз разошлёт её во все активные чаты.
RELEASES = [
    ("1.2.1", "2026-10-30", [
        "🔎 Поиск книг в Google Books работает стабильнее",
        "🔧 Книги больше не теряются, когда группа становится супергруппой (например, после назначения бота админом)",
        "🔎 Бот может попросить уточнить книги, записанные до версии 1.2: выбери вариант — и у карточки появится ✏️",
    ]),
    ("1.2", "2026-10-30", [
        "✍️ /add теперь спрашивает автора и название по очереди (одной строкой тоже можно)",
        "🔎 Бот находит книгу в Google Books и предлагает правильное написание",
        "😐 Третья реакция — «Нормально», если книга ни туда ни сюда",
        "✏️ Кнопка на карточке книги — исправить автора, жанр, реакцию или удалить запись",
        "📚 /list показывает только твои книги",
        "🏆 /top — самые читаемые книги среди всех пользователей бота (анонимно)",
        "🧹 Бот убирает за собой команды — в чате остаются только карточки книг",
        "💡 /feedback — предложить идею или сообщить о проблеме",
        "🆕 /news — что нового в боте",
    ]),
    ("1.1", "2026-10-29", [
        "➕ Свои жанры: кнопка «Новый жанр» при записи книги",
        "👥 Бота можно добавить в любую группу или писать ему в личку",
    ]),
    ("1.0", "2026-10-29", [
        "📚 Первая версия: /add, жанр и оценка книги, /stats, /list, /recommend, /undo",
    ]),
]
VERSION = RELEASES[0][0]


def release_text(rel) -> str:
    ver, date, items = rel
    d = f"{date[8:10]}.{date[5:7]}.{date[:4]}"
    return f"🆕 <b>Книжный год {ver}</b> · {d}\n" + "\n".join(f"• {html.escape(i)}" for i in items)


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
                recommend  INTEGER,          -- устарело, см. rating
                created_at TEXT    NOT NULL
            )""")
        con.execute("""
            CREATE TABLE IF NOT EXISTS chats (
                chat_id    INTEGER PRIMARY KEY,
                title      TEXT,
                type       TEXT,
                first_seen TEXT NOT NULL,
                last_seen  TEXT NOT NULL,
                removed    INTEGER NOT NULL DEFAULT 0   -- 1 — бота удалили из чата
            )""")
        con.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                user_name  TEXT    NOT NULL,
                text       TEXT    NOT NULL,
                created_at TEXT    NOT NULL,
                done_at    TEXT                 -- NULL — открыто
            )""")
        chat_cols = [r["name"] for r in con.execute("PRAGMA table_info(chats)")]
        if "notified_version" not in chat_cols:
            con.execute("ALTER TABLE chats ADD COLUMN notified_version TEXT")   # какой релиз уже анонсирован
        # Миграция v1: реакция 1/0/-1 (👍/😐/👎) вместо recommend 1/0, ключ книги и признак проверки
        if con.execute("PRAGMA user_version").fetchone()[0] < 1:
            cols = [r["name"] for r in con.execute("PRAGMA table_info(books)")]
            if "rating" not in cols:
                con.execute("ALTER TABLE books ADD COLUMN rating INTEGER")   # 1 👍, 0 😐, -1 👎, NULL — нет
                con.execute("UPDATE books SET rating = CASE recommend WHEN 1 THEN 1 WHEN 0 THEN -1 END")
            if "book_key" not in cols:
                con.execute("ALTER TABLE books ADD COLUMN book_key TEXT")    # для общей статистики
                con.execute("ALTER TABLE books ADD COLUMN verified INTEGER NOT NULL DEFAULT 0")
            for r in con.execute("SELECT id, author, title FROM books").fetchall():
                con.execute("UPDATE books SET book_key = ? WHERE id = ?",
                            (book_key(r["author"], r["title"]), r["id"]))
            con.execute("PRAGMA user_version = 1")
        # Миграция: раньше жанры были общими (без chat_id) — раздаём их каждому чату с книгами
        old_cols = [r["name"] for r in con.execute("PRAGMA table_info(genres)")]
        if old_cols and "chat_id" not in old_cols:
            con.execute("ALTER TABLE genres RENAME TO genres_old")
        con.execute("""
            CREATE TABLE IF NOT EXISTS genres (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                name    TEXT    NOT NULL,
                key     TEXT    NOT NULL,   -- name.casefold(): SQLite не умеет без регистра для кириллицы
                UNIQUE (chat_id, key)
            )""")
        if old_cols and "chat_id" not in old_cols:
            con.execute("""
                INSERT INTO genres (chat_id, name, key)
                SELECT c.chat_id, g.name, g.key
                FROM (SELECT DISTINCT chat_id FROM books) c, genres_old g ORDER BY c.chat_id, g.id""")
            con.execute("DROP TABLE genres_old")


def all_genres(chat_id: int) -> list[sqlite3.Row]:
    with db() as con:
        if con.execute("SELECT 1 FROM genres WHERE chat_id = ? LIMIT 1", (chat_id,)).fetchone() is None:
            # новый чат — заводим стартовый набор жанров
            con.executemany("INSERT INTO genres (chat_id, name, key) VALUES (?, ?, ?)",
                            [(chat_id, g, g.casefold()) for g in GENRES])
        # «Другое» всегда в конце, остальные — в порядке добавления
        return con.execute(
            "SELECT * FROM genres WHERE chat_id = ? ORDER BY (name = 'Другое'), id",
            (chat_id,)).fetchall()


def add_genre(chat_id: int, name: str) -> str:
    """Добавляет жанр в чат (без учёта регистра) и возвращает его каноничное написание."""
    with db() as con:
        con.execute("INSERT OR IGNORE INTO genres (chat_id, name, key) VALUES (?, ?, ?)",
                    (chat_id, name, name.casefold()))
        return con.execute("SELECT name FROM genres WHERE chat_id = ? AND key = ?",
                           (chat_id, name.casefold())).fetchone()[0]


def touch_chat(chat, removed: int = 0) -> None:
    """Запоминает чат и время последней активности в нём."""
    now = datetime.now().isoformat(timespec="seconds")
    title = chat.title or chat.full_name or str(chat.id)
    with db() as con:
        con.execute("""
            INSERT INTO chats (chat_id, title, type, first_seen, last_seen, removed, notified_version)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title, type = excluded.type,
                last_seen = excluded.last_seen, removed = excluded.removed""",
            (chat.id, title, chat.type, now, now, removed, VERSION))   # новичкам старые анонсы не шлём


def norm(s: str | None) -> str:
    s = (s or "").casefold().replace("ё", "е")
    return " ".join(re.sub(r"[^\w\s]", " ", s).split())


def author_key(author: str | None) -> str:
    """«Антон Павлович Чехов», «А. П. Чехов», «чехов» -> «чехов».
    Отбрасываем инициалы и отчества, из оставшегося берём последнее слово (фамилию)."""
    words = [w for w in norm(author).split()
             if len(w) > 2 and not re.search(r"(вич|вна|ична|ична)$", w)]
    return words[-1] if words else ""


def book_key(author: str | None, title: str) -> str:
    """Единый ключ книги для статистики по всем чатам: фамилия + название без регистра, ё/е и пунктуации."""
    return f"{author_key(author)}|{norm(title)}"


RATING = {1: "👍", 0: "😐", -1: "👎"}


# ---------- поиск книги (Google Books) ----------
async def search_books(query: str, limit: int = 3) -> list[tuple[str | None, str]]:
    """Возвращает до limit вариантов (автор, название) без повторов. При любой ошибке — []."""
    params = {"q": query, "maxResults": "15", "printType": "books"}
    if GOOGLE_BOOKS_KEY:
        params["key"] = GOOGLE_BOOKS_KEY
    data = None
    for attempt in range(2):   # при 429 (слишком много запросов) — одна повторная попытка
        try:
            timeout = aiohttp.ClientTimeout(total=6)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get("https://www.googleapis.com/books/v1/volumes", params=params) as r:
                    if r.status == 200:
                        data = await r.json()
                        break
                    logging.warning("Google Books: HTTP %s (попытка %s)", r.status, attempt + 1)
                    if r.status != 429:
                        return []
        except Exception as e:
            logging.warning("Google Books недоступен: %s", e)
            return []
        await asyncio.sleep(1.5)
    if data is None:
        return []
    out, seen = [], set()
    for item in data.get("items", []):
        info = item.get("volumeInfo", {})
        title = (info.get("title") or "").strip()
        if not title:
            continue
        authors = info.get("authors") or []
        author = authors[0].strip() if authors else None
        k = book_key(author, title)
        if k in seen:
            continue
        seen.add(k)
        out.append((author, title))
        if len(out) == limit:
            break
    return out


def year_prefix() -> str:
    return f"{datetime.now().year}-"


def book_line(b: sqlite3.Row, with_user: bool = False) -> str:
    name = html.escape(b["title"])
    if b["author"]:
        name = f"{html.escape(b['author'])} — {name}"
    extra = []
    if b["genre"]:
        extra.append(html.escape(b["genre"]))
    if b["rating"] is not None:
        extra.append(RATING.get(b["rating"], ""))
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
def genre_kb(chat_id: int, book_id: int):
    kb = InlineKeyboardBuilder()
    for g in all_genres(chat_id):
        kb.button(text=g["name"], callback_data=f"g:{book_id}:{g['id']}")
    kb.button(text="➕ Новый жанр", callback_data=f"gn:{book_id}")
    kb.adjust(3)
    return kb.as_markup()


def rec_kb(book_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="👍 Стоит", callback_data=f"r:{book_id}:1")
    kb.button(text="😐 Нормально", callback_data=f"r:{book_id}:0")
    kb.button(text="👎 Не стоит", callback_data=f"r:{book_id}:-1")
    kb.adjust(3)
    return kb.as_markup()


def verify_kb(book_id: int, candidates: list[tuple[str | None, str]]):
    kb = InlineKeyboardBuilder()
    for i, (a, t) in enumerate(candidates):
        label = f"{a} — {t}" if a else t
        kb.button(text=label[:60], callback_data=f"v:{book_id}:{i}")
    kb.button(text="✍️ Оставить как написал", callback_data=f"v:{book_id}:-")
    kb.adjust(1)
    return kb.as_markup()


def card_kb(book_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️", callback_data=f"e:{book_id}")
    return kb.as_markup()


def edit_kb(book_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="Автор и название", callback_data=f"ea:{book_id}")
    kb.button(text="Жанр", callback_data=f"eg:{book_id}")
    kb.button(text="Реакция", callback_data=f"er:{book_id}")
    kb.button(text="🗑 Удалить", callback_data=f"ed:{book_id}")
    kb.button(text="↩️ Назад", callback_data=f"ex:{book_id}")
    kb.adjust(3, 2)
    return kb.as_markup()


# ---------- общие помощники ----------
def mention(user) -> str:
    """Упоминание, которое видно как имя и «адресует» ForceReply нужному человеку."""
    return f'<a href="tg://user?id={user.id}">{html.escape(user.first_name)}</a>'


async def drop(msg: Message | None) -> None:
    """Тихо удаляет сообщение. В группе нужно право админа «Удаление сообщений»."""
    if msg is None:
        return
    try:
        await msg.delete()
    except Exception:
        pass


def get_book(book_id: int) -> sqlite3.Row | None:
    with db() as con:
        return con.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()


def book_title_line(b: sqlite3.Row) -> str:
    name = f"{b['author']} — {b['title']}" if b["author"] else b["title"]
    return html.escape(name)


async def advance(card: Message, b: sqlite3.Row) -> None:
    """Ведёт карточку книги к следующему незаполненному шагу, а в конце — к итоговому виду."""
    if b["genre"] is None:
        await card.edit_text(f"📖 {book_title_line(b)}\nКакой жанр?",
                             reply_markup=genre_kb(b["chat_id"], b["id"]))
    elif b["rating"] is None:
        await card.edit_text(f"📖 {book_title_line(b)} — {html.escape(b['genre'])}\nКак тебе книга?",
                             reply_markup=rec_kb(b["id"]))
    else:
        with db() as con:
            n = con.execute(
                "SELECT COUNT(*) FROM books WHERE chat_id = ? AND user_id = ? AND created_at LIKE ? AND id <= ?",
                (b["chat_id"], b["user_id"], b["created_at"][:5] + "%", b["id"])).fetchone()[0]
        await card.edit_text(
            f"✅ {html.escape(b['user_name'])}: {book_line(b)}\n{n}-я книга в {b['created_at'][:4]} году.",
            reply_markup=card_kb(b["id"]))


# (chat_id, user_id) -> чего ждём: {"step": "author"|"title"|"genre", "prompt": Message, ...}
pending: dict[tuple[int, int], dict] = {}
# book_id -> варианты из поиска (живут до перезапуска; после — кнопки работают как «оставить»)
pending_candidates: dict[int, list[tuple[str | None, str]]] = {}


async def ask(chat_msg: Message, user, step: str, text: str, placeholder: str, **data) -> None:
    """Задаёт вопрос, на который человек отвечает реплаем, и запоминает, чего ждём."""
    key = (chat_msg.chat.id, user.id)
    old = pending.get(key)
    if old:
        await drop(old["prompt"])
    prompt = await chat_msg.bot.send_message(
        chat_msg.chat.id, f"{mention(user)}, {text}",
        reply_markup=ForceReply(selective=True, input_field_placeholder=placeholder))
    pending[key] = {"step": step, "prompt": prompt, **data}


# ---------- обработчики ----------
open_router = Router()   # работает в любом чате
router = Router()        # только в разрешённом чате
if ALLOWED_CHAT_IDS:
    router.message.filter(F.chat.id.in_(ALLOWED_CHAT_IDS))
    router.callback_query.filter(F.message.chat.id.in_(ALLOWED_CHAT_IDS))
    router.my_chat_member.filter(F.chat.id.in_(ALLOWED_CHAT_IDS))


@open_router.message(Command("myid"))
async def cmd_myid(m: Message):
    await m.reply(f"Твой user id: <code>{m.from_user.id}</code>")


@open_router.message(Command("admin"))
async def cmd_admin(m: Message):
    if not ADMIN_ID or m.from_user.id != ADMIN_ID:
        return   # для остальных команды как бы нет
    y = year_prefix() + "%"
    with db() as con:
        tot = con.execute("""
            SELECT COUNT(DISTINCT chat_id) AS chats, COUNT(DISTINCT user_id) AS users,
                   COUNT(*) AS books, SUM(created_at LIKE ?) AS books_y FROM books""", (y,)).fetchone()
        known = con.execute(
            "SELECT COUNT(*) AS n, SUM(removed = 0) AS active FROM chats").fetchone()
        rows = con.execute("""
            SELECT c.chat_id, c.title, c.type, c.first_seen, c.last_seen, c.removed,
                   COUNT(b.id) AS books, COUNT(DISTINCT b.user_id) AS users,
                   MAX(b.created_at) AS last_book
            FROM chats c LEFT JOIN books b ON b.chat_id = c.chat_id
            GROUP BY c.chat_id ORDER BY c.last_seen DESC LIMIT 20""").fetchall()
    lines = [
        f"🛠 <b>Админка</b> · версия {VERSION}",
        f"Чатов с ботом: {known['n']} (активных {known['active'] or 0})",
        f"Чатов с книгами: {tot['chats']}, читателей: {tot['users']}",
        f"Книг всего: {tot['books']}, в этом году: {tot['books_y'] or 0}",
        "",
        "<b>Последние активные чаты:</b>",
    ]
    kinds = {"private": "личка", "group": "группа", "supergroup": "группа", "channel": "канал"}
    for r in rows:
        mark = " ❌ удалил бота" if r["removed"] else ""
        last_book = r["last_book"][:10] if r["last_book"] else "—"
        lines.append(
            f"• {html.escape(r['title'] or str(r['chat_id']))} ({kinds.get(r['type'], r['type'])}){mark}\n"
            f"   книг {r['books']}, читателей {r['users']}, с {r['first_seen'][:10]}, "
            f"посл. книга {last_book}, активность {r['last_seen'][:10]}")
    await m.reply("\n".join(lines))


def migrate_chat(old: int, new: int) -> int:
    """Группа стала супергруппой и сменила ID — переносим всё на новый. Возвращает число книг."""
    with db() as con:
        n = con.execute("UPDATE books SET chat_id = ? WHERE chat_id = ?", (new, old)).rowcount
        con.execute("INSERT OR IGNORE INTO genres (chat_id, name, key) "
                    "SELECT ?, name, key FROM genres WHERE chat_id = ? ORDER BY id", (new, old))
        con.execute("DELETE FROM genres WHERE chat_id = ?", (old,))
        con.execute("UPDATE feedback SET chat_id = ? WHERE chat_id = ?", (new, old))
        con.execute("UPDATE chats SET removed = 1 WHERE chat_id = ?", (old,))
    logging.info("Чат %s переехал в %s, перенесено книг: %s", old, new, n)
    return n


@open_router.message(F.migrate_to_chat_id)
async def on_migrate_to(m: Message):
    migrate_chat(m.chat.id, m.migrate_to_chat_id)


@open_router.message(F.migrate_from_chat_id)
async def on_migrate_from(m: Message):
    migrate_chat(m.migrate_from_chat_id, m.chat.id)


@open_router.message(Command("reverify"))
async def cmd_reverify(m: Message):
    """Админ: предложить авторам уточнить непроверенные книги через Google Books."""
    if not ADMIN_ID or m.from_user.id != ADMIN_ID:
        return
    with db() as con:
        rows = con.execute("""
            SELECT b.* FROM books b JOIN chats c ON c.chat_id = b.chat_id AND c.removed = 0
            WHERE b.verified = 0 ORDER BY b.chat_id, b.id""").fetchall()
    await m.reply(f"🔎 Непроверенных книг: {len(rows)}. Рассылаю уточнения…")
    asked = 0
    for b in rows:
        query = f"{b['author'] or ''} {b['title']}".strip()
        candidates = await search_books(query)
        if not candidates:
            continue
        pending_candidates[b["id"]] = candidates
        try:
            await m.bot.send_message(
                b["chat_id"],
                f'🔎 <a href="tg://user?id={b["user_id"]}">{html.escape(b["user_name"])}</a>, '
                f"уточни книгу «{book_title_line(b)}» — это какая из них?",
                reply_markup=verify_kb(b["id"], candidates))
            asked += 1
        except Exception as e:
            logging.warning("reverify %s: %s", b["id"], e)
        await asyncio.sleep(0.5)
    await m.answer(f"✅ Отправил уточнений: {asked} из {len(rows)} (для остальных Google ничего не нашёл).")


@open_router.message(Command("chatid"))
async def cmd_chatid(m: Message):
    await m.reply(f"ID этого чата: <code>{m.chat.id}</code>")


HELP = ("📚 <b>Книжный год</b>\n\n"
        "/add — записать прочитанную книгу (бот спросит автора и название)\n"
        "   или сразу: /add Автор — Название\n"
        "/stats — сколько прочитано в этом году\n"
        "/list — мои книги этого года\n"
        "/recommend — что стоит прочитать (👍)\n"
        "/top — самые читаемые книги среди всех пользователей бота (анонимно)\n"
        "/undo — удалить свою последнюю запись\n"
        "/feedback — предложить идею или сообщить о проблеме\n"
        "/news — что нового в боте\n\n"
        "✏️ на карточке книги — исправить или удалить свою запись.\n"
        "Работает и в группе, и в личке с ботом. В каждом чате — своя статистика и свои жанры.\n"
        "Чтобы в группе не оставалось команд, сделайте бота админом с правом удалять сообщения.")


@router.message(Command("start", "help"))
async def cmd_help(m: Message):
    await m.answer(HELP)
    await drop(m)


@open_router.my_chat_member(ChatMemberUpdatedFilter(LEAVE_TRANSITION))
async def on_removed(event: ChatMemberUpdated):
    touch_chat(event.chat, removed=1)


@router.my_chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_added(event: ChatMemberUpdated, bot: Bot):
    touch_chat(event.chat)
    await bot.send_message(event.chat.id, "Привет! Я считаю прочитанные книги.\n\n" + HELP)


# ----- добавление книги -----
@router.message(Command("add"))
async def cmd_add(m: Message, command: CommandObject):
    await drop(m)
    if not command.args:
        await ask(m, m.from_user, "author", "✍️ кто автор? Ответь на это сообщение.\n"
                  "Если автора нет — отправь «-».", "Например: Чехов")
        return
    author, title = parse_book(command.args)
    await save_and_search(m, m.from_user, author, title, command.args)


async def save_and_search(chat_msg: Message, user, author: str | None, title: str, query: str) -> None:
    with db() as con:
        cur = con.execute(
            "INSERT INTO books (chat_id, user_id, user_name, author, title, book_key, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_msg.chat.id, user.id, user.first_name, author, title,
             book_key(author, title), datetime.now().isoformat(timespec="seconds")))
        book_id = cur.lastrowid
    card = await chat_msg.bot.send_message(chat_msg.chat.id, f"🔎 Ищу «{html.escape(title)}»…")
    await search_into_card(card, get_book(book_id), query)


async def search_into_card(card: Message, b: sqlite3.Row, query: str) -> None:
    query = re.sub(r"\s+[—–-]\s+", " ", query)
    candidates = await search_books(query)
    if candidates:
        pending_candidates[b["id"]] = candidates
        await card.edit_text(f"🔎 {html.escape(b['user_name'])}, это какая книга?",
                             reply_markup=verify_kb(b["id"], candidates))
    else:
        await advance(card, b)


async def owned_book(cb: CallbackQuery, book_id: int) -> sqlite3.Row | None:
    b = get_book(book_id)
    if b is None:
        await cb.answer("Запись не найдена", show_alert=True)
        return None
    if b["user_id"] != cb.from_user.id:
        await cb.answer("Это не твоя книга 🙂", show_alert=True)
        return None
    return b


@router.callback_query(F.data.startswith("v:"))
async def on_verify(cb: CallbackQuery):
    _, book_id, choice = cb.data.split(":")
    b = await owned_book(cb, int(book_id))
    if not b:
        return
    candidates = pending_candidates.pop(b["id"], [])
    if choice != "-" and choice.isdigit() and int(choice) < len(candidates):
        author, title = candidates[int(choice)]
        with db() as con:
            con.execute("UPDATE books SET author = ?, title = ?, book_key = ?, verified = 1 WHERE id = ?",
                        (author, title, book_key(author, title), b["id"]))
    await advance(cb.message, get_book(b["id"]))
    await cb.answer()


@router.callback_query(F.data.startswith("g:"))
async def on_genre(cb: CallbackQuery):
    _, book_id, genre_id = cb.data.split(":")
    b = await owned_book(cb, int(book_id))
    if not b:
        return
    with db() as con:
        row = con.execute("SELECT name FROM genres WHERE id = ? AND chat_id = ?",
                          (int(genre_id), b["chat_id"])).fetchone()
        con.execute("UPDATE books SET genre = ? WHERE id = ?", (row["name"] if row else "Другое", b["id"]))
    await advance(cb.message, get_book(b["id"]))
    await cb.answer()


@router.callback_query(F.data.startswith("gn:"))
async def on_new_genre(cb: CallbackQuery):
    b = await owned_book(cb, int(cb.data.split(":")[1]))
    if not b:
        return
    await ask(cb.message, cb.from_user, "genre", "напиши название нового жанра ответом на это сообщение.",
              "Например: Пьеса", book_id=b["id"], card=cb.message)
    await cb.answer()


@router.callback_query(F.data.startswith("r:"))
async def on_rec(cb: CallbackQuery):
    _, book_id, val = cb.data.split(":")
    b = await owned_book(cb, int(book_id))
    if not b:
        return
    with db() as con:
        con.execute("UPDATE books SET rating = ? WHERE id = ?", (int(val), b["id"]))
    await advance(cb.message, get_book(b["id"]))
    await cb.answer()


@router.message(F.text & ~F.text.startswith("/") & F.reply_to_message)
async def on_reply(m: Message):
    key = (m.chat.id, m.from_user.id)
    st = pending.get(key)
    if not st or m.reply_to_message.message_id != st["prompt"].message_id:
        return
    text = m.text.strip()
    if not text:
        return
    del pending[key]
    await drop(st["prompt"])
    await drop(m)

    if st["step"] == "author":
        author = None if text in ("-", "—", "–") else text[:100]
        await ask(m, m.from_user, "title", "📖 как называется книга?", "Например: Вишнёвый сад",
                  author=author, book_id=st.get("book_id"), card=st.get("card"))
        return

    if st["step"] == "feedback":
        await save_feedback(m, text)
        return

    if st["step"] == "title":
        author, title = st["author"], text[:200]
        query = f"{author or ''} {title}".strip()
        if st.get("book_id"):   # правка существующей записи
            with db() as con:
                con.execute("UPDATE books SET author = ?, title = ?, book_key = ?, verified = 0 WHERE id = ?",
                            (author, title, book_key(author, title), st["book_id"]))
            b = get_book(st["book_id"])
            if b:
                await search_into_card(st["card"], b, query)
        else:
            await save_and_search(m, m.from_user, author, title, query)
        return

    # step == "genre"
    genre = add_genre(m.chat.id, text[:40])
    with db() as con:
        con.execute("UPDATE books SET genre = ? WHERE id = ?", (genre, st["book_id"]))
    b = get_book(st["book_id"])
    if b:
        await advance(st["card"], b)


# ----- редактирование карточки -----
@router.callback_query(F.data.startswith("e:"))
async def on_edit(cb: CallbackQuery):
    b = await owned_book(cb, int(cb.data.split(":")[1]))
    if not b:
        return
    await cb.message.edit_text(f"✏️ {book_line(b)}\nЧто исправить?", reply_markup=edit_kb(b["id"]))
    await cb.answer()


@router.callback_query(F.data.startswith("ea:"))
async def on_edit_title(cb: CallbackQuery):
    b = await owned_book(cb, int(cb.data.split(":")[1]))
    if not b:
        return
    await ask(cb.message, cb.from_user, "author", "✍️ кто автор? Ответь на это сообщение.\n"
              "Если автора нет — отправь «-».", "Например: Чехов", book_id=b["id"], card=cb.message)
    await cb.answer()


@router.callback_query(F.data.startswith("eg:"))
async def on_edit_genre(cb: CallbackQuery):
    b = await owned_book(cb, int(cb.data.split(":")[1]))
    if not b:
        return
    await cb.message.edit_text(f"📖 {book_title_line(b)}\nКакой жанр?",
                               reply_markup=genre_kb(b["chat_id"], b["id"]))
    await cb.answer()


@router.callback_query(F.data.startswith("er:"))
async def on_edit_rating(cb: CallbackQuery):
    b = await owned_book(cb, int(cb.data.split(":")[1]))
    if not b:
        return
    await cb.message.edit_text(f"📖 {book_title_line(b)}\nКак тебе книга?", reply_markup=rec_kb(b["id"]))
    await cb.answer()


@router.callback_query(F.data.startswith("ed:"))
async def on_edit_delete(cb: CallbackQuery):
    b = await owned_book(cb, int(cb.data.split(":")[1]))
    if not b:
        return
    with db() as con:
        con.execute("DELETE FROM books WHERE id = ?", (b["id"],))
    await cb.answer("Запись удалена")
    try:
        await cb.message.delete()
    except Exception:
        await cb.message.edit_text(f"🗑 Удалено: {book_line(b)}")


@router.callback_query(F.data.startswith("ex:"))
async def on_edit_back(cb: CallbackQuery):
    b = await owned_book(cb, int(cb.data.split(":")[1]))
    if not b:
        return
    await advance(cb.message, b)
    await cb.answer()


# ----- релиз-ноты -----
@router.message(Command("news"))
async def cmd_news(m: Message, command: CommandObject):
    await drop(m)
    rels = RELEASES if (command.args or "").strip().lower() in ("all", "все") else RELEASES[:1]
    text = "\n\n".join(release_text(r) for r in rels)
    if len(rels) == 1 and len(RELEASES) > 1:
        text += "\n\n<i>Вся история: /news all</i>"
    await m.answer(text)


async def announce_release(bot: Bot) -> None:
    """Один раз рассылает свежий релиз во все активные чаты, где его ещё не видели."""
    with db() as con:
        chats = con.execute(
            "SELECT chat_id FROM chats WHERE removed = 0 AND COALESCE(notified_version, '') != ?",
            (VERSION,)).fetchall()
    if not chats:
        return
    text = release_text(RELEASES[0]) + "\n\n<i>Идеи и проблемы — в /feedback</i>"
    sent = 0
    for c in chats:
        removed = 0
        try:
            await bot.send_message(c["chat_id"], text)
            sent += 1
        except Exception as e:
            logging.warning("Анонс в %s не доставлен: %s", c["chat_id"], e)
            if "Forbidden" in type(e).__name__ or "kicked" in str(e) or "not a member" in str(e):
                removed = 1   # бота удалили или заблокировали
        with db() as con:
            con.execute("UPDATE chats SET notified_version = ?, removed = MAX(removed, ?) WHERE chat_id = ?",
                        (VERSION, removed, c["chat_id"]))
        await asyncio.sleep(0.1)   # не упираемся в лимиты Telegram
    logging.info("Анонс %s: доставлен в %s из %s чатов", VERSION, sent, len(chats))


# ----- обратная связь и бэклог -----
@router.message(Command("feedback"))
async def cmd_feedback(m: Message, command: CommandObject):
    await drop(m)
    if command.args:
        await save_feedback(m, command.args.strip())
    else:
        await ask(m, m.from_user, "feedback", "💡 что улучшить или добавить в бота? Ответь на это сообщение.",
                  "Например: хочу ставить цель на год")


async def save_feedback(m: Message, text: str) -> None:
    text = text[:2000]
    with db() as con:
        fid = con.execute(
            "INSERT INTO feedback (chat_id, user_id, user_name, text, created_at) VALUES (?, ?, ?, ?, ?)",
            (m.chat.id, m.from_user.id, m.from_user.first_name, text,
             datetime.now().isoformat(timespec="seconds"))).lastrowid
    await m.bot.send_message(m.chat.id, f"🙏 {mention(m.from_user)}, спасибо! Записал пожелание №{fid}.")
    if ADMIN_ID:
        try:
            await m.bot.send_message(
                ADMIN_ID, f"💡 <b>Новое пожелание №{fid}</b> от {mention(m.from_user)}"
                          f" ({html.escape(m.chat.title or 'личка')}):\n{html.escape(text)}")
        except Exception as e:
            logging.warning("Не смог уведомить админа: %s", e)


@open_router.message(Command("backlog"))
async def cmd_backlog(m: Message):
    if not ADMIN_ID or m.from_user.id != ADMIN_ID:
        return
    with db() as con:
        rows = con.execute("SELECT * FROM feedback WHERE done_at IS NULL ORDER BY id").fetchall()
        done = con.execute("SELECT COUNT(*) FROM feedback WHERE done_at IS NOT NULL").fetchone()[0]
    if not rows:
        await m.reply(f"📋 Бэклог пуст. Сделано: {done}.")
        return
    lines = [f"📋 <b>Бэклог</b>: открыто {len(rows)}, сделано {done}"]
    for r in rows:
        lines.append(f"\n<b>№{r['id']}</b> · {html.escape(r['user_name'])} · {r['created_at'][:10]}\n"
                     f"{html.escape(r['text'])}")
    lines.append("\nЗакрыть: /done номер")
    chunk = ""   # лимит Telegram — 4096 символов; режем по пожеланиям, чтобы не порвать HTML
    for line in lines:
        if len(chunk) + len(line) > 3800:
            await m.answer(chunk)
            chunk = ""
        chunk += line + "\n"
    await m.answer(chunk)


@open_router.message(Command("done"))
async def cmd_done(m: Message, command: CommandObject):
    if not ADMIN_ID or m.from_user.id != ADMIN_ID:
        return
    if not command.args or not command.args.strip().isdigit():
        await m.reply("Напиши так: <code>/done 5</code>")
        return
    fid = int(command.args.strip())
    with db() as con:
        r = con.execute("SELECT * FROM feedback WHERE id = ?", (fid,)).fetchone()
        if r:
            con.execute("UPDATE feedback SET done_at = ? WHERE id = ?",
                        (datetime.now().isoformat(timespec="seconds"), fid))
    if not r:
        await m.reply(f"Пожелания №{fid} нет.")
        return
    note = ""
    try:
        await m.bot.send_message(
            r["user_id"], f"🎉 Твоё пожелание сделано!\n<i>{html.escape(r['text'][:300])}</i>\nСпасибо, что помогаешь боту стать лучше.")
        note = " Автору отправил сообщение."
    except Exception:
        note = " Автору написать не получилось: он не начинал личку с ботом."
    await m.reply(f"✅ №{fid} закрыто.{note}")


# ----- статистика и списки -----
@router.message(Command("stats"))
async def cmd_stats(m: Message):
    await drop(m)
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
        await m.answer("В этом году пока ни одной книги. Начни с /add 📚")
        return
    total = sum(p["n"] for p in people)
    lines = [f"📊 <b>{datetime.now().year}: всего {total}</b>"]
    lines += [f"• {html.escape(p['name'])} — {p['n']}" for p in people]
    lines.append("\n<b>По жанрам:</b>")
    lines += [f"• {html.escape(g['g'])} — {g['n']}" for g in genres]
    await m.answer("\n".join(lines))


@router.message(Command("list"))
async def cmd_list(m: Message):
    await drop(m)
    with db() as con:
        rows = con.execute(
            "SELECT * FROM books WHERE chat_id = ? AND user_id = ? AND created_at LIKE ? ORDER BY created_at",
            (m.chat.id, m.from_user.id, year_prefix() + "%")).fetchall()
    if not rows:
        await m.answer(f"{mention(m.from_user)}, у тебя пока нет книг в этом году. Начни с /add 📚")
        return
    text = "\n".join(f"{i}. {book_line(b)}" for i, b in enumerate(rows, 1))
    await m.answer(f"📚 <b>{html.escape(m.from_user.first_name)}, твои книги {datetime.now().year}:</b>\n{text}")


@router.message(Command("recommend"))
async def cmd_recommend(m: Message):
    await drop(m)
    with db() as con:
        rows = con.execute(
            "SELECT * FROM books WHERE chat_id = ? AND rating = 1 ORDER BY created_at DESC",
            (m.chat.id,)).fetchall()
    if not rows:
        await m.answer("Пока никто ничего не посоветовал 👍")
        return
    text = "\n".join(f"• {book_line(b, with_user=True)}" for b in rows)
    await m.answer(f"👍 <b>Стоит прочитать:</b>\n{text}")


@router.message(Command("top"))
async def cmd_top(m: Message):
    """Самые читаемые книги года среди всех пользователей бота — анонимно."""
    await drop(m)
    y = year_prefix() + "%"
    with db() as con:
        rows = con.execute("""
            SELECT book_key AS key, COUNT(DISTINCT user_id) AS readers,
                   SUM(rating = 1) AS up, SUM(rating = 0) AS mid, SUM(rating = -1) AS down
            FROM books WHERE created_at LIKE ? AND book_key IS NOT NULL
            GROUP BY book_key
            ORDER BY readers DESC, up DESC, MAX(created_at) DESC LIMIT 10""", (y,)).fetchall()
        total_readers = con.execute(
            "SELECT COUNT(DISTINCT user_id) FROM books WHERE created_at LIKE ?", (y,)).fetchone()[0]
        names = {}
        for r in rows:   # написание — из проверенной записи, иначе из самой свежей
            d = con.execute("SELECT author, title FROM books WHERE book_key = ? "
                            "ORDER BY verified DESC, id DESC LIMIT 1", (r["key"],)).fetchone()
            names[r["key"]] = f"{d['author']} — {d['title']}" if d["author"] else d["title"]
    if not rows:
        await m.answer("Пока пусто — запиши первую книгу через /add 📚")
        return
    lines = [f"🏆 <b>Топ {datetime.now().year} среди всех читателей бота</b> ({total_readers} чел.)"]
    for i, r in enumerate(rows, 1):
        marks = " ".join(f"{e}{n}" for e, n in (("👍", r["up"]), ("😐", r["mid"]), ("👎", r["down"])) if n)
        lines.append(f"{i}. {html.escape(names[r['key']])} — читали {r['readers']}" + (f", {marks}" if marks else ""))
    lines.append("\n<i>Статистика анонимная: только книги и цифры, без имён и чатов.</i>")
    await m.answer("\n".join(lines))


@router.message(Command("undo"))
async def cmd_undo(m: Message):
    await drop(m)
    with db() as con:
        b = con.execute(
            "SELECT * FROM books WHERE chat_id = ? AND user_id = ? ORDER BY id DESC LIMIT 1",
            (m.chat.id, m.from_user.id)).fetchone()
        if b:
            con.execute("DELETE FROM books WHERE id = ?", (b["id"],))
    await m.answer(f"🗑 {mention(m.from_user)}, удалил: {book_line(b)}" if b
                   else f"{mention(m.from_user)}, у тебя нет записей.")


async def main():
    init_db()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    await bot.set_my_commands([
        BotCommand(command="add", description="Записать прочитанную книгу"),
        BotCommand(command="list", description="Мои книги этого года"),
        BotCommand(command="stats", description="Сколько прочитано в этом году"),
        BotCommand(command="recommend", description="Что стоит прочитать"),
        BotCommand(command="top", description="Топ книг среди всех читателей"),
        BotCommand(command="undo", description="Удалить свою последнюю запись"),
        BotCommand(command="feedback", description="Предложить идею или сообщить о проблеме"),
        BotCommand(command="news", description="Что нового в боте"),
    ])
    dp = Dispatcher()

    @dp.message.outer_middleware()
    async def track_msg(handler, event: Message, data):
        touch_chat(event.chat)
        return await handler(event, data)

    @dp.callback_query.outer_middleware()
    async def track_cb(handler, event: CallbackQuery, data):
        if event.message:
            touch_chat(event.message.chat)
        return await handler(event, data)

    dp.include_routers(open_router, router)
    asyncio.create_task(announce_release(bot))
    # drop_pending_updates=False — подхватываем сообщения, пришедшие пока ПК был выключен
    await dp.start_polling(bot, drop_pending_updates=False)


if __name__ == "__main__":
    asyncio.run(main())
