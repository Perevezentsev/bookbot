"""Книжный год — Telegram-бот для учёта прочитанных книг в общем чате."""
import asyncio
import html
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta
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
# Wikidata просит представляться: имя бота и ссылка на проект (без личных контактов)
WIKIDATA_UA = "knizhny-god-bot/1.4 (https://github.com/Perevezentsev/bookbot)"
WIKIDATA_TIMEOUT = 4     # секунд на запрос — диалог не должен подвисать
CHAIN_TTL_MIN = 30       # через сколько минут брошенная запись книги убирается из чата
ALLOWED_CHAT_IDS = {int(x) for x in (os.getenv("ALLOWED_CHAT_IDS") or os.getenv("ALLOWED_CHAT_ID") or "")
                    .replace(" ", "").split(",") if x}

# ---------- релизы ----------
# Единственный источник — CHANGELOG.md рядом с ботом (он же текст GitHub Release по тегу vX.Y.Z).
# Новый релиз = новый раздел «## [X.Y.Z] — ГГГГ-ММ-ДД» в начале CHANGELOG.md.
# После перезапуска бот один раз разошлёт его во все активные чаты.
CHANGELOG_PATH = Path(__file__).with_name("CHANGELOG.md")
_RELEASE_HEAD = re.compile(r"^## \[(\d+\.\d+\.\d+)\]\s*[—–-]\s*(\d{4}-\d{2}-\d{2})")


def load_releases(path: Path = CHANGELOG_PATH) -> list[dict]:
    """Разбирает CHANGELOG.md: [{version, date, summary, sections: [(заголовок, [пункты])]}], новые сверху."""
    releases: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        logging.error("Нет %s — /news и анонсы релизов не работают", path.name)
        return releases
    cur = None
    for line in lines:
        m = _RELEASE_HEAD.match(line)
        if m:
            cur = {"version": m[1], "date": m[2], "summary": "", "sections": []}
            releases.append(cur)
        elif cur is None or not line.strip():
            continue
        elif line.startswith("### "):
            cur["sections"].append((line[4:].strip(), []))
        elif line.lstrip().startswith("- ") and cur["sections"]:
            cur["sections"][-1][1].append(line.lstrip()[2:].strip())
        elif not cur["sections"] and not cur["summary"]:
            cur["summary"] = line.strip()
    return releases


RELEASES = load_releases()
VERSION = RELEASES[0]["version"] if RELEASES else "0.0.0"


def md_inline(text: str) -> str:
    """Markdown из CHANGELOG → HTML Telegram: экранирование и `код`.
    Команды (`/add …`) остаются обычным текстом — так Telegram делает их кликабельными."""
    def code(m: re.Match) -> str:
        inner = m.group(1)
        return inner if inner.startswith("/") else f"<code>{inner}</code>"
    return re.sub(r"`([^`]+)`", code, html.escape(text))


def release_text(rel: dict) -> str:
    d = rel["date"]
    out = [f"🆕 <b>Книжный год {rel['version']}</b> · {d[8:10]}.{d[5:7]}.{d[:4]}"]
    if rel["summary"]:
        out.append(md_inline(rel["summary"]))
    for title, items in rel["sections"]:
        out.append(f"\n<b>{html.escape(title)}</b>")
        out += [f"• {md_inline(i)}" for i in items]
    return "\n".join(out)


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
        # Миграция v2: автор как сущность (Wikidata ID, варианты написания) и цепочки сообщений /add.
        # Сами книги привязываются к авторам в migrate_authors() — там нужна сеть.
        if con.execute("PRAGMA user_version").fetchone()[0] < 2:
            if con.execute("SELECT 1 FROM books LIMIT 1").fetchone():
                logging.info("Перед миграцией v2 — копия базы: %s", backup_db().name)
            con.execute("""
                CREATE TABLE IF NOT EXISTS authors (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    name       TEXT NOT NULL,          -- каноничное «Имя Фамилия»
                    wikidata   TEXT UNIQUE,            -- Q-номер; NULL — автор «как написали»
                    born       INTEGER,
                    died       INTEGER,
                    created_at TEXT NOT NULL
                )""")
            con.execute("""
                CREATE TABLE IF NOT EXISTS author_aliases (
                    key        TEXT    NOT NULL,       -- norm(alias)
                    author_id  INTEGER NOT NULL,
                    alias      TEXT    NOT NULL,
                    kind       TEXT    NOT NULL,       -- typed — так написал читатель; name — имя из Wikidata
                    sure       INTEGER NOT NULL DEFAULT 0,   -- 1 — можно не переспрашивать
                    PRIMARY KEY (key, author_id)
                )""")
            con.execute("""
                CREATE TABLE IF NOT EXISTS chains (
                    chat_id    INTEGER NOT NULL,
                    user_id    INTEGER NOT NULL,
                    kind       TEXT    NOT NULL,       -- add — новая запись, edit — правка через ✏️
                    book_id    INTEGER,
                    card_id    INTEGER,                -- карточка остаётся, остальное удаляется
                    msgs       TEXT    NOT NULL DEFAULT '',   -- id сообщений через запятую
                    updated_at TEXT    NOT NULL,
                    PRIMARY KEY (chat_id, user_id)
                )""")
            if "author_id" not in [r["name"] for r in con.execute("PRAGMA table_info(books)")]:
                con.execute("ALTER TABLE books ADD COLUMN author_id INTEGER")   # NULL — нет автора или ещё не сопоставлен
            con.execute("PRAGMA user_version = 2")


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


def author_words(author: str | None) -> list[str]:
    """Значимые слова автора: без инициалов и отчеств."""
    return [w for w in norm(author).split() if len(w) > 2 and not re.search(r"(вич|вна|ична)$", w)]


def author_key(author: str | None) -> str:
    """«Антон Павлович Чехов», «А. П. Чехов», «чехов» -> «чехов».
    Отбрасываем инициалы и отчества, из оставшегося берём последнее слово (фамилию)."""
    words = author_words(author)
    return words[-1] if words else ""


def book_key(author: str | None, title: str) -> str:
    """Единый ключ книги для статистики по всем чатам: фамилия + название без регистра, ё/е и пунктуации."""
    return f"{author_key(author)}|{norm(title)}"


# ---------- нечёткое сравнение книг (опечатки) ----------
def edits(a: str, b: str) -> int:
    """Сколько правок отделяет строки: вставка, удаление, замена, перестановка соседних букв."""
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[-1]


def _close(a: str, b: str, exact_upto: int, one_upto: int) -> int | None:
    """Число правок, если строки похожи (порог зависит от длины), иначе None.
    Короткие строки — только точно: «Оно» и «Она» — разные книги."""
    n = max(len(a), len(b))
    limit = 0 if n <= exact_upto else 1 if n <= one_upto else 2
    if abs(len(a) - len(b)) > limit:
        return None
    d = edits(a, b)
    return d if d <= limit else None


def match_score(a1: str | None, t1: str, a2: str | None, t2: str) -> int | None:
    """Похожи ли книги: число правок (0 — одинаковые) или None.
    Автор: любое слово одного похоже на любое слово другого («Жан-Поль Сартр» ~ «Сартр Жан-Поль»);
    если автор не указан хотя бы у одной — сравниваем только названия."""
    td = _close(norm(t1), norm(t2), 4, 10)
    if td is None:
        return None
    w1, w2 = author_words(a1), author_words(a2)
    if not w1 or not w2:
        return td
    ad = [d for x in w1 for y in w2 if (d := _close(x, y, 3, 6)) is not None]
    return td + min(ad) if ad else None


