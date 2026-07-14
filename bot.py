#!/usr/bin/env python3
"""Telegram bot: guess anime by a random screenshot."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import sqlite3
import time
import unicodedata
from io import BytesIO
from pathlib import Path

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "anime.db"
SHIKIMORI_API = "https://shikimori.io/api"
SHIKIMORI_CDN = "https://shikimori.io"
MOVIESTILLS_BASE = "https://www.moviestillsdb.com"
USER_AGENT = "ani_guesser_bot/1.0"
MAX_TRIES = 12
MAX_TYPOS = 3
SUPER_USER_ID = 913414981

CB_SKIP = "guess:skip"
CB_HINT = "guess:hint"

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("ani_guesser")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

# Browser TLS impersonation helps with some CDNs / bot checks.
try:
    from curl_cffi import requests as curl_requests

    BROWSER_SESSION = curl_requests.Session(impersonate="chrome131")
except Exception:  # pragma: no cover
    BROWSER_SESSION = None

def load_token() -> str:
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env or environment")
    return token


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_score_tables() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chat_scores (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                score INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_chat_scores_rank
                ON chat_scores (chat_id, score DESC);
            """
        )


def game_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⏭ Skip", callback_data=CB_SKIP),
                InlineKeyboardButton("💡 Hint", callback_data=CB_HINT),
            ]
        ]
    )


def normalize(text: str | None) -> str:
    """Compare titles by letters/digits only (ignore hyphens, spaces, punctuation)."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.casefold().replace("ё", "е")
    text = text.replace("×", "x").replace("✕", "x")
    # Drop spaces, hyphens, colons, etc. → "человек-паук" == "человек паук" == "человекпаук"
    return "".join(ch for ch in text if ch.isalnum())


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if abs(len(a) - len(b)) > MAX_TYPOS:
        return MAX_TYPOS + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        row_min = i
        for j, cb in enumerate(b, 1):
            ins = cur[j - 1] + 1
            delete = prev[j] + 1
            sub = prev[j - 1] + (ca != cb)
            val = min(ins, delete, sub)
            cur.append(val)
            if val < row_min:
                row_min = val
        if row_min > MAX_TYPOS:
            return MAX_TYPOS + 1
        prev = cur
    return prev[-1]


def allowed_typos(length: int) -> int:
    # Titles shorter than 4 letters: exact match only.
    if length < 4:
        return 0
    if length <= 6:
        return 1
    if length <= 12:
        return 2
    return MAX_TYPOS


def titles_match(guess: str, alias: str) -> bool:
    g = normalize(guess)
    a = normalize(alias)
    if not g or not a:
        return False
    if g == a:
        return True
    # Short titles (< 4 letters): no typos, no substring match.
    if min(len(g), len(a)) < 4:
        return False
    shorter, longer = (g, a) if len(g) <= len(a) else (a, g)
    if shorter in longer:
        if len(longer) - len(shorter) <= max(6, len(shorter) // 2):
            return True
    limit = min(MAX_TYPOS, allowed_typos(min(len(g), len(a))))
    if limit == 0:
        return False
    return levenshtein(g, a) <= limit


def has_movies_table(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='movies'"
    ).fetchone()
    return bool(row)


def random_anime_entry(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        SELECT a.id, a.mal_id, a.series_id, a.title, a.title_romaji, a.title_english,
               a.title_native, a.title_russian, a.title_license_ru,
               a.url, a.format, a.episodes,
               s.name AS series_name, s.name_russian AS series_russian,
               s.name_license_ru AS series_license_ru
        FROM anime a
        LEFT JOIN anime_series s ON s.id = a.series_id
        WHERE a.mal_id IS NOT NULL
        ORDER BY RANDOM()
        LIMIT 1
        """
    ).fetchone()
    item = dict(row)
    item["kind"] = "anime"
    return item


