import os
import re
import time
import sqlite3
import asyncio
from dataclasses import dataclass
from typing import Optional, Dict, Tuple, List

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = set(int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit())
CHECK_EVERY_MIN = int(os.getenv("CHECK_EVERY_MIN", "10"))
DB_FILE = "updates_bot.db"

FOOTER = "\n\n— Bot created by @RekkoOwn"

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN")
if not ADMIN_IDS:
    raise RuntimeError("Missing ADMIN_IDS")


@dataclass
class GameSource:
    key: str
    name: str
    url: str
    base: str
    link_regex: re.Pattern


# SOURCES (some sites block scraping sometimes; this bot will show the real error in panel)
GAMES: Dict[str, GameSource] = {
    "deltaforce": GameSource(
        key="deltaforce",
        name="Delta Force Mobile",
        url="https://deltaforce.garena.com/en/news/all",
        base="https://deltaforce.garena.com",
        link_regex=re.compile(r"/en/news/"),
    ),
    "8ball": GameSource(
        key="8ball",
        name="8 Ball Pool Mobile",
        url="https://www.8ballpool.com/en/news",
        base="https://www.8ballpool.com",
        link_regex=re.compile(r"^/en/news/"),
    ),
    "arenabreakout": GameSource(
        key="arenabreakout",
        name="Arena Breakout Mobile",
        url="https://arenabreakout.com/web202210/news/notice.html",
        base="https://arenabreakout.com",
        link_regex=re.compile(r"/web202210/news/"),
    ),
    "mlbb": GameSource(
        key="mlbb",
        name="Mobile Legends: Bang Bang",
        url="https://www.mobilelegends.com/news",
        base="https://www.mobilelegends.com",
        link_regex=re.compile(r"/news/"),
    ),
    "freefire": GameSource(
        key="freefire",
        name="Free Fire Mobile",
        url="https://ff.garena.com/en/news/",
        base="https://ff.garena.com",
        link_regex=re.compile(r"/en/|/article/|/news/"),
    ),
    "codm": GameSource(
        key="codm",
        name="Call of Duty Mobile",
        url="https://www.callofduty.com/blog/mobile",
        base="https://www.callofduty.com",
        link_regex=re.compile(r"/blog/"),
    ),
    "bloodstrike": GameSource(
        key="bloodstrike",
        name="BloodStrike Mobile",
        url="https://www.blood-strike.com/m/news/",
        base="https://www.blood-strike.com",
        link_regex=re.compile(r"/news/|/update/"),
    ),
}

# Patch/update keyword prioritization
PATCH_KEYWORDS = [
    "patch", "patch notes", "update", "version", "hotfix", "bug fix", "bugfix",
    "maintenance", "optimisation", "optimization", "season", "ob", "balance"
]