def known_books(exclude_id: int = 0) -> list[sqlite3.Row]:
    """Все книги бота по одной на book_key — в каноничном написании (как в /top:
    из проверенной записи, иначе из самой свежей), с числом записей и чатов."""
    with db() as con:
        return con.execute("""
            SELECT id, author, author_id, title, book_key, verified, n, chats FROM (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY book_key ORDER BY verified DESC, id DESC) AS rn,
                       COUNT(*) OVER (PARTITION BY book_key) AS n
                FROM books WHERE book_key IS NOT NULL AND id != ?)
            JOIN (SELECT book_key AS k, COUNT(DISTINCT chat_id) AS chats FROM books
                  WHERE id != ? GROUP BY book_key) ON k = book_key
            WHERE rn = 1""", (exclude_id, exclude_id)).fetchall()


def same_book(a1: str | None, id1: int | None, t1: str, a2: str | None, id2: int | None, t2: str) -> int | None:
    """Одна ли это книга: число правок или None. Если у обеих есть автор из базы — сравниваем его ID
    и название; иначе — написание автора и название, как раньше."""
    if id1 and id2:
        return _close(norm(t1), norm(t2), 4, 10) if id1 == id2 else None
    return match_score(a1, t1, a2, t2)


def similar_known(author: str | None, author_id: int | None, title: str, exclude_id: int = 0,
                  limit: int = 3) -> list[sqlite3.Row]:
    """Уже записанные в боте книги, похожие на введённую (у выбранного автора — только его книги).
    Первыми — с выверенным написанием (выбрано из Google Books) и самые частые:
    точное совпадение с чужой опечаткой не должно её закреплять."""
    found = []
    for k in known_books(exclude_id):
        s = same_book(author, author_id, title, k["author"], k["author_id"], k["title"])
        if s is not None:
            found.append((-k["verified"], -k["n"], s, k["id"], k))
    found.sort(key=lambda x: x[:4])
    return [x[-1] for x in found[:limit]]


# ---------- авторы ----------
# Автор — запись в authors: каноничное «Имя Фамилия», Wikidata ID (надёжный ключ) и варианты написания.
# В books.author хранится каноничное имя (его и показываем), books.author_id — кто это на самом деле.
PATRONYMIC = re.compile(r"(вич|вна|ична|оглы|кызы)$")
# Профессии в Wikidata, по которым ищем авторов книг
WRITER_OCCUPATIONS = ("Q36180", "Q6625963", "Q49757", "Q214917", "Q4853732", "Q1930187",
                      "Q11774202", "Q18844224", "Q12144794")   # писатель, романист, поэт, драматург, детский
                                                                # писатель, журналист, эссеист, фантаст, прозаик


def name_tokens(s: str | None) -> list[str]:
    """Слова имени без регистра и ё; дефис внутри слова сохраняем: «жан-поль»."""
    s = (s or "").casefold().replace("ё", "е")
    return [w.strip("-") for w in re.findall(r"[\w-]+", s) if w.strip("-")]


def parse_author(s: str | None) -> tuple[list[str], list[str]]:
    """Как написали автора -> (полные слова, инициалы). Инициал — 1–2 буквы: «Д.», «Дж.», «Ж.-П.».
    Отчества отбрасываем."""
    full, initials = [], []
    for w in name_tokens(s):
        for part in ([w] if len(w) > 2 else w.split("-")):
            if len(part) <= 2:
                initials.append(part)
            elif not PATRONYMIC.search(part):
                full.append(part)
    return full, initials


def is_specific(typed: str | None) -> bool:
    """Написание с именем или инициалом («Ирвинг дж.», «Л. Толстой»), а не голая фамилия."""
    full, initials = parse_author(typed)
    return bool(initials) or len(full) > 1


def author_fits(typed: str | None, name: str, any_surname: bool = False) -> int | None:
    """Подходит ли написанное к имени автора: число правок в фамилии или None.
    Фамилия — среди всех слов написанного (с допуском опечаток), остальные слова должны совпасть
    с именами, первый инициал — быть началом имени («Дж.» ↔ Джон — да, «А.» ↔ Джон — нет).
    В имени из Wikidata фамилия — последнее слово; у автора «как написали» (any_surname) — любое."""
    full, initials = parse_author(typed)
    words, _ = parse_author(name)
    if not full or not words:
        return None
    splits = range(len(words)) if any_surname else [len(words) - 1]
    best = None
    for j in splits:
        surname, given = words[j], words[:j] + words[j + 1:]
        given_parts = [p for g in given for p in {g, *g.split("-")}]
        if initials and given and not any(p.startswith(initials[0]) for p in [given[0], *given[0].split("-")]):
            continue
        for i, w in enumerate(full):
            d = _close(w, surname, 3, 6)
            if d is None:
                continue
            rest = full[:i] + full[i + 1:]
            if all(any(_close(r, g, 3, 6) is not None for g in given_parts) for r in rest):
                best = d if best is None else min(best, d)
    return best


def canonical_name(label: str, aliases: list[str]) -> str:
    """Имя из Wikidata -> «Имя Фамилия»: «Джон Уинслоу Ирвинг» -> «Джон Ирвинг».
    Форма «Фамилия, Имя» переворачивается, отчества отбрасываются; из метки и вариантов берём
    самый короткий, что начинается тем же именем и кончается той же фамилией («Габриель Гарсиа Маркес»
    остаётся целиком — короткого варианта у него нет)."""
    def flip(s: str) -> str:
        if "," in s:
            a, b = (x.strip() for x in s.split(",", 1))
            s = f"{b} {a}"
        return " ".join(w for w in s.split() if not PATRONYMIC.search(w.casefold()))
    base = flip(label)
    t = name_tokens(base)
    if len(t) < 2:
        return base
    opts = [o for o in [base, *map(flip, aliases)]
            if len(o.split()) >= 2 and name_tokens(o)[:1] == t[:1] and name_tokens(o)[-1:] == t[-1:]]
    return min(opts, key=lambda o: len(o.split())) if opts else base


def _year(claims: dict, prop: str) -> int | None:
    try:
        return int(claims[prop][0]["mainsnak"]["datavalue"]["value"]["time"][1:5])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