def random_movie_entry(conn: sqlite3.Connection) -> dict | None:
    if not has_movies_table(conn):
        return None
    row = conn.execute(
        """
        SELECT m.id, m.imdb_id, m.title, m.title_russian, m.title_english,
               m.year, m.url, m.images, m.aliases, m.series_id,
               s.name AS series_name, s.name_russian AS series_russian
        FROM movies m
        LEFT JOIN movie_series s ON s.id = m.series_id
        ORDER BY RANDOM()
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["kind"] = "movie"
    return item


def collect_anime_aliases(conn: sqlite3.Connection, anime: dict) -> list[str]:
    aliases: list[str] = []

    def add(*values: str | None) -> None:
        for v in values:
            if v and normalize(v):
                aliases.append(v)

    add(
        anime.get("title"),
        anime.get("title_romaji"),
        anime.get("title_english"),
        anime.get("title_native"),
        anime.get("title_russian"),
        anime.get("title_license_ru"),
        anime.get("series_name"),
        anime.get("series_russian"),
        anime.get("series_license_ru"),
    )

    series_id = anime.get("series_id")
    if series_id:
        for row in conn.execute(
            """
            SELECT a.title, a.title_romaji, a.title_english, a.title_native,
                   a.title_russian, a.title_license_ru
            FROM anime a
            WHERE a.series_id = ?
            """,
            (series_id,),
        ):
            add(*row)

    seen: set[str] = set()
    unique: list[str] = []
    for alias in aliases:
        key = normalize(alias)
        if key not in seen:
            seen.add(key)
            unique.append(alias)
    return unique


def collect_movie_aliases(movie: dict) -> list[str]:
    aliases: list[str] = []
    try:
        stored = json.loads(movie.get("aliases") or "[]")
    except Exception:
        stored = []
    for v in [
        movie.get("title"),
        movie.get("title_russian"),
        movie.get("title_english"),
        movie.get("series_name"),
        movie.get("series_russian"),
        *stored,
    ]:
        if v and normalize(str(v)):
            aliases.append(str(v))
    seen: set[str] = set()
    unique: list[str] = []
    for alias in aliases:
        key = normalize(alias)
        if key not in seen:
            seen.add(key)
            unique.append(alias)
    return unique


def fetch_anime_screenshots(mal_id: int) -> list[str]:
    url = f"{SHIKIMORI_API}/animes/{mal_id}"
    resp = SESSION.get(url, timeout=30)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    data = resp.json()
    urls = []
    for shot in data.get("screenshots") or []:
        path = shot.get("original") or shot.get("preview")
        if not path:
            continue
        urls.append(path if path.startswith("http") else SHIKIMORI_CDN + path)
    return urls


def movie_search_query(movie: dict) -> str:
    title = ""
    for key in ("title_english", "title", "title_russian"):
        value = (movie.get(key) or "").strip()
        if value:
            title = value
            break
    if not title:
        return ""
    year = movie.get("year")
    if year:
        return f"{title} {year}"
    return title


def movie_match_titles(movie: dict) -> list[str]:
    titles: list[str] = []
    try:
        aliases = json.loads(movie.get("aliases") or "[]")
    except Exception:
        aliases = []
    for value in [
        movie.get("title_english"),
        movie.get("title"),
        movie.get("title_russian"),
        *aliases,
    ]:
        text = str(value or "").strip()
        if not text:
            continue
        # Drop year suffix from aliases for matching
        text = re.sub(r"\s*\((?:19|20)\d{2}\)\s*$", "", text).strip()
        if text and text not in titles:
            titles.append(text)
    return titles


def _title_matches(query: str, result_title: str) -> bool:
    from difflib import SequenceMatcher

    def prep(s: str) -> str:
        s = normalize(s)
        return s[4:] if s.startswith("the ") else s

    a, b = prep(query), prep(result_title)
    if not a or not b:
        return False
    if a == b:
        return True
    # Query is a prefix of a longer result title ("matrix" ⊂ "matrix reloaded").
    # Do NOT reverse this: "beetlejuice beetlejuice" must not match "beetlejuice".
    if b.startswith(a + " "):
        return True
    if a in b:
        # "no way home" inside "spider man no way home"
        shorter, longer = a, b
        if len(shorter) >= 5 and len(shorter) / max(len(longer), 1) >= 0.45:
            return True
    return SequenceMatcher(None, a, b).ratio() >= 0.92


def _msdb_http_get(url: str, **kwargs):
    if BROWSER_SESSION is None:
        return SESSION.get(url, **kwargs)
    return BROWSER_SESSION.get(url, **kwargs)


def search_moviestills_movie(
    query: str,
    match_titles: list[str] | None = None,
    year: int | None = None,
) -> str | None:
    """Search MovieStillsDB and return the best matching /movies/... path."""
    from bs4 import BeautifulSoup

    q = (query or "").strip()
    if not q:
        return None
    titles = [t for t in (match_titles or [q]) if (t or "").strip()]
    resp = _msdb_http_get(
        f"{MOVIESTILLS_BASE}/search",
        params={"query": q},
        headers={
            "accept": "text/html,application/xhtml+xml",
            "referer": f"{MOVIESTILLS_BASE}/",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")
    candidates: list[tuple[str, int | None, str]] = []
    for a in soup.select('a.font-bold[href*="/movies/"]'):
        href = (a.get("href") or "").strip()
        name = a.get_text(" ", strip=True)
        if not href.startswith("/movies/") or href.count("/") < 2:
            continue
        # Year often sits in the same card text.
        parent_text = a.parent.get_text(" ", strip=True) if a.parent else name
        found_year = None
        m = re.search(r"\b((?:19|20)\d{2})\b", parent_text)
        if m:
            found_year = int(m.group(1))
        candidates.append((name, found_year, href))

    if not candidates:
        return None

    # Prefer exact year + title match, then title-only.
    def score(item: tuple[str, int | None, str]) -> tuple[int, int]:
        name, found_year, _href = item
        title_ok = any(_title_matches(t, name) for t in titles)
        year_ok = year is not None and found_year == year
        return (2 if title_ok and year_ok else 1 if title_ok else 0, 1 if year_ok else 0)

    ranked = sorted(candidates, key=score, reverse=True)
    best_href = ranked[0][2]
    if score(ranked[0])[0] < 1:
        return None
    return best_href


def fetch_moviestills_from_movie_page(movie_path: str) -> str | None:
    """Parse stills from a MovieStillsDB movie page and return one landscape still URL."""
    import html as htmlmod

    from bs4 import BeautifulSoup

    path = movie_path if movie_path.startswith("http") else f"{MOVIESTILLS_BASE}{movie_path}"
    resp = _msdb_http_get(
        path,
        headers={
            "accept": "text/html,application/xhtml+xml",
            "referer": f"{MOVIESTILLS_BASE}/",
        },
        timeout=45,
    )
    if resp.status_code != 200:
        resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")
    gallery = next((n for n in soup.find_all(True) if n.has_attr(":initial-items")), None)
    if gallery is None:
        return None

    try:
        items = json.loads(htmlmod.unescape(gallery[":initial-items"]))
    except Exception:
        return None
    if not isinstance(items, list) or not items:
        return None

    def still_url(item: dict) -> str | None:
        preview = item.get("preview") or {}
        thumb = item.get("thumbnail") or {}
        return preview.get("path") or thumb.get("path")

    def is_landscape(item: dict) -> bool:
        w = int(item.get("width") or 0)
        h = int(item.get("height") or 0)
        return w > 0 and h > 0 and w >= h * 1.05

    landscape = [it for it in items if isinstance(it, dict) and is_landscape(it) and still_url(it)]
    pool = landscape or [it for it in items if isinstance(it, dict) and still_url(it)]
    if not pool:
        return None
    chosen = random.choice(pool)
    return still_url(chosen)


def fetch_moviestills_frame(
    query: str,
    match_titles: list[str] | None = None,
    year: int | None = None,
) -> str | None:
    """Live-fetch one still via MovieStillsDB search → movie page. Not stored in DB."""
    movie_path = search_moviestills_movie(query, match_titles=match_titles, year=year)
    if not movie_path:
        return None
    return fetch_moviestills_from_movie_page(movie_path)


def pick_random_frame(conn: sqlite3.Connection) -> tuple[dict, str, list[str]]:
    last_error = None
    movies_ready = has_movies_table(conn) and conn.execute(
        "SELECT 1 FROM movies LIMIT 1"
    ).fetchone()

    for _ in range(MAX_TRIES):
        want_movie = bool(movies_ready) and random.random() < 0.5
        if want_movie:
            movie = random_movie_entry(conn)
            if movie:
                query = movie_search_query(movie)
                try:
                    shot = fetch_moviestills_frame(
                        query,
                        match_titles=movie_match_titles(movie),
                        year=movie.get("year"),
                    )
                except Exception as e:
                    last_error = e
                    log.warning("moviestills fail %s: %s", query, e)
                    shot = None
                if shot:
                    return movie, shot, collect_movie_aliases(movie)
                time.sleep(0.2)
                continue

        anime = random_anime_entry(conn)
        try:
            shots = fetch_anime_screenshots(anime["mal_id"])
        except Exception as e:
            last_error = e
            log.warning("screenshots fail mal=%s: %s", anime["mal_id"], e)
            time.sleep(0.3)
            continue
        if shots:
            return anime, random.choice(shots), collect_anime_aliases(conn, anime)
        time.sleep(0.2)
    raise RuntimeError(f"No screenshots found after {MAX_TRIES} tries: {last_error}")


def display_answer(item: dict) -> str:
    title = item.get("title_russian") or item.get("title")
    series = item.get("series_russian") or item.get("series_name")
    kind = "🎬 Фильм" if item.get("kind") == "movie" else "🎌 Аниме"
    lines = [f"{kind}: {title}"]
    if item.get("year"):
        lines[0] += f" ({item['year']})"
    if series and normalize(series) != normalize(title or ""):
        lines.append(f"📂 Серия: {series}")
    return "\n".join(lines)


def check_guess(guess: str, aliases: list[str]) -> bool:
    return any(titles_match(guess, alias) for alias in aliases)


def add_score(chat_id: int, user) -> int:
    username = getattr(user, "username", None)
    full_name = (user.full_name if user else None) or "Игрок"
    user_id = user.id
    with db() as conn:
        conn.execute(
            """
            INSERT INTO chat_scores (chat_id, user_id, username, full_name, score)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                score = score + 1,
                username = excluded.username,
                full_name = excluded.full_name
            """,
            (chat_id, user_id, username, full_name),
        )
        row = conn.execute(
            "SELECT score FROM chat_scores WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()
        return int(row["score"])


def ranking_text(chat_id: int, limit: int = 15) -> str:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT username, full_name, score
            FROM chat_scores
            WHERE chat_id = ?
            ORDER BY score DESC, full_name ASC
            LIMIT ?
            """,
            (chat_id, limit),
        ).fetchall()
    if not rows:
        return "🏆 Рейтинг этого чата пока пуст."
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = ["🏆 Рейтинг чата:"]
    for i, row in enumerate(rows, 1):
        name = f"@{row['username']}" if row["username"] else row["full_name"]
        medal = medals.get(i, f"{i}.")
        lines.append(f"{medal} {name} — {row['score']}")
    return "\n".join(lines)


