#!/usr/bin/env python3
"""Telegram bot: guess anime by a random screenshot."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
import unicodedata
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Any, Iterator

import psycopg2
import psycopg2.extensions
import requests
from psycopg2.extras import RealDictCursor
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ChatAction, ChatMemberStatus, ChatType
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BASE_DIR = Path(__file__).parent
SHIKIMORI_API = "https://shikimori.io/api"
SHIKIMORI_CDN = "https://shikimori.io"
MOVIESTILLS_BASE = "https://www.moviestillsdb.com"
USER_AGENT = "ani_guesser_bot/1.0"
MAX_TRIES = 12
MAX_TYPOS = 3
SUPER_USER_ID = 913414981
DEFAULT_TAKEOVER_MINUTES = 10

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


def _load_dotenv() -> dict[str, str]:
    env: dict[str, str] = {}
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return env
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


_DOTENV = _load_dotenv()


def load_token() -> str:
    token = (
        os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        or _DOTENV.get("TELEGRAM_BOT_TOKEN", "").strip()
    )
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env or environment")
    return token


def database_url() -> str:
    url = (
        os.environ.get("DATABASE_URL", "").strip()
        or _DOTENV.get("DATABASE_URL", "").strip()
    )
    if not url:
        raise SystemExit("Set DATABASE_URL in .env or environment")
    return url


@contextmanager
def db() -> Iterator[psycopg2.extensions.connection]:
    conn = psycopg2.connect(database_url(), cursor_factory=RealDictCursor)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fetchone(conn, sql: str, params: tuple | list | None = None) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def fetchall(conn, sql: str, params: tuple | list | None = None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def execute(conn, sql: str, params: tuple | list | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(sql, params)


def init_score_tables() -> None:
    with db() as conn:
        execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS chat_scores (
                chat_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                username TEXT,
                full_name TEXT,
                score BIGINT NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            )
            """,
        )
        execute(
            conn,
            """
            CREATE INDEX IF NOT EXISTS idx_chat_scores_rank
                ON chat_scores (chat_id, score DESC)
            """,
        )
        execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id BIGINT PRIMARY KEY,
                hint_cooldown INTEGER NOT NULL DEFAULT 0,
                skip_cooldown INTEGER NOT NULL DEFAULT 0
            )
            """,
        )
        execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
        )


def get_chat_settings(chat_id: int) -> dict[str, int]:
    with db() as conn:
        row = fetchone(
            conn,
            """
            SELECT hint_cooldown, skip_cooldown
            FROM chat_settings
            WHERE chat_id = %s
            """,
            (chat_id,),
        )
    if not row:
        return {"hint_cooldown": 0, "skip_cooldown": 0}
    return {
        "hint_cooldown": max(0, int(row["hint_cooldown"] or 0)),
        "skip_cooldown": max(0, int(row["skip_cooldown"] or 0)),
    }


def get_bot_setting(key: str, default: str = "") -> str:
    with db() as conn:
        row = fetchone(
            conn,
            "SELECT value FROM bot_settings WHERE key = %s",
            (key,),
        )
    if not row:
        return default
    return str(row["value"] or default)


def set_bot_setting(key: str, value: str) -> None:
    with db() as conn:
        execute(
            conn,
            """
            INSERT INTO bot_settings (key, value)
            VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            (key, value),
        )


def get_takeover_minutes() -> int:
    raw = get_bot_setting("takeover_minutes", str(DEFAULT_TAKEOVER_MINUTES))
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_TAKEOVER_MINUTES


def set_takeover_minutes(minutes: int) -> int:
    minutes = max(0, int(minutes))
    set_bot_setting("takeover_minutes", str(minutes))
    return minutes


def set_chat_cooldown(chat_id: int, kind: str, seconds: int) -> int:
    seconds = max(0, int(seconds))
    if kind not in ("hint", "skip"):
        raise ValueError(f"unknown cooldown kind: {kind}")
    column = "hint_cooldown" if kind == "hint" else "skip_cooldown"
    with db() as conn:
        execute(
            conn,
            f"""
            INSERT INTO chat_settings (chat_id, hint_cooldown, skip_cooldown)
            VALUES (%s, 0, 0)
            ON CONFLICT (chat_id) DO NOTHING
            """,
            (chat_id,),
        )
        execute(
            conn,
            f"UPDATE chat_settings SET {column} = %s WHERE chat_id = %s",
            (seconds, chat_id),
        )
    return seconds