async def search_authors(typed: str, limit: int = 5) -> list[dict] | None:
    """Писатели из Wikidata, к которым подходит написанное: [{qid, name, en, born, died, links, d}],
    самые похожие и известные первыми. None — Wikidata недоступна (тогда работаем без неё)."""
    full, _ = parse_author(typed)
    if not full:
        return []
    api = "https://www.wikidata.org/w/api.php"
    query = (" ".join(full) + " haswbstatement:P31=Q5 haswbstatement:"
             + "|".join(f"P106={q}" for q in WRITER_OCCUPATIONS))
    try:
        timeout = aiohttp.ClientTimeout(total=WIKIDATA_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": WIKIDATA_UA}) as s:
            async with s.get(api, params={"action": "query", "list": "search", "srsearch": query,
                                          "srlimit": "7", "srnamespace": "0", "format": "json"}) as r:
                if r.status != 200:
                    logging.warning("Wikidata: HTTP %s", r.status)
                    return None
                ids = [x["title"] for x in (await r.json()).get("query", {}).get("search", [])]
            if not ids:
                return []
            async with s.get(api, params={"action": "wbgetentities", "ids": "|".join(ids), "format": "json",
                                          "props": "labels|aliases|claims|sitelinks", "languages": "ru|en"}) as r:
                if r.status != 200:
                    logging.warning("Wikidata: HTTP %s", r.status)
                    return None
                entities = (await r.json()).get("entities", {})
    except Exception as e:
        logging.warning("Wikidata недоступна: %s %s", type(e).__name__, e)
        return None
    out = []
    for qid in ids:
        e = entities.get(qid, {})
        names = {}
        for lang in ("ru", "en"):
            label = e.get("labels", {}).get(lang, {}).get("value")
            if label:
                names[lang] = canonical_name(label, [a["value"] for a in e.get("aliases", {}).get(lang, [])])
        if not names:
            continue
        fits = [d for n in names.values() if (d := author_fits(typed, n)) is not None]
        if not fits:
            continue
        claims = e.get("claims", {})
        out.append({"qid": qid, "name": names.get("ru") or names["en"], "en": names.get("en"),
                    "born": _year(claims, "P569"), "died": _year(claims, "P570"),
                    "links": len(e.get("sitelinks", {})), "d": min(fits)})
    out.sort(key=lambda c: (c["d"], -c["links"]))
    return out[:limit]


def clear_pick(cands: list[dict]) -> dict | None:
    """Кандидат, которого можно выбрать без вопроса (для миграции старых записей):
    единственный или известнее следующего в 5+ раз (Герман Гессе — 164 статьи в Википедиях, следующий — 2)."""
    if len(cands) == 1:
        return cands[0]
    if len(cands) > 1 and cands[0]["d"] <= cands[1]["d"] and cands[0]["links"] >= 5 * max(cands[1]["links"], 1):
        return cands[0]
    return None


def years(born: int | None, died: int | None) -> str:
    if born and died:
        return f"{born}–{died}"
    return f"род. {born}" if born else ""


def get_author(author_id: int | None) -> sqlite3.Row | None:
    if not author_id:
        return None
    with db() as con:
        return con.execute("SELECT * FROM authors WHERE id = ?", (author_id,)).fetchone()


def add_alias(author_id: int, alias: str, kind: str = "typed", sure: bool = False) -> None:
    """Запоминает вариант написания. Один вариант у двух авторов — значит, он неоднозначный,
    и такие варианты бот всегда переспрашивает."""
    key = norm(alias)
    if not key:
        return
    with db() as con:
        con.execute("INSERT INTO author_aliases (key, author_id, alias, kind, sure) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(key, author_id) DO UPDATE SET sure = MAX(sure, excluded.sure)",
                    (key, author_id, alias, kind, int(sure)))


def author_from_wikidata(c: dict) -> int:
    """Автор из Wikidata: находит по Q-номеру или заводит нового. Возвращает id."""
    with db() as con:
        row = con.execute("SELECT id FROM authors WHERE wikidata = ?", (c["qid"],)).fetchone()
        if row:
            return row["id"]
        author_id = con.execute(
            "INSERT INTO authors (name, wikidata, born, died, created_at) VALUES (?, ?, ?, ?, ?)",
            (c["name"], c["qid"], c.get("born"), c.get("died"),
             datetime.now().isoformat(timespec="seconds"))).lastrowid
    for n in {c["name"], c.get("en")} - {None}:
        add_alias(author_id, n, kind="name")
    return author_id


def author_as_typed(typed: str) -> int:
    """Автор «как написали» (нет в Wikidata или Wikidata недоступна): такой же без Wikidata — или новый."""
    typed = " ".join(typed.split())
    with db() as con:
        for row in con.execute("SELECT id, name FROM authors WHERE wikidata IS NULL ORDER BY id"):
            if norm(row["name"]) == norm(typed):
                return row["id"]
        return con.execute("INSERT INTO authors (name, created_at) VALUES (?, ?)",
                           (typed, datetime.now().isoformat(timespec="seconds"))).lastrowid


def sure_alias(typed: str) -> int | None:
    """Автор, которого можно взять без вопроса: написание подтверждали как однозначное,
    и оно ведёт ровно к одному автору."""
    with db() as con:
        rows = con.execute("SELECT author_id, sure FROM author_aliases WHERE key = ? AND kind = 'typed'",
                           (norm(typed),)).fetchall()
        others = con.execute("SELECT COUNT(DISTINCT author_id) FROM author_aliases WHERE key = ?",
                             (norm(typed),)).fetchone()[0]
    if len(rows) == 1 and others == 1 and rows[0]["sure"]:
        return rows[0]["author_id"]
    return None


def similar_authors(typed: str, limit: int = 3) -> list[sqlite3.Row]:
    """Авторы из базы, к которым подходит написанное: подтверждённое написание — первым,
    затем по числу правок и по числу книг."""
    key = norm(typed)
    with db() as con:
        authors = con.execute("""
            SELECT a.*, (SELECT COUNT(*) FROM books b WHERE b.author_id = a.id) AS n FROM authors a""").fetchall()
        aliases = con.execute("SELECT author_id, key, alias, kind FROM author_aliases").fetchall()
    by_author: dict[int, list[sqlite3.Row]] = {}
    for s in aliases:
        by_author.setdefault(s["author_id"], []).append(s)
    found = []
    for a in authors:
        names = [a["name"]] + [s["alias"] for s in by_author.get(a["id"], []) if s["kind"] == "name"]
        exact = any(s["key"] == key for s in by_author.get(a["id"], []))
        ds = [d for n in names if (d := author_fits(typed, n, any_surname=not a["wikidata"])) is not None]
        if exact or ds:
            found.append((-1 if exact else min(ds), -a["n"], a["id"], a))
    found.sort(key=lambda x: x[:3])
    return [x[-1] for x in found[:limit]]


async def resolve_author_quietly(name: str | None) -> int | None:
    """Автор без вопросов (например, из варианта Google для книги, записанной без автора):
    однозначное написание, понятный кандидат в Wikidata или «как написано»."""
    if not name or not name.strip():
        return None
    if (aid := sure_alias(name)) is not None:
        return aid
    with db() as con:   # точно такое написание уже есть ровно у одного автора
        exact = {r[0] for r in con.execute("SELECT author_id FROM author_aliases WHERE key = ?", (norm(name),))}
    if len(exact) == 1:
        return exact.pop()
    pick = clear_pick(await search_authors(name) or [])   # понятный писатель из Wikidata
    if pick:
        return author_from_wikidata(pick)
    known = similar_authors(name, limit=2)   # иначе — единственный похожий из базы
    return known[0]["id"] if len(known) == 1 else author_as_typed(name)


def key_for(author_id: int | None, author: str | None, title: str) -> str:
    """book_key: от ID автора («#12|игра в бисер»), а без сопоставленного автора — по-старому."""
    return f"#{author_id}|{norm(title)}" if author_id else book_key(author, title)


async def migrate_authors() -> None:
    """Миграция v2, сетевая часть: сопоставляет старые записи с авторами.
    Понятные кандидаты Wikidata — сразу; спорные написания — к уже найденному автору из базы,
    если подходят только к нему («Д. Ирвинг» -> Джон Ирвинг); остальные — «как написано» (их покажет /dups).
    Wikidata недоступна — оставляем записи как есть и пробуем при следующем запуске."""
    with db() as con:
        raws = [r[0] for r in con.execute(
            "SELECT DISTINCT author FROM books WHERE author_id IS NULL AND TRIM(COALESCE(author, '')) != ''")]
    if not raws:
        return
    resolved: dict[str, tuple[int, bool]] = {}
    unclear = []
    for raw in raws:
        cands = await search_authors(raw)
        if cands is None:
            logging.warning("Миграция авторов отложена: Wikidata недоступна")
            return
        pick = clear_pick(cands)
        if pick:
            resolved[raw] = (author_from_wikidata(pick), is_specific(raw) or len(cands) == 1)
        else:
            unclear.append(raw)
        await asyncio.sleep(0.2)
    for raw in unclear:
        fit = {aid for aid, _ in resolved.values()
               if author_fits(raw, get_author(aid)["name"], any_surname=not get_author(aid)["wikidata"]) is not None}
        resolved[raw] = (fit.pop(), is_specific(raw)) if len(fit) == 1 else (author_as_typed(raw), False)
    with db() as con:
        rows = con.execute("SELECT id, author, title FROM books WHERE author_id IS NULL AND author IN (%s)"
                           % ",".join("?" * len(resolved)), list(resolved)).fetchall()
    for r in rows:
        aid, sure = resolved[r["author"]]
        add_alias(aid, r["author"], sure=sure)
        a = get_author(aid)
        with db() as con:
            con.execute("UPDATE books SET author_id = ?, author = ?, book_key = ? WHERE id = ?",
                        (aid, a["name"], key_for(aid, a["name"], r["title"]), r["id"]))
    logging.info("Миграция авторов: написаний %s, записей %s", len(resolved), len(rows))


RATING = {1: "👍", 0: "😐", -1: "👎"}


# ---------- поиск книги (Google Books) ----------
def clean_title(title: str) -> str:
    """Название из Google без хвостов: «Анна Каренина. Коллекционное издание», «Слова / Les mots» -> суть."""
    return re.split(r"\s*(?:[:;/(\[]|\.\s|\s[—–-]\s)", title.strip(), maxsplit=1)[0].strip(" .,") or title.strip()


async def search_books(query: str, limit: int = 15) -> list[tuple[str | None, str]] | None:
    """Возвращает до limit вариантов (автор, название) без повторов; названия — без хвостов изданий.
    None — Google недоступен (ошибка, лимит): тогда «ничего не найдено» ничего не значит."""
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
                    try:   # текст ошибки Google (без ключа) — чтобы, например, «API key not valid» был виден
                        reason = (await r.json(content_type=None)).get("error", {}).get("message", "")
                    except Exception:
                        reason = ""
                    logging.warning("Google Books: HTTP %s (попытка %s) %s", r.status, attempt + 1, reason)
                    if r.status != 429:
                        return None
        except Exception as e:
            logging.warning("Google Books недоступен: %s %s", type(e).__name__, e)
            return None
        await asyncio.sleep(1.5)
    if data is None:
        return None
    out, seen = [], set()
    for item in data.get("items", []):
        info = item.get("volumeInfo", {})
        title = clean_title(info.get("title") or "")
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


def verify_kb(book_id: int, candidates: list[dict]):
    kb = InlineKeyboardBuilder()
    for i, c in enumerate(candidates):
        label = ("📚 " if c["known"] else "") + (f"{c['author']} — {c['title']}" if c["author"] else c["title"])
        kb.button(text=label[:60], callback_data=f"v:{book_id}:{i}")
    kb.button(text="✍️ Оставить как написал", callback_data=f"v:{book_id}:-")
    kb.adjust(1)
    return kb.as_markup()


def author_kb(user_id: int, options: list[dict]):
    kb = InlineKeyboardBuilder()
    for i, o in enumerate(options):
        label = ("📚 " if o["known"] else "") + o["name"] + (f" ({o['years']})" if o["years"] else "")
        kb.button(text=label[:60], callback_data=f"au:{user_id}:{i}")
    kb.button(text="✍️ Оставить как написал", callback_data=f"au:{user_id}:-")
    kb.adjust(1)
    return kb.as_markup()


def not_found_kb(book_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Исправить название", callback_data=f"nf:{book_id}:fix")
    kb.button(text="✍️ Всё верно, оставить", callback_data=f"nf:{book_id}:ok")
    kb.adjust(1)
    return kb.as_markup()


def dup_kb(book_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, добавить", callback_data=f"dp:{book_id}:1")
    kb.button(text="✖️ Нет, не надо", callback_data=f"dp:{book_id}:0")
    kb.adjust(2)
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


def card_view(b: sqlite3.Row) -> tuple[str, object]:
    """Текст и кнопки карточки для следующего незаполненного шага или итоговый вид."""
    if b["genre"] is None:
        return f"📖 {book_title_line(b)}\nКакой жанр?", genre_kb(b["chat_id"], b["id"])
    if b["rating"] is None:
        return f"📖 {book_title_line(b)} — {html.escape(b['genre'])}\nКак тебе книга?", rec_kb(b["id"])
    with db() as con:
        n = con.execute(
            "SELECT COUNT(*) FROM books WHERE chat_id = ? AND user_id = ? AND created_at LIKE ? AND id <= ?",
            (b["chat_id"], b["user_id"], b["created_at"][:5] + "%", b["id"])).fetchone()[0]
    return (f"✅ {html.escape(b['user_name'])}: {book_line(b)}\n{n}-я книга в {b['created_at'][:4]} году.",
            card_kb(b["id"]))


async def advance(card: Message, b: sqlite3.Row) -> None:
    """Ведёт карточку книги к следующему незаполненному шагу. Карточка готова — убираем из чата
    всю цепочку (команду, вопросы и ответы), остаётся только карточка."""
    text, kb = card_view(b)
    await card.edit_text(text, reply_markup=kb)
    if b["genre"] is not None and b["rating"] is not None:
        await chain_finish(card.bot, b["chat_id"], b["user_id"], book_id=b["id"])


def earlier_copy(b: sqlite3.Row) -> sqlite3.Row | None:
    """Та же книга (с учётом опечаток), уже записанная этим человеком в этом чате в этом году."""
    with db() as con:
        rows = con.execute(
            "SELECT * FROM books WHERE chat_id = ? AND user_id = ? AND id != ? AND created_at LIKE ? "
            "ORDER BY id", (b["chat_id"], b["user_id"], b["id"], b["created_at"][:5] + "%")).fetchall()
    return next((r for r in rows if same_book(b["author"], b["author_id"], b["title"],
                                              r["author"], r["author_id"], r["title"]) is not None), None)


async def after_verify(card: Message, b: sqlite3.Row) -> None:
    """Книга определена. Для новой записи — предупреждаем о повторе, затем ведём карточку дальше."""
    if b["genre"] is None and b["rating"] is None:   # правку через ✏️ не трогаем
        old = earlier_copy(b)
        if old:
            d = old["created_at"]
            await card.edit_text(
                f"📖 {book_title_line(b)}\n{html.escape(b['user_name'])}, ты уже записывал(а) "
                f"эту книгу {d[8:10]}.{d[5:7]} — всё равно добавить?", reply_markup=dup_kb(b["id"]))
            return
    await advance(card, b)


# ----- цепочка сообщений записи книги -----
# Пока человек записывает книгу (/add или ✏️ «Автор и название»), команда, вопросы бота и ответы
# остаются в чате; когда карточка готова — удаляются, остаётся только карточка.
# Цепочка хранится в базе, чтобы после перезапуска бот мог за собой убрать.
def chain_get(chat_id: int, user_id: int) -> sqlite3.Row | None:
    with db() as con:
        return con.execute("SELECT * FROM chains WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)).fetchone()


def chain_note(chat_id: int, user_id: int, *msg_ids: int, book_id: int | None = None,
               card_id: int | None = None) -> bool:
    """Добавляет сообщения в цепочку и продлевает её. False — цепочки нет."""
    ch = chain_get(chat_id, user_id)
    if ch is None:
        return False
    msgs = [x for x in ch["msgs"].split(",") if x] + [str(m) for m in msg_ids if m and m != ch["card_id"]]
    with db() as con:
        con.execute("UPDATE chains SET msgs = ?, book_id = COALESCE(?, book_id), card_id = COALESCE(?, card_id), "
                    "updated_at = ? WHERE chat_id = ? AND user_id = ?",
                    (",".join(msgs), book_id, card_id, datetime.now().isoformat(timespec="seconds"),
                     chat_id, user_id))
    return True


async def delete_msg(bot: Bot, chat_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass   # нет прав, сообщение старше 48 часов или уже удалено


async def _chain_close(bot: Bot, ch: sqlite3.Row) -> None:
    pending.pop((ch["chat_id"], ch["user_id"]), None)
    with db() as con:
        con.execute("DELETE FROM chains WHERE chat_id = ? AND user_id = ?", (ch["chat_id"], ch["user_id"]))
    for mid in [x for x in ch["msgs"].split(",") if x]:
        await delete_msg(bot, ch["chat_id"], int(mid))


async def chain_finish(bot: Bot, chat_id: int, user_id: int, book_id: int | None = None) -> None:
    """Цепочка завершена (карточка готова или запись удалена): убираем всё, кроме карточки."""
    ch = chain_get(chat_id, user_id)
    if ch and (book_id is None or ch["book_id"] == book_id):
        await _chain_close(bot, ch)


async def chain_abort(bot: Bot, ch: sqlite3.Row) -> None:
    """Брошенная цепочка: убираем сообщения. Новая запись — вместе с карточкой и недооформленной
    записью; брошенная правка — карточка возвращается к итоговому виду, запись остаётся."""
    await _chain_close(bot, ch)
    b = get_book(ch["book_id"]) if ch["book_id"] else None
    if ch["kind"] == "add":
        await delete_msg(bot, ch["chat_id"], ch["card_id"])
        if b and (b["genre"] is None or b["rating"] is None):
            with db() as con:
                con.execute("DELETE FROM books WHERE id = ?", (b["id"],))
            pending_candidates.pop(b["id"], None)
            logging.info("Брошенная запись книги %s удалена", b["id"])
    elif b and ch["card_id"]:
        text, kb = card_view(b)
        try:
            await bot.edit_message_text(text, chat_id=ch["chat_id"], message_id=ch["card_id"], reply_markup=kb)
        except Exception:
            pass


async def chain_start(bot: Bot, chat_id: int, user_id: int, kind: str, msg_ids: list[int] | None = None,
                      book_id: int | None = None, card_id: int | None = None) -> None:
    """Новая цепочка. Незаконченную прежнюю у этого человека в этом чате — убираем сразу."""
    old = chain_get(chat_id, user_id)
    if old:
        await chain_abort(bot, old)
    with db() as con:
        con.execute("INSERT INTO chains (chat_id, user_id, kind, book_id, card_id, msgs, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (chat_id, user_id, kind, book_id, card_id, ",".join(map(str, msg_ids or [])),
                     datetime.now().isoformat(timespec="seconds")))


async def sweep_once(bot: Bot) -> int:
    """Убирает цепочки, брошенные дольше CHAIN_TTL_MIN минут. Возвращает, сколько убрано."""
    border = (datetime.now() - timedelta(minutes=CHAIN_TTL_MIN)).isoformat(timespec="seconds")
    with db() as con:
        stale = con.execute("SELECT * FROM chains WHERE updated_at < ?", (border,)).fetchall()
    for ch in stale:
        await chain_abort(bot, ch)
    return len(stale)


async def sweep_chains(bot: Bot) -> None:
    """Раз в минуту — уборка брошенных цепочек (и сразу после запуска — за время простоя)."""
    while True:
        try:
            await sweep_once(bot)
        except Exception as e:
            logging.warning("Уборка цепочек: %s", e)
        await asyncio.sleep(60)


# (chat_id, user_id) -> чего ждём: {"step": "author"|"author_pick"|"title"|"genre"|"feedback", ...}
pending: dict[tuple[int, int], dict] = {}
# book_id -> варианты книги {author, title, key, author_id, known} — живут до перезапуска;
# после — кнопки работают как «оставить»
pending_candidates: dict[int, list[dict]] = {}


def author_names(author_id: int) -> list[str]:
    """Каноничное имя автора и его имена из Wikidata (в том числе латиницей: John Irving)."""
    a = get_author(author_id)
    with db() as con:
        rows = con.execute("SELECT alias FROM author_aliases WHERE author_id = ? AND kind = 'name'",
                           (author_id,)).fetchall()
    return ([a["name"]] if a else []) + [r["alias"] for r in rows]


async def find_candidates(b: sqlite3.Row, query: str) -> list[dict] | None:
    """Варианты книги: сначала похожие, уже записанные в боте, затем Google Books.
    Из Google — только книги с похожим названием (а не, например, книги о самом авторе);
    если автор выбран — только его книги, и берём лишь название (автор остаётся выбранным).
    [] — ничего похожего нет; None — ничего нет в базе, а Google недоступен (проверить не можем)."""
    known = similar_known(b["author"], b["author_id"], b["title"], exclude_id=b["id"])
    out = [{"author": k["author"], "title": k["title"], "key": k["book_key"], "author_id": k["author_id"],
            "known": True} for k in known]
    seen = {norm(k["title"]) for k in known}
    found = await search_books(query)
    names = author_names(b["author_id"]) if b["author_id"] else []
    for a, t in found or []:
        if norm(t) in seen or _close(norm(t), norm(b["title"]), 4, 10) is None:
            continue
        if b["author_id"]:
            if a and not any(author_fits(a, n, any_surname=True) is not None for n in names):
                continue   # другой автор с похожим названием
            out.append({"author": b["author"], "title": t, "key": key_for(b["author_id"], b["author"], t),
                        "author_id": b["author_id"], "known": False})
        elif not any(match_score(a, t, k["author"], k["title"]) is not None for k in known):
            out.append({"author": a, "title": t, "key": None, "author_id": None, "known": False})
        seen.add(norm(t))
    if not out and found is None:
        return None
    return out[:5]


async def ask(chat_msg: Message, user, step: str, text: str, placeholder: str, **data) -> None:
    """Задаёт вопрос, на который человек отвечает реплаем, и запоминает, чего ждём."""
    key = (chat_msg.chat.id, user.id)
    old = pending.get(key)
    if old and old.get("prompt"):
        await drop(old["prompt"])
    prompt = await chat_msg.bot.send_message(
        chat_msg.chat.id, f"{mention(user)}, {text}",
        reply_markup=ForceReply(selective=True, input_field_placeholder=placeholder))
    chain_note(chat_msg.chat.id, user.id, prompt.message_id)
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
        candidates = await find_candidates(b, query)
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


# ----- дубли книг и авторов (админ) -----
def similar_pairs(limit: int = 30) -> list[tuple[sqlite3.Row, sqlite3.Row]]:
    """Пары разных book_key, похожих с учётом опечаток. Первой — книга с большим числом записей."""
    books = known_books()
    pairs = []
    for i, a in enumerate(books):
        for b in books[i + 1:]:
            s = same_book(a["author"], a["author_id"], a["title"], b["author"], b["author_id"], b["title"])
            if s is not None:
                pairs.append((s, *sorted((a, b), key=lambda r: (-r["n"], r["id"]))))
    pairs.sort(key=lambda p: p[0])
    return [(a, b) for _, a, b in pairs[:limit]]


def author_stats() -> list[sqlite3.Row]:
    with db() as con:
        return con.execute("""
            SELECT a.*, COUNT(b.id) AS n, COUNT(DISTINCT b.chat_id) AS chats
            FROM authors a LEFT JOIN books b ON b.author_id = a.id GROUP BY a.id ORDER BY a.id""").fetchall()


def similar_author_pairs(limit: int = 30) -> list[tuple[sqlite3.Row, sqlite3.Row]]:
    """Пары авторов, похожих на одного человека: опечатки, инициалы, «как написали» рядом с Wikidata.
    Двое с разными Wikidata ID — разные люди (Лев и Алексей Толстые), их не предлагаем."""
    authors = [a for a in author_stats() if a["n"]]
    pairs = []
    for i, a in enumerate(authors):
        for b in authors[i + 1:]:
            if a["wikidata"] and b["wikidata"]:
                continue
            ds = [d for d in (author_fits(a["name"], b["name"], any_surname=not b["wikidata"]),
                              author_fits(b["name"], a["name"], any_surname=not a["wikidata"])) if d is not None]
            if ds:   # первым — автор из Wikidata, затем с большим числом записей
                pairs.append((min(ds), *sorted((a, b), key=lambda r: (not r["wikidata"], -r["n"], r["id"]))))
    pairs.sort(key=lambda p: p[0])
    return [(a, b) for _, a, b in pairs[:limit]]


def backup_db() -> Path:
    """Копия базы рядом с ней: books.db.bak-ГГГГММДД-ЧЧММСС (безопасно при работающем боте)."""
    path = DB_PATH.with_name(f"{DB_PATH.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    src, dst = sqlite3.connect(DB_PATH), sqlite3.connect(path)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    return path


def merge_books(keep_id: int, drop_id: int) -> tuple[int, Path] | None:
    """Все записи книги drop_id получают написание, автора и book_key книги keep_id. None — записи пропали."""
    keep, drop_ = get_book(keep_id), get_book(drop_id)
    if not keep or not drop_ or keep["book_key"] == drop_["book_key"]:
        return None
    path = backup_db()
    with db() as con:
        n = con.execute("UPDATE books SET author = ?, author_id = ?, title = ?, book_key = ? WHERE book_key = ?",
                        (keep["author"], keep["author_id"], keep["title"], keep["book_key"],
                         drop_["book_key"])).rowcount
        # написание подтвердил админ — /reverify про эти записи больше не спрашивает
        con.execute("UPDATE books SET verified = 1 WHERE book_key = ?", (keep["book_key"],))
    logging.info("Слияние книг: %s -> %s, записей %s, бэкап %s", drop_["book_key"], keep["book_key"], n, path.name)
    return n, path


def merge_authors(keep_id: int, drop_id: int) -> tuple[int, Path] | None:
    """Книги и варианты написания автора drop_id переходят к keep_id; drop_id удаляется.
    Если у оставленного нет Wikidata ID, а у второго есть, — ID переходит к оставленному."""
    keep, drop_ = get_author(keep_id), get_author(drop_id)
    if not keep or not drop_ or keep_id == drop_id:
        return None
    path = backup_db()
    with db() as con:
        rows = con.execute("SELECT id, title FROM books WHERE author_id = ?", (drop_id,)).fetchall()
        for r in rows:
            con.execute("UPDATE books SET author_id = ?, author = ?, book_key = ? WHERE id = ?",
                        (keep_id, keep["name"], key_for(keep_id, keep["name"], r["title"]), r["id"]))
        con.execute("""
            INSERT INTO author_aliases (key, author_id, alias, kind, sure)
            SELECT key, ?, alias, kind, sure FROM author_aliases WHERE author_id = ? AND true
            ON CONFLICT(key, author_id) DO UPDATE SET sure = MAX(sure, excluded.sure)""", (keep_id, drop_id))
        con.execute("DELETE FROM author_aliases WHERE author_id = ?", (drop_id,))
        con.execute("DELETE FROM authors WHERE id = ?", (drop_id,))
        if not keep["wikidata"] and drop_["wikidata"]:
            con.execute("UPDATE authors SET wikidata = ?, born = ?, died = ? WHERE id = ?",
                        (drop_["wikidata"], drop_["born"], drop_["died"], keep_id))
    logging.info("Слияние авторов: %s -> %s, записей %s, бэкап %s", drop_id, keep_id, len(rows), path.name)
    return len(rows), path


def is_admin(user) -> bool:
    return bool(ADMIN_ID) and user.id == ADMIN_ID


def author_title(a: sqlite3.Row) -> str:
    extra = ", ".join(x for x in (years(a["born"], a["died"]), a["wikidata"] or "без Wikidata") if x)
    return f"{html.escape(a['name'])} <i>({extra})</i>"


@open_router.message(Command("dups"))
async def cmd_dups(m: Message):
    """Админ: похожие авторы и книги по всем чатам — выбрать, что оставить, и слить."""
    if not is_admin(m.from_user):
        return
    authors, books = similar_author_pairs(), similar_pairs()
    if not authors and not books:
        await m.reply("👯 Похожих авторов и книг не нашёл.")
        return
    items = []   # (текст, кнопка «оставить А», кнопка «оставить Б»)
    for a, b in authors:
        items.append((f"👤 А: {author_title(a)} — {a['n']} зап., чатов {a['chats']}\n"
                      f"    Б: {author_title(b)} — {b['n']} зап., чатов {b['chats']}",
                      f"dm:a:{a['id']}:{b['id']}", f"dm:a:{b['id']}:{a['id']}"))
    for a, b in books:
        items.append((f"📖 А: {book_title_line(a)} <i>({a['n']} зап., чатов {a['chats']})</i>\n"
                      f"    Б: {book_title_line(b)} <i>({b['n']} зап., чатов {b['chats']})</i>",
                      f"dm:b:{a['id']}:{b['id']}", f"dm:b:{b['id']}:{a['id']}"))
    text = "👯 <b>Похожие авторы и книги</b>. Выбери, что оставить (сначала авторы — после них книги):\n"
    kb = InlineKeyboardBuilder()
    for i, (item, keep_a, keep_b) in enumerate(items, 1):
        item = f"\n<b>{i}.</b> {item}\n"
        if len(text) + len(item) > 3800:   # лимит Telegram — 4096; остальное — после слияния этих
            text += f"\n…и ещё пар: {len(items) - i + 1}"
            break
        text += item
        kb.button(text=f"{i}: оставить А", callback_data=keep_a)
        kb.button(text=f"{i}: оставить Б", callback_data=keep_b)
    kb.adjust(2)
    await m.reply(text, reply_markup=kb.as_markup())


@open_router.callback_query(F.data.startswith("dm:"))
async def on_dups_pick(cb: CallbackQuery):
    if not is_admin(cb.from_user):
        return await cb.answer()
    _, kind, keep_id, drop_id = cb.data.split(":")
    with db() as con:
        if kind == "a":
            keep, drop_ = get_author(int(keep_id)), get_author(int(drop_id))
            if not keep or not drop_:
                return await cb.answer("Список устарел — вызови /dups заново", show_alert=True)
            r = con.execute("SELECT COUNT(*) AS n, COUNT(DISTINCT chat_id) AS chats FROM books WHERE author_id = ?",
                            (drop_["id"],)).fetchone()
            what = f"Автор «{html.escape(drop_['name'])}» → «{html.escape(keep['name'])}»"
        else:
            keep, drop_ = get_book(int(keep_id)), get_book(int(drop_id))
            if not keep or not drop_ or keep["book_key"] == drop_["book_key"]:
                return await cb.answer("Список устарел — вызови /dups заново", show_alert=True)
            r = con.execute("SELECT COUNT(*) AS n, COUNT(DISTINCT chat_id) AS chats FROM books WHERE book_key = ?",
                            (drop_["book_key"],)).fetchone()
            what = f"«{book_title_line(drop_)}» → «{book_title_line(keep)}»"
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Слить", callback_data=f"dmy:{kind}:{keep_id}:{drop_id}")
    kb.button(text="✖️ Отмена", callback_data="dmn")
    await cb.message.answer(
        f"Изменится записей: <b>{r['n']}</b> в чатах: <b>{r['chats']}</b>\n{what}\n"
        f"Перед слиянием сделаю копию базы. Сливаем?", reply_markup=kb.as_markup())
    await cb.answer()


@open_router.callback_query(F.data.startswith("dmy:"))
async def on_dups_merge(cb: CallbackQuery):
    if not is_admin(cb.from_user):
        return await cb.answer()
    _, kind, keep_id, drop_id = cb.data.split(":")
    res = (merge_authors if kind == "a" else merge_books)(int(keep_id), int(drop_id))
    if res is None:
        await cb.message.edit_text("Уже слито или записи удалены — вызови /dups заново.")
    else:
        n, path = res
        await cb.message.edit_text(f"✅ Слито записей: {n}. Копия базы: <code>{html.escape(path.name)}</code>\n"
                                   f"Обновить список: /dups")
    await cb.answer()


@open_router.callback_query(F.data == "dmn")
async def on_dups_cancel(cb: CallbackQuery):
    if not is_admin(cb.from_user):
        return await cb.answer()
    await cb.message.edit_text("Слияние отменено.")
    await cb.answer()


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
# Цепочка: автор → «Это какой автор?» → название → «Это какая книга?» → жанр → реакция.
@router.message(Command("add"))
async def cmd_add(m: Message, command: CommandObject):
    await chain_start(m.bot, m.chat.id, m.from_user.id, "add", [m.message_id])
    if not command.args:
        await ask(m, m.from_user, "author", "✍️ кто автор? Ответь на это сообщение.\n"
                  "Если автора нет — отправь «-».", "Например: Чехов")
        return
    author, title = parse_book(command.args)
    await got_author(m, m.from_user, author, title=title)


async def got_author(chat_msg: Message, user, typed: str | None, title: str | None = None,
                     book_id: int | None = None, card: Message | None = None) -> None:
    """Автор написан — уточняем, кто это: сначала авторы из базы, затем писатели из Wikidata.
    Однозначное подтверждённое написание («Ирвинг дж.») — без вопроса, но только для новой записи:
    при правке через ✏️ спрашиваем всегда, иначе ошибку не исправить."""
    if not typed:
        return await after_author(chat_msg, user, None, title, book_id, card)
    if book_id is None and (aid := sure_alias(typed)) is not None:
        return await after_author(chat_msg, user, aid, title, book_id, card)
    known = similar_authors(typed)
    found = await search_authors(typed)   # None — Wikidata недоступна: показываем только авторов из базы
    options = [{"id": a["id"], "name": a["name"], "years": years(a["born"], a["died"]), "known": True}
               for a in known]
    shown = {a["wikidata"] for a in known if a["wikidata"]}
    for c in found or []:
        if len(options) >= 4:
            break
        if c["qid"] in shown:
            continue
        with db() as con:
            row = con.execute("SELECT * FROM authors WHERE wikidata = ?", (c["qid"],)).fetchone()
        if row:   # есть в базе, но под другим написанием
            options.append({"id": row["id"], "name": row["name"], "years": years(row["born"], row["died"]),
                            "known": True})
        else:
            options.append({"wd": c, "name": c["name"], "years": years(c["born"], c["died"]), "known": False})
        shown.add(c["qid"])
    if not options:   # некого предложить — автор «как написали»
        aid = author_as_typed(typed)
        add_alias(aid, typed, sure=is_specific(typed))
        return await after_author(chat_msg, user, aid, title, book_id, card)
    text = (f"✍️ {html.escape(user.first_name)}, «{html.escape(typed)}» — это какой автор?"
            + ("\n📚 — уже есть в боте" if any(o["known"] for o in options) else ""))
    if card is None:
        card = await chat_msg.bot.send_message(chat_msg.chat.id, text, reply_markup=author_kb(user.id, options))
        chain_note(chat_msg.chat.id, user.id, card_id=card.message_id)
    else:
        await card.edit_text(text, reply_markup=author_kb(user.id, options))
    pending[(chat_msg.chat.id, user.id)] = {
        "step": "author_pick", "typed": typed, "options": options, "title": title, "book_id": book_id,
        "card": card, "sure": is_specific(typed) or (found is not None and len(found) == 1)}


@router.callback_query(F.data.startswith("au:"))
async def on_author_pick(cb: CallbackQuery):
    _, user_id, choice = cb.data.split(":")
    if cb.from_user.id != int(user_id):
        return await cb.answer("Это не твоя книга 🙂", show_alert=True)
    key = (cb.message.chat.id, cb.from_user.id)
    st = pending.get(key)
    if not st or st["step"] != "author_pick" or st["card"].message_id != cb.message.message_id:
        ch = chain_get(*key)
        if ch and ch["card_id"] == cb.message.message_id:
            await chain_abort(cb.bot, ch)
        return await cb.answer("Этот вопрос устарел — начни заново: /add", show_alert=True)
    del pending[key]
    typed = st["typed"]
    if choice == "-":
        aid = author_as_typed(typed)
        add_alias(aid, typed, sure=is_specific(typed))
    else:
        o = st["options"][int(choice)]
        aid = o["id"] if "id" in o else author_from_wikidata(o["wd"])
        add_alias(aid, typed, sure=st["sure"])
    chain_note(*key)
    await cb.answer()
    await after_author(st["card"], cb.from_user, aid, st["title"], st["book_id"], st["card"])


async def after_author(chat_msg: Message, user, author_id: int | None, title: str | None,
                       book_id: int | None, card: Message | None) -> None:
    """Автор определён: спрашиваем название (если его ещё нет) или сразу ищем книгу."""
    a = get_author(author_id)
    name = a["name"] if a else None
    if title:
        return await save_book(chat_msg, user, author_id, name, title, book_id, card)
    if card is not None and name:
        await card.edit_text(f"📖 {html.escape(name)} — …")
    await ask(chat_msg, user, "title", "📖 как называется книга?", "Например: Вишнёвый сад",
              author_id=author_id, author_name=name, book_id=book_id, card=card)


async def save_book(chat_msg: Message, user, author_id: int | None, author: str | None, title: str,
                    book_id: int | None, card: Message | None) -> None:
    """Записывает книгу (или правит запись) и ищет варианты написания названия."""
    key = key_for(author_id, author, title)
    if book_id:   # правка существующей записи
        with db() as con:
            con.execute("UPDATE books SET author = ?, author_id = ?, title = ?, book_key = ?, verified = 0 "
                        "WHERE id = ?", (author, author_id, title, key, book_id))
    else:
        with db() as con:
            book_id = con.execute(
                "INSERT INTO books (chat_id, user_id, user_name, author, author_id, title, book_key, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (chat_msg.chat.id, user.id, user.first_name, author, author_id, title, key,
                 datetime.now().isoformat(timespec="seconds"))).lastrowid
    text = f"🔎 Ищу «{html.escape(title)}»…"
    if card is None:
        card = await chat_msg.bot.send_message(chat_msg.chat.id, text)
    else:
        await card.edit_text(text)
    chain_note(chat_msg.chat.id, user.id, book_id=book_id, card_id=card.message_id)
    b = get_book(book_id)
    if b:
        await search_into_card(card, b, f"{author or ''} {title}".strip())


async def search_into_card(card: Message, b: sqlite3.Row, query: str) -> None:
    query = re.sub(r"\s+[—–-]\s+", " ", query)
    candidates = await find_candidates(b, query)
    if candidates:
        pending_candidates[b["id"]] = candidates
        note = "\n📚 — уже записывали в боте" if any(c["known"] for c in candidates) else ""
        await card.edit_text(f"🔎 {html.escape(b['user_name'])}, это какая книга?{note}",
                             reply_markup=verify_kb(b["id"], candidates))
    elif candidates is not None:   # Google ответил, но похожего нет — мягко переспрашиваем
        who = f" ({html.escape(b['author'])})" if b["author"] else ""
        await card.edit_text(f"🤔 {html.escape(b['user_name'])}, не нашёл книгу «{html.escape(b['title'])}»{who}.\n"
                             f"Проверь название — или оставь как есть, если всё верно.",
                             reply_markup=not_found_kb(b["id"]))
    else:   # проверить не можем (Google недоступен) — идём дальше без вопроса
        await after_verify(card, b)


@router.callback_query(F.data.startswith("nf:"))
async def on_not_found(cb: CallbackQuery):
    _, book_id, choice = cb.data.split(":")
    b = await owned_book(cb, int(book_id))
    if not b:
        return
    await cb.answer()
    if choice == "ok":
        return await after_verify(cb.message, b)
    await cb.message.edit_text(f"📖 {html.escape(b['author'] + ' — ') if b['author'] else ''}…")
    await ask(cb.message, cb.from_user, "title", "📖 как называется книга?", "Например: Вишнёвый сад",
              author_id=b["author_id"], author_name=b["author"], book_id=b["id"], card=cb.message)


async def owned_book(cb: CallbackQuery, book_id: int) -> sqlite3.Row | None:
    b = get_book(book_id)
    if b is None:
        await cb.answer("Запись не найдена", show_alert=True)
        return None
    if b["user_id"] != cb.from_user.id:
        await cb.answer("Это не твоя книга 🙂", show_alert=True)
        return None
    chain_note(b["chat_id"], b["user_id"])   # человек на месте — цепочка не брошена
    return b


@router.callback_query(F.data.startswith("v:"))
async def on_verify(cb: CallbackQuery):
    _, book_id, choice = cb.data.split(":")
    b = await owned_book(cb, int(book_id))
    if not b:
        return
    candidates = pending_candidates.pop(b["id"], [])
    if choice != "-" and choice.isdigit() and int(choice) < len(candidates):
        c = candidates[int(choice)]   # для книги из бота — её написание, автор и ключ
        author, author_id, key = c["author"], c["author_id"], c["key"]
        if author_id is None and author and not c["known"]:   # вариант Google для книги без автора
            author_id = await resolve_author_quietly(author)
            author = get_author(author_id)["name"]
        key = key or key_for(author_id, author, c["title"])
        with db() as con:
            con.execute("UPDATE books SET author = ?, author_id = ?, title = ?, book_key = ?, verified = 1 "
                        "WHERE id = ?", (author, author_id, c["title"], key, b["id"]))
    await after_verify(cb.message, get_book(b["id"]))
    await cb.answer()


@router.callback_query(F.data.startswith("dp:"))
async def on_duplicate(cb: CallbackQuery):
    _, book_id, keep = cb.data.split(":")
    b = await owned_book(cb, int(book_id))
    if not b:
        return
    if keep == "1":
        await advance(cb.message, b)
        await cb.answer()
        return
    with db() as con:
        con.execute("DELETE FROM books WHERE id = ?", (b["id"],))
    await cb.answer("Не добавил — книга уже есть в списке")
    await chain_finish(cb.bot, b["chat_id"], b["user_id"], book_id=b["id"])
    try:
        await cb.message.delete()
    except Exception:
        await cb.message.edit_text(f"✖️ Не добавлено: {book_title_line(b)}")


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
    if not st or not st.get("prompt") or m.reply_to_message.message_id != st["prompt"].message_id:
        return
    text = m.text.strip()
    if not text:
        return
    del pending[key]
    if not chain_note(m.chat.id, m.from_user.id, m.message_id):   # вне цепочки — убираем сразу
        await drop(st["prompt"])
        await drop(m)

    if st["step"] == "author":
        author = None if text in ("-", "—", "–") else text[:100]
        await got_author(m, m.from_user, author, book_id=st.get("book_id"), card=st.get("card"))
        return

    if st["step"] == "feedback":
        await save_feedback(m, text)
        return

    if st["step"] == "title":
        await save_book(m, m.from_user, st["author_id"], st["author_name"], text[:200],
                        st.get("book_id"), st.get("card"))
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
    await chain_start(cb.bot, b["chat_id"], b["user_id"], "edit", book_id=b["id"], card_id=cb.message.message_id)
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
    await chain_finish(cb.bot, b["chat_id"], b["user_id"], book_id=b["id"])
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
    if not RELEASES:
        await m.answer("Список изменений пока недоступен.")
        return
    if (command.args or "").strip().lower() in ("all", "все"):
        for rel in RELEASES:   # по сообщению на версию — вся история не влезет в лимит Telegram
            await m.answer(release_text(rel))
        return
    text = release_text(RELEASES[0])
    if len(RELEASES) > 1:
        text += "\n\n<i>Вся история: /news all</i>"
    await m.answer(text)


async def announce_release(bot: Bot) -> None:
    """Один раз рассылает свежий релиз во все активные чаты, где его ещё не видели."""
    if not RELEASES:
        return
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
    ch = chain_get(m.chat.id, m.from_user.id)
    if b and ch and ch["book_id"] == b["id"]:   # отменили книгу, которую ещё записывают, — убираем и цепочку
        await chain_abort(m.bot, ch)
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
    await migrate_authors()   # старые записи -> авторы; без сети — попробуем при следующем запуске
    asyncio.create_task(announce_release(bot))
    asyncio.create_task(sweep_chains(bot))
    # drop_pending_updates=False — подхватываем сообщения, пришедшие пока ПК был выключен
    await dp.start_polling(bot, drop_pending_updates=False)


if __name__ == "__main__":
    asyncio.run(main())