def is_active(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return bool(context.chat_data.get("active"))


def controller_id(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    return context.chat_data.get("controller_id")


def set_controller(context: ContextTypes.DEFAULT_TYPE, user) -> None:
    if not user:
        return
    context.chat_data["controller_id"] = user.id
    context.chat_data["controller_name"] = (
        f"@{user.username}" if user.username else user.full_name
    )


def controller_name(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.chat_data.get("controller_name") or "ведущий"


def can_control(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if user and user.id == SUPER_USER_ID:
        return True
    cid = controller_id(context)
    if not user or cid is None:
        return False
    return user.id == cid


async def deny_control(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = f"Кнопки только у {controller_name(context)}."
    if update.callback_query:
        await update.callback_query.answer(msg, show_alert=True)
    else:
        await reply_text(update, f"⛔ {msg}")


def make_hint(title: str) -> str:
    letter_idxs = [i for i, c in enumerate(title) if c.isalnum()]
    if not letter_idxs:
        return ""
    n = len(letter_idxs)
    show = max(1, n // 3)
    start = random.randint(0, max(0, n - show))
    reveal = set(letter_idxs[start : start + show])
    out = []
    for i, ch in enumerate(title):
        if ch.isalnum():
            out.append(ch if i in reveal else "•")
        else:
            out.append(ch)
    return "".join(out)


async def reply_photo(update: Update, photo, caption: str) -> None:
    keyboard = game_keyboard()
    if update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_photo(
            photo=photo, caption=caption, reply_markup=keyboard
        )
    elif update.message:
        await update.message.reply_photo(
            photo=photo, caption=caption, reply_markup=keyboard
        )


async def reply_text(update: Update, text: str, with_keyboard: bool = False) -> None:
    markup = game_keyboard() if with_keyboard else None
    kwargs = {"text": text, "reply_markup": markup, "disable_web_page_preview": True}
    if update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_text(**kwargs)
    elif update.message:
        await update.message.reply_text(**kwargs)


async def send_chat_action(update: Update, action: str = ChatAction.UPLOAD_PHOTO) -> None:
    chat = update.effective_chat
    if chat:
        await chat.send_action(action)


async def send_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_active(context):
        return

    await send_chat_action(update)

    def _pick() -> tuple[dict, str, list[str]]:
        conn = db()
        try:
            return pick_random_frame(conn)
        finally:
            conn.close()

    try:
        # HTTP to MovieStillsDB/Shikimori is sync — keep the event loop free.
        item, frame_url, aliases = await asyncio.to_thread(_pick)
    except Exception as e:
        log.exception("pick frame failed")
        await reply_text(update, f"Не удалось найти кадр: {e}")
        return

    context.chat_data["challenge"] = {
        "kind": item.get("kind"),
        "id": item["id"],
        "series_id": item.get("series_id"),
        "aliases": aliases,
        "answer": display_answer(item),
        "url": item.get("url"),
        "title": item.get("title_russian") or item.get("title"),
    }
    context.chat_data["attempts"] = 0

    caption = (
        "🎯 Угадай тайтл по кадру (аниме или фильм)!\n"
        "Напиши название (рус / eng / серия).\n"
        "Опечатки в 2–3 буквы ок.\n"
        f"Skip / Hint: {controller_name(context)}"
    )

    # Always upload bytes ourselves: Telegram's URL fetch can fail for some CDNs.
    try:
        def _download() -> tuple[bytes, str]:
            headers = {"User-Agent": USER_AGENT}
            if "moviestillsdb.com" in frame_url:
                headers["Referer"] = f"{MOVIESTILLS_BASE}/"
            resp = SESSION.get(frame_url, timeout=60, headers=headers)
            resp.raise_for_status()
            data = resp.content
            # Sniff real type — anime stills may be PNG while movies are JPEG.
            if data.startswith(b"\x89PNG"):
                name = "still.png"
            elif data[:3] == b"GIF":
                name = "still.gif"
            elif data.startswith(b"RIFF") and b"WEBP" in data[:16]:
                name = "still.webp"
            else:
                name = "still.jpg"
            return data, name

        raw, filename = await asyncio.to_thread(_download)
        photo = InputFile(BytesIO(raw), filename=filename)
        await reply_photo(update, photo, caption)
    except Exception as e:
        log.exception("send photo failed")
        await reply_text(update, f"Не удалось отправить кадр: {e}")
        return

    log.info(
        "challenge chat=%s kind=%s title=%s aliases=%d",
        update.effective_chat.id if update.effective_chat else "?",
        item.get("kind"),
        item.get("title"),
        len(aliases),
    )


async def do_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_active(context):
        if update.callback_query:
            await update.callback_query.answer("Игра не запущена", show_alert=True)
        else:
            await reply_text(update, "Игра не запущена. /start_guess")
        return
    if not can_control(update, context):
        await deny_control(update, context)
        return
    if update.callback_query:
        await update.callback_query.answer()
    challenge = context.chat_data.get("challenge")
    if challenge:
        await reply_text(update, f"Пропуск.\nПравильный ответ:\n🎬 {challenge['answer']}")
        context.chat_data.pop("challenge", None)
    await send_challenge(update, context)


async def do_hint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_active(context):
        if update.callback_query:
            await update.callback_query.answer("Игра не запущена", show_alert=True)
        else:
            await reply_text(update, "Игра не запущена. /start_guess")
        return
    if not can_control(update, context):
        await deny_control(update, context)
        return
    if update.callback_query:
        await update.callback_query.answer()
    challenge = context.chat_data.get("challenge")
    if not challenge:
        await reply_text(update, "Сейчас нет активного кадра. Нажми Skip или /start_guess")
        return
    hint = make_hint(challenge.get("title") or "")
    if not hint:
        await reply_text(update, "Подсказку дать нечего — жми Skip", with_keyboard=True)
        return
    await reply_text(update, f"💡 Подсказка: {hint}", with_keyboard=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply_text(
        update,
        "Привет! Это игра «угадай аниме по кадру».\n\n"
        "/start_guess — начать игру в этом чате\n"
        "/stop_guess — остановить\n"
        "/ranking — рейтинг чата\n"
        "/skip /hint — или кнопки под кадром",
    )


async def start_guess(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_active(context) and context.chat_data.get("challenge"):
        await reply_text(update, "Игра уже идёт. Угадывай или жми Skip / Hint.")
        return
    context.chat_data["active"] = True
    set_controller(context, update.effective_user)
    await reply_text(
        update,
        f"🎮 Игра запущена!\nSkip / Hint управляет: {controller_name(context)}",
    )
    await send_challenge(update, context)


async def stop_guess(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    challenge = context.chat_data.get("challenge")
    context.chat_data["active"] = False
    context.chat_data.pop("challenge", None)
    context.chat_data.pop("controller_id", None)
    context.chat_data.pop("controller_name", None)
    context.chat_data["attempts"] = 0
    text = "⏹ Игра остановлена."
    if challenge:
        text += f"\nПоследний ответ был:\n🎬 {challenge['answer']}"
    text += "\n\n" + ranking_text(update.effective_chat.id)
    await reply_text(update, text)


async def ranking_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply_text(update, ranking_text(update.effective_chat.id))


async def skip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await do_skip(update, context)


async def hint_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await do_hint(update, context)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    if data == CB_SKIP:
        await do_skip(update, context)
    elif data == CB_HINT:
        await do_hint(update, context)


async def on_guess(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    if not is_active(context):
        return  # ignore chat noise when game is off

    text = update.message.text.strip()
    challenge = context.chat_data.get("challenge")
    if not challenge:
        await update.message.reply_text(
            "Нет активного кадра. /start_guess",
            disable_web_page_preview=True,
        )
        return

    context.chat_data["attempts"] = int(context.chat_data.get("attempts") or 0) + 1
    attempts = context.chat_data["attempts"]

    if check_guess(text, challenge["aliases"]):
        score = add_score(update.effective_chat.id, update.effective_user)
        answer = challenge["answer"]
        user = update.effective_user
        set_controller(context, user)
        who = f"@{user.username}" if user and user.username else (user.full_name if user else "Игрок")
        msg = (
            f"✅ Верно! {who}\n"
            f"🎬 {answer}\n"
            f"Skip / Hint теперь у {who}"
        )
        await update.message.reply_text(msg, disable_web_page_preview=True)
        context.chat_data.pop("challenge", None)
        await send_challenge(update, context)


def main() -> None:
    token = load_token()
    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}")
    init_score_tables()
    log.info("Movie stills: MovieStillsDB live fetch enabled")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("start_guess", start_guess))
    app.add_handler(CommandHandler("stop_guess", stop_guess))
    app.add_handler(CommandHandler("ranking", ranking_cmd))
    app.add_handler(CommandHandler("top", ranking_cmd))
    app.add_handler(CommandHandler("skip", skip_cmd))
    app.add_handler(CommandHandler("next", skip_cmd))
    app.add_handler(CommandHandler("hint", hint_cmd))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^guess:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_guess))

    log.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