def cooldown_remaining(context: ContextTypes.DEFAULT_TYPE, kind: str, seconds: int) -> int:
    if seconds <= 0:
        return 0
    key = f"last_{kind}_at"
    last = float(context.chat_data.get(key) or 0)
    if not last:
        return 0
    left = int(seconds - (time.time() - last))
    return max(0, left)


def mark_cooldown_used(context: ContextTypes.DEFAULT_TYPE, kind: str) -> None:
    context.chat_data[f"last_{kind}_at"] = time.time()


async def deny_cooldown(
    update: Update, kind: str, remaining: int
) -> None:
    label = "Hint" if kind == "hint" else "Skip"
    msg = f"⏳ {label} на кулдауне: ещё {remaining} сек."
    if update.callback_query:
        await update.callback_query.answer(msg, show_alert=True)
    # Команда без кнопки — без сообщения в чат (только answer у callback).


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


def has_movies_table(conn) -> bool:
    row = fetchone(
        conn,
        """
        SELECT 1 AS ok
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'movies'
        """,
    )
    return bool(row)


def random_anime_entry(conn) -> dict:
    row = fetchone(
        conn,
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
        """,
    )
    if not row:
        raise RuntimeError("anime table is empty")
    item = dict(row)
    item["kind"] = "anime"
    return item


def random_movie_entry(conn) -> dict | None:
    if not has_movies_table(conn):
        return None
    row = fetchone(
        conn,
        """
        SELECT m.id, m.imdb_id, m.title, m.title_russian, m.title_english,
               m.year, m.url, m.images, m.aliases, m.series_id,
               s.name AS series_name, s.name_russian AS series_russian
        FROM movies m
        LEFT JOIN movie_series s ON s.id = m.series_id
        ORDER BY RANDOM()
        LIMIT 1
        """,
    )
    if not row:
        return None
    item = dict(row)
    item["kind"] = "movie"
    return item


def collect_anime_aliases(conn, anime: dict) -> list[str]:
    aliases: list[str] = []

    def add(*values: Any) -> None:
        for v in values:
            if v and normalize(str(v)):
                aliases.append(str(v))

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
        for row in fetchall(
            conn,
            """
            SELECT a.title, a.title_romaji, a.title_english, a.title_native,
                   a.title_russian, a.title_license_ru
            FROM anime a
            WHERE a.series_id = %s
            """,
            (series_id,),
        ):
            add(
                row["title"],
                row["title_romaji"],
                row["title_english"],
                row["title_native"],
                row["title_russian"],
                row["title_license_ru"],
            )

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


def pick_random_frame(conn) -> tuple[dict, str, list[str]]:
    last_error = None
    movies_ready = has_movies_table(conn) and fetchone(
        conn, "SELECT 1 AS ok FROM movies LIMIT 1"
    )

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
        execute(
            conn,
            """
            INSERT INTO chat_scores (chat_id, user_id, username, full_name, score)
            VALUES (%s, %s, %s, %s, 1)
            ON CONFLICT (chat_id, user_id) DO UPDATE SET
                score = chat_scores.score + 1,
                username = EXCLUDED.username,
                full_name = EXCLUDED.full_name
            """,
            (chat_id, user_id, username, full_name),
        )
        row = fetchone(
            conn,
            "SELECT score FROM chat_scores WHERE chat_id = %s AND user_id = %s",
            (chat_id, user_id),
        )
        return int(row["score"])


def ranking_text(chat_id: int, limit: int = 15) -> str:
    with db() as conn:
        rows = fetchall(
            conn,
            """
            SELECT username, full_name, score
            FROM chat_scores
            WHERE chat_id = %s
            ORDER BY score DESC, full_name ASC
            LIMIT %s
            """,
            (chat_id, limit),
        )
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
    context.chat_data["controller_at"] = time.time()


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


def takeover_remaining(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Seconds until others can take over. None = feature off. 0 = ready now."""
    minutes = get_takeover_minutes()
    if minutes <= 0:
        return None
    started = float(context.chat_data.get("controller_at") or 0)
    if not started:
        return 0
    left = int(minutes * 60 - (time.time() - started))
    return max(0, left)


def can_takeover(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Another player may seize Skip / start a new game after the idle timeout."""
    if can_control(update, context):
        return False
    left = takeover_remaining(context)
    return left is not None and left == 0


async def is_chat_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return False
    if user.id == SUPER_USER_ID:
        return True
    if chat.type == ChatType.PRIVATE:
        return True
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except Exception:
        log.exception("get_chat_member failed chat=%s user=%s", chat.id, user.id)
        return False
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


async def deny_control(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    left = takeover_remaining(context)
    if left is None:
        msg = f"Кнопки только у {controller_name(context)}."
    elif left > 0:
        mins = (left + 59) // 60
        msg = (
            f"Кнопки у {controller_name(context)}. "
            f"Перехватить можно через ~{mins} мин."
        )
    else:
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
        with db() as conn:
            return pick_random_frame(conn)

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
    may_takeover = can_takeover(update, context)
    if not can_control(update, context) and not may_takeover:
        await deny_control(update, context)
        return
    chat = update.effective_chat
    settings = get_chat_settings(chat.id) if chat else {"skip_cooldown": 0}
    left = cooldown_remaining(context, "skip", settings["skip_cooldown"])
    if left > 0:
        await deny_cooldown(update, "skip", left)
        return
    took_over = False
    if may_takeover:
        set_controller(context, update.effective_user)
        took_over = True
    if update.callback_query:
        await update.callback_query.answer()
    challenge = context.chat_data.get("challenge")
    if challenge:
        prefix = (
            f"🔄 Управление перешло к {controller_name(context)}.\n"
            if took_over
            else ""
        )
        await reply_text(
            update,
            f"{prefix}Пропуск.\nПравильный ответ:\n🎬 {challenge['answer']}",
        )
        context.chat_data.pop("challenge", None)
    elif took_over:
        await reply_text(
            update,
            f"🔄 Управление перешло к {controller_name(context)}.",
        )
    mark_cooldown_used(context, "skip")
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
    chat = update.effective_chat
    settings = get_chat_settings(chat.id) if chat else {"hint_cooldown": 0}
    left = cooldown_remaining(context, "hint", settings["hint_cooldown"])
    if left > 0:
        await deny_cooldown(update, "hint", left)
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
    mark_cooldown_used(context, "hint")
    await reply_text(update, f"💡 Подсказка: {hint}", with_keyboard=True)


def _parse_cooldown_arg(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    if not context.args:
        return None
    raw = (context.args[0] or "").strip().lower().rstrip("s")
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("нужно число секунд, например: 15")
    if value < 0 or value > 3600:
        raise ValueError("кулдаун: от 0 до 3600 секунд")
    return value


def _parse_minutes_arg(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    if not context.args:
        return None
    raw = (context.args[0] or "").strip().lower().rstrip("m")
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("нужно число минут, например: 5")
    if value < 0 or value > 1440:
        raise ValueError("таймаут: от 0 до 1440 минут")
    return value


async def hint_cd_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat:
        return
    try:
        value = _parse_cooldown_arg(context)
    except ValueError as e:
        await reply_text(update, f"⛔ {e}\nПример: /hint_cd 20")
        return
    if value is None:
        current = get_chat_settings(chat.id)["hint_cooldown"]
        left = cooldown_remaining(context, "hint", current)
        text = f"💡 Кулдаун Hint: {current} сек." if current else "💡 Кулдаун Hint: выключен."
        if current and left > 0:
            text += f"\nОсталось до следующего: {left} сек."
        text += "\nЗадать: /hint_cd 20  (0 = выкл, только админ чата)"
        await reply_text(update, text)
        return
    if not await is_chat_admin(update, context):
        await reply_text(update, "⛔ Кулдауны кнопок может менять только админ чата.")
        return
    set_chat_cooldown(chat.id, "hint", value)
    if value == 0:
        await reply_text(update, "💡 Кулдаун Hint выключен.")
    else:
        await reply_text(update, f"💡 Кулдаун Hint: {value} сек.")


async def skip_cd_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat:
        return
    try:
        value = _parse_cooldown_arg(context)
    except ValueError as e:
        await reply_text(update, f"⛔ {e}\nПример: /skip_cd 30")
        return
    if value is None:
        current = get_chat_settings(chat.id)["skip_cooldown"]
        left = cooldown_remaining(context, "skip", current)
        text = f"⏭ Кулдаун Skip: {current} сек." if current else "⏭ Кулдаун Skip: выключен."
        if current and left > 0:
            text += f"\nОсталось до следующего: {left} сек."
        text += "\nЗадать: /skip_cd 30  (0 = выкл, только админ чата)"
        await reply_text(update, text)
        return
    if not await is_chat_admin(update, context):
        await reply_text(update, "⛔ Кулдауны кнопок может менять только админ чата.")
        return
    set_chat_cooldown(chat.id, "skip", value)
    if value == 0:
        await reply_text(update, "⏭ Кулдаун Skip выключен.")
    else:
        await reply_text(update, f"⏭ Кулдаун Skip: {value} сек.")


async def takeover_cd_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != SUPER_USER_ID:
        await reply_text(update, "⛔ Эту настройку может менять только суперадмин.")
        return
    try:
        value = _parse_minutes_arg(context)
    except ValueError as e:
        await reply_text(update, f"⛔ {e}\nПример: /takeover_cd 5")
        return
    if value is None:
        current = get_takeover_minutes()
        if current:
            text = (
                f"⏱ Перехват управления: через {current} мин. после назначения ведущего "
                f"другой игрок может Skip (кнопки перейдут к нему) или /start_guess."
            )
            left = takeover_remaining(context)
            if left is not None and is_active(context):
                if left > 0:
                    text += f"\nСейчас до перехвата: ~{(left + 59) // 60} мин."
                else:
                    text += "\nСейчас перехват уже доступен."
        else:
            text = "⏱ Перехват управления выключен (0)."
        text += "\nЗадать: /takeover_cd 5  (0 = выкл)"
        await reply_text(update, text)
        return
    set_takeover_minutes(value)
    if value == 0:
        await reply_text(update, "⏱ Перехват управления выключен.")
    else:
        await reply_text(
            update,
            f"⏱ Перехват управления: {value} мин.\n"
            "После этого другой игрок может Skip (станут его кнопки) или начать новую игру.",
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply_text(
        update,
        "Привет! Это игра «угадай аниме по кадру».\n\n"
        "/start_guess — начать игру в этом чате\n"
        "/stop_guess — остановить\n"
        "/ranking — рейтинг чата\n"
        "/skip /hint — или кнопки под кадром\n"
        "/skip_cd [сек] — кулдаун Skip (админ чата)\n"
        "/hint_cd [сек] — кулдаун Hint (админ чата)\n"
        "/takeover_cd [мин] — через сколько мин. можно перехватить ведущего",
    )


async def start_guess(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_active(context) and context.chat_data.get("challenge"):
        if can_takeover(update, context):
            challenge = context.chat_data.get("challenge")
            set_controller(context, update.effective_user)
            prefix = f"🔄 Управление перешло к {controller_name(context)}.\n"
            if challenge:
                await reply_text(
                    update,
                    f"{prefix}Новая игра.\nПравильный ответ был:\n🎬 {challenge['answer']}",
                )
                context.chat_data.pop("challenge", None)
            else:
                await reply_text(update, f"{prefix}Новая игра.")
            context.chat_data["active"] = True
            await send_challenge(update, context)
            return
        left = takeover_remaining(context)
        if left is not None and left > 0:
            await reply_text(
                update,
                f"Игра уже идёт (ведущий: {controller_name(context)}). "
                f"Перехватить /start_guess или Skip можно через ~{(left + 59) // 60} мин.",
            )
        else:
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
    context.chat_data.pop("controller_at", None)
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
    # Fail fast if DATABASE_URL / network / schema are wrong.
    database_url()
    init_score_tables()
    log.info("Database: PostgreSQL")
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
    app.add_handler(CommandHandler("hint_cd", hint_cd_cmd))
    app.add_handler(CommandHandler("skip_cd", skip_cd_cmd))
    app.add_handler(CommandHandler("takeover_cd", takeover_cd_cmd))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^guess:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_guess))

    # Python 3.14+: asyncio.get_event_loop() no longer creates a loop for PTB.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    log.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