# ----------------------------
# DB
# ----------------------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS topic_map (
            game_key TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            thread_id INTEGER NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (game_key, chat_id, thread_id)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS last_seen (
            game_key TEXT PRIMARY KEY,
            last_url TEXT,
            last_title TEXT,
            last_ts INTEGER
        )
        """)

def set_topic_binding(game_key: str, chat_id: int, thread_id: int, enabled: int = 1):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO topic_map(game_key, chat_id, thread_id, enabled) VALUES (?,?,?,?)",
            (game_key, chat_id, thread_id, enabled)
        )

def get_binding(game_key: str, chat_id: int, thread_id: int) -> Optional[int]:
    with db() as conn:
        row = conn.execute(
            "SELECT enabled FROM topic_map WHERE game_key=? AND chat_id=? AND thread_id=?",
            (game_key, chat_id, thread_id)
        ).fetchone()
    return int(row[0]) if row else None

def get_bound_targets(game_key: str) -> List[Tuple[int, int, int]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT chat_id, thread_id, enabled FROM topic_map WHERE game_key=?",
            (game_key,)
        ).fetchall()
    return [(int(r[0]), int(r[1]), int(r[2])) for r in rows]

def get_last_seen(game_key: str) -> Optional[str]:
    with db() as conn:
        row = conn.execute("SELECT last_url FROM last_seen WHERE game_key=?", (game_key,)).fetchone()
    return row[0] if row else None

def update_last_seen(game_key: str, title: str, url: str):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO last_seen(game_key, last_url, last_title, last_ts) VALUES (?,?,?,?)",
            (game_key, url, title, int(time.time()))
        )


# ----------------------------
# Helpers
# ----------------------------
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

def escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )

def absolutize(base: str, href: str) -> str:
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return base.rstrip("/") + href
    return base.rstrip("/") + "/" + href

def contains_patch_keywords(title: str) -> bool:
    t = title.lower()
    return any(k in t for k in PATCH_KEYWORDS)

async def http_get(url: str) -> httpx.Response:
    last_err = None
    for _ in range(3):
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=HEADERS) as client:
                r = await client.get(url)
                r.raise_for_status()
                return r
        except Exception as e:
            last_err = e
            await asyncio.sleep(2)
    raise last_err


# ----------------------------
# Scraping list page -> pick best "patch/update" candidate
# ----------------------------
def extract_candidates(html: str, source: GameSource) -> List[Tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    out: List[Tuple[str, str]] = []

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        if not source.link_regex.search(href):
            continue

        url = absolutize(source.base, href)
        title = (a.get_text(" ", strip=True) or a.get("title") or a.get("aria-label") or "").strip()
        title = re.sub(r"\s+", " ", title)

        # basic quality filter
        if len(title) < 6:
            continue
        if any(bad in title.lower() for bad in ["read more", "view more", "learn more"]):
            continue

        out.append((title, url))

    # de-dup by URL keep first occurrence
    seen = set()
    uniq = []
    for t, u in out:
        if u in seen:
            continue
        seen.add(u)
        uniq.append((t, u))
    return uniq

def pick_best_candidate(candidates: List[Tuple[str, str]]) -> Optional[Tuple[str, str]]:
    if not candidates:
        return None
    # Prefer patch/update titles first
    patch_first = [c for c in candidates if contains_patch_keywords(c[0])]
    return patch_first[0] if patch_first else candidates[0]


# ----------------------------
# Article page -> OG meta for media + desc
# ----------------------------
def parse_article_meta(article_html: str, fallback_title: str, fallback_url: str) -> Tuple[str, str, Optional[str], str]:
    soup = BeautifulSoup(article_html, "html.parser")

    def meta(prop: str) -> Optional[str]:
        tag = soup.find("meta", attrs={"property": prop})
        if tag and tag.get("content"):
            return tag["content"].strip()
        tag = soup.find("meta", attrs={"name": prop})
        if tag and tag.get("content"):
            return tag["content"].strip()
        return None

    title = meta("og:title") or meta("twitter:title") or fallback_title
    desc = meta("og:description") or meta("description") or meta("twitter:description") or ""
    img = meta("og:image") or meta("twitter:image")
    url = meta("og:url") or fallback_url

    desc = re.sub(r"\s+", " ", desc).strip()
    if len(desc) > 220:
        desc = desc[:217] + "..."

    return title, url, img, desc


async def fetch_latest_patchlike(source: GameSource) -> Optional[Tuple[str, str, Optional[str], str]]:
    """
    Returns: (title, url, image_url_or_none, desc)
    Picks a "patch/update" looking post first, otherwise falls back to newest.
    """
    list_resp = await http_get(source.url)
    candidates = extract_candidates(list_resp.text, source)
    best = pick_best_candidate(candidates)
    if not best:
        return None

    fallback_title, article_url = best

    try:
        art_resp = await http_get(article_url)
        title, url, img, desc = parse_article_meta(art_resp.text, fallback_title, article_url)
        return title, url, img, desc
    except Exception:
        return fallback_title, article_url, None, ""


# ----------------------------
# Sending (media + words + footer)
# ----------------------------
async def send_update_media(bot, chat_id: int, thread_id: Optional[int], game: GameSource,
                            title: str, url: str, img: Optional[str], desc: str):
    caption_lines = [
        f"🛠️ <b>{escape_html(game.name)} — Update / Patch</b>",
        f"<b>{escape_html(title)}</b>",
    ]
    if desc:
        caption_lines.append(escape_html(desc))
    caption_lines.append(escape_html(url))
    caption = "\n".join(caption_lines) + FOOTER

    if img:
        try:
            await bot.send_photo(
                chat_id=chat_id,
                message_thread_id=thread_id,
                photo=img,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
            return
        except Exception:
            pass

    await bot.send_message(
        chat_id=chat_id,
        message_thread_id=thread_id,
        text=caption,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=False,
    )


# ----------------------------
# Buttons UI (No Game List)
# ----------------------------
def panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Setup Topic / Channel", callback_data="panel:setup")],
        [InlineKeyboardButton("🛠️ Latest Update / Patch", callback_data="panel:latest")],
        [InlineKeyboardButton("🔔 Alerts ON / OFF", callback_data="panel:alerts")],
    ])

def games_keyboard(prefix: str) -> InlineKeyboardMarkup:
    rows = []
    keys = list(GAMES.keys())
    for i in range(0, len(keys), 2):
        row = []
        for k in keys[i:i+2]:
            row.append(InlineKeyboardButton(GAMES[k].name, callback_data=f"{prefix}:{k}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="panel:back")])
    return InlineKeyboardMarkup(rows)


# ----------------------------
# /News command (members use this)
# ----------------------------
async def news_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Works in private, groups, topics, channels (if bot gets updates)
    if not update.effective_chat:
        return
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        message_thread_id=update.effective_message.message_thread_id if update.effective_message and update.effective_message.is_topic_message else None,
        text="📰 <b>Updates Panel</b>\nTap buttons below." + FOOTER,
        parse_mode=ParseMode.HTML,
        reply_markup=panel_keyboard(),
    )

    # auto-delete the /News message in groups/topics if bot has permission
    try:
        if update.message and update.effective_chat.type in ("group", "supergroup"):
            await update.message.delete()
    except Exception:
        pass


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /start also opens same panel
    await news_cmd(update, context)


# ----------------------------
# Callbacks
# ----------------------------
def current_context_from_message(msg) -> Tuple[int, Optional[int]]:
    """
    Returns (chat_id, thread_id_or_none)
    thread_id is used only for topics.
    Channels do not have topics (thread_id None).
    """
    chat_id = msg.chat_id
    thread_id = msg.message_thread_id if getattr(msg, "is_topic_message", False) else None
    return chat_id, thread_id

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.message:
        return

    await q.answer()
    data = q.data or ""
    msg = q.message
    chat_id, thread_id = current_context_from_message(msg)

    if data == "panel:back":
        await q.edit_message_reply_markup(reply_markup=panel_keyboard())
        return

    if data == "panel:setup":
        await q.edit_message_text(
            "Select a game to bind here.\n\n"
            "✅ If you run /News inside a Topic, it binds THAT topic.\n"
            "✅ If you run /News inside a Channel, it binds THAT channel.\n"
            + FOOTER,
            parse_mode=ParseMode.HTML,
            reply_markup=games_keyboard("setup"),
        )
        return

    if data == "panel:latest":
        await q.edit_message_text(
            "Pick a game to send the latest Update / Patch:\n" + FOOTER,
            parse_mode=ParseMode.HTML,
            reply_markup=games_keyboard("latest"),
        )
        return

    if data == "panel:alerts":
        await q.edit_message_text(
            "Pick a game to toggle alerts for THIS place (topic/channel):\n" + FOOTER,
            parse_mode=ParseMode.HTML,
            reply_markup=games_keyboard("toggle"),
        )
        return

    # Setup binding
    if data.startswith("setup:"):
        game_key = data.split(":", 1)[1]
        if game_key not in GAMES:
            await q.edit_message_text("Unknown game." + FOOTER, reply_markup=panel_keyboard())
            return

        # Use thread_id if topic, else 0 for "no topic"
        bind_thread_id = thread_id if thread_id is not None else 0
        set_topic_binding(game_key, chat_id, bind_thread_id, enabled=1)

        await q.edit_message_text(
            f"✅ Bound here to:\n<b>{escape_html(GAMES[game_key].name)}</b>\nAlerts: <b>ON</b>{FOOTER}",
            parse_mode=ParseMode.HTML,
            reply_markup=panel_keyboard(),
        )
        return

    # Latest on demand
    if data.startswith("latest:"):
        game_key = data.split(":", 1)[1]
        if game_key not in GAMES:
            await q.edit_message_text("Unknown game." + FOOTER, reply_markup=panel_keyboard())
            return

        game = GAMES[game_key]
        await q.edit_message_text(f"⏳ Fetching latest update/patch for <b>{escape_html(game.name)}</b>...{FOOTER}",
                                  parse_mode=ParseMode.HTML)

        try:
            latest = await fetch_latest_patchlike(game)
            if not latest:
                await q.edit_message_text("Couldn't find a latest update/patch (site layout may have changed)." + FOOTER,
                                          reply_markup=panel_keyboard())
                return

            title, url, img, desc = latest

            send_thread_id = thread_id  # None for channels, topic id for topics
            await send_update_media(context.bot, chat_id, send_thread_id, game, title, url, img, desc)

            await q.edit_message_text("✅ Sent. Tap another option:" + FOOTER, parse_mode=ParseMode.HTML,
                                      reply_markup=panel_keyboard())
        except httpx.HTTPStatusError as e:
            await q.edit_message_text(
                f"❌ HTTP {e.response.status_code}\nSource: {escape_html(game.url)}{FOOTER}",
                parse_mode=ParseMode.HTML,
                reply_markup=panel_keyboard(),
            )
        except httpx.RequestError as e:
            await q.edit_message_text(
                f"❌ Network error\nSource: {escape_html(game.url)}\n{escape_html(str(e))}{FOOTER}",
                parse_mode=ParseMode.HTML,
                reply_markup=panel_keyboard(),
            )
        except Exception as e:
            await q.edit_message_text(
                f"❌ Error: {escape_html(type(e).__name__)}: {escape_html(str(e))}{FOOTER}",
                parse_mode=ParseMode.HTML,
                reply_markup=panel_keyboard(),
            )
        return

    # Toggle alerts
    if data.startswith("toggle:"):
        game_key = data.split(":", 1)[1]
        if game_key not in GAMES:
            await q.edit_message_text("Unknown game." + FOOTER, reply_markup=panel_keyboard())
            return

        bind_thread_id = thread_id if thread_id is not None else 0
        current = get_binding(game_key, chat_id, bind_thread_id)
        new_enabled = 0 if current == 1 else 1
        set_topic_binding(game_key, chat_id, bind_thread_id, enabled=new_enabled)

        await q.edit_message_text(
            f"🔔 Alerts for <b>{escape_html(GAMES[game_key].name)}</b> here: "
            f"<b>{'ON' if new_enabled else 'OFF'}</b>{FOOTER}",
            parse_mode=ParseMode.HTML,
            reply_markup=panel_keyboard(),
        )
        return


# ----------------------------
# Background job: posts new patch/update automatically
# ----------------------------
async def check_all_job(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    print("[JOB] check cycle", time.strftime("%Y-%m-%d %H:%M:%S"))

    for game_key, game in GAMES.items():
        try:
            latest = await fetch_latest_patchlike(game)
            if not latest:
                continue
            title, url, img, desc = latest

            last_url = get_last_seen(game_key)

            # baseline store (no spam on first run)
            if not last_url:
                update_last_seen(game_key, title, url)
                continue

            if url != last_url:
                update_last_seen(game_key, title, url)

                targets = get_bound_targets(game_key)
                for chat_id, thread_id, enabled in targets:
                    if not enabled:
                        continue
                    # thread_id=0 means "not a topic" (channel or normal group)
                    send_thread = None if thread_id == 0 else thread_id
                    await send_update_media(app.bot, chat_id, send_thread, game, title, url, img, desc)

        except Exception as e:
            print(f"[WARN] {game_key} check failed:", repr(e))


# ----------------------------
# Main
# ----------------------------
def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # Members use /News
    app.add_handler(CommandHandler(["news", "News"], news_cmd))
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))

    app.job_queue.run_repeating(check_all_job, interval=CHECK_EVERY_MIN * 60, first=10)

    print("Bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
