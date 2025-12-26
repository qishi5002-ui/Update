import os
import re
import time
import json
import sqlite3
from dataclasses import dataclass
from typing import Optional, Dict, Tuple, List

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = set(int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit())
CHECK_EVERY_MIN = int(os.getenv("CHECK_EVERY_MIN", "10"))

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN in .env")
if not ADMIN_IDS:
    raise RuntimeError("Missing ADMIN_IDS in .env (comma-separated numeric Telegram IDs)")

DB_FILE = "updates_bot.db"


# ----------------------------
# Game Sources (edit anytime)
# ----------------------------
@dataclass
class GameSource:
    key: str
    name: str
    url: str
    base: str
    # A regex that the "best" article links usually match for that site
    link_regex: re.Pattern


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
        link_regex=re.compile(r"/en/article/|/en/news/"),
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
        link_regex=re.compile(r"/news/"),
    ),
}


# ----------------------------
# SQLite helpers
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

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ----------------------------
# Scraping (simple + robust-ish)
# ----------------------------
def absolutize(base: str, href: str) -> str:
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return base.rstrip("/") + href
    return base.rstrip("/") + "/" + href

def extract_latest_article(html: str, source: GameSource) -> Optional[Tuple[str, str]]:
    """
    Returns (title, url) for the best/latest link found.
    We grab ALL <a href> that match source.link_regex, then pick the first unique URL
    that looks like an article.
    """
    soup = BeautifulSoup(html, "html.parser")
    candidates: List[Tuple[str, str]] = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        if not source.link_regex.search(href):
            continue

        url = absolutize(source.base, href)

        # Title: prefer the anchor text; fallback to aria-label/title
        title = (a.get_text(" ", strip=True) or a.get("title") or a.get("aria-label") or "").strip()
        title = re.sub(r"\s+", " ", title)

        # Avoid junk / nav
        if len(title) < 6:
            continue
        if any(bad in title.lower() for bad in ["read more", "learn more", "click here", "view more", "more"]):
            continue

        candidates.append((title, url))

    # de-dup by URL, keep first occurrence
    seen = set()
    unique = []
    for t, u in candidates:
        if u in seen:
            continue
        seen.add(u)
        unique.append((t, u))

    return unique[0] if unique else None


async def fetch_latest(source: GameSource) -> Optional[Tuple[str, str]]:
    headers = {
        "User-Agent": "Mozilla/5.0 (TelegramBotUpdatesWatcher/1.0)"
    }
    async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers=headers) as client:
        r = await client.get(source.url)
        r.raise_for_status()
        latest = extract_latest_article(r.text, source)
        return latest


# ----------------------------
# Posting logic
# ----------------------------
def get_bound_topics(game_key: str) -> List[Tuple[int, int, int]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT chat_id, thread_id, enabled FROM topic_map WHERE game_key=?",
            (game_key,)
        ).fetchall()
    return [(int(r[0]), int(r[1]), int(r[2])) for r in rows]

def set_topic_binding(game_key: str, chat_id: int, thread_id: int, enabled: int = 1):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO topic_map(game_key, chat_id, thread_id, enabled) VALUES (?,?,?,?)",
            (game_key, chat_id, thread_id, enabled)
        )

def set_enabled(game_key: str, chat_id: int, thread_id: int, enabled: int):
    with db() as conn:
        conn.execute(
            "UPDATE topic_map SET enabled=? WHERE game_key=? AND chat_id=? AND thread_id=?",
            (enabled, game_key, chat_id, thread_id)
        )

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


async def post_update(app: Application, game: GameSource, title: str, url: str):
    text = f"🆕 <b>{game.name}</b>\n<b>{escape_html(title)}</b>\n{escape_html(url)}"
    targets = get_bound_topics(game.key)

    for chat_id, thread_id, enabled in targets:
        if not enabled:
            continue
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
        except Exception as e:
            # Don’t crash the whole job if one topic fails
            print(f"[WARN] Failed to post to chat={chat_id} thread={thread_id}: {e}")

def escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )


# ----------------------------
# Commands
# ----------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Multi-game Updates Bot\n\n"
        "Commands:\n"
        "/games\n"
        "/settopic <game>  (run INSIDE the topic)\n"
        "/latest <game>\n"
        "/on <game>   /off <game>\n"
        "/status (admin)\n"
    )

async def games_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["🎮 Supported games:"]
    for k, g in GAMES.items():
        lines.append(f"- {k}  →  {g.name}")
    await update.message.reply_text("\n".join(lines))

async def settopic_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    if msg.is_topic_message is False or msg.message_thread_id is None:
        await msg.reply_text("Run /settopic inside a Topic (thread).")
        return

    if not context.args:
        await msg.reply_text("Usage: /settopic <game>\nExample: /settopic deltaforce")
        return

    game_key = context.args[0].lower().strip()
    if game_key not in GAMES:
        await msg.reply_text("Unknown game key. Use /games to see options.")
        return

    set_topic_binding(game_key, msg.chat_id, msg.message_thread_id, enabled=1)
    await msg.reply_text(f"✅ Bound this topic to: {GAMES[game_key].name}\n(Alerts ON)")

async def latest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    if not context.args:
        await msg.reply_text("Usage: /latest <game>\nExample: /latest codm")
        return

    game_key = context.args[0].lower().strip()
    if game_key not in GAMES:
        await msg.reply_text("Unknown game key. Use /games to see options.")
        return

    game = GAMES[game_key]
    try:
        latest = await fetch_latest(game)
        if not latest:
            await msg.reply_text("Couldn't find a latest post on that source (site layout may have changed).")
            return
        title, url = latest
        await msg.reply_text(f"🆕 {game.name}\n{title}\n{url}")
    except Exception as e:
        await msg.reply_text(f"Error fetching latest: {e}")

async def on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await toggle_cmd(update, context, enabled=1)

async def off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await toggle_cmd(update, context, enabled=0)

async def toggle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, enabled: int):
    msg = update.message
    if not msg:
        return

    if msg.is_topic_message is False or msg.message_thread_id is None:
        await msg.reply_text("Run this inside the Topic you want to enable/disable.")
        return

    if not context.args:
        await msg.reply_text("Usage: /on <game>   or   /off <game>")
        return

    game_key = context.args[0].lower().strip()
    if game_key not in GAMES:
        await msg.reply_text("Unknown game key. Use /games to see options.")
        return

    set_topic_binding(game_key, msg.chat_id, msg.message_thread_id, enabled=enabled)
    await msg.reply_text(f"✅ {GAMES[game_key].name} alerts are now {'ON' if enabled else 'OFF'} for this topic.")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    if not is_admin(msg.from_user.id):
        await msg.reply_text("Admin only.")
        return

    lines = ["📌 Current topic bindings:"]
    with db() as conn:
        rows = conn.execute("SELECT game_key, chat_id, thread_id, enabled FROM topic_map ORDER BY game_key").fetchall()
    if not rows:
        lines.append("(none) — run /settopic inside each topic.")
    else:
        for game_key, chat_id, thread_id, enabled in rows:
            name = GAMES.get(game_key, GameSource(game_key, game_key, "", "", re.compile("."))).name
            lines.append(f"- {game_key} ({name}) → chat {chat_id}, topic {thread_id}, {'ON' if enabled else 'OFF'}")
    await msg.reply_text("\n".join(lines))


# ----------------------------
# Background checker (JobQueue)
# ----------------------------
async def check_all_job(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    for game_key, game in GAMES.items():
        try:
            latest = await fetch_latest(game)
            if not latest:
                continue
            title, url = latest
            last_url = get_last_seen(game_key)

            # If first time ever, just store and do NOT spam old stuff
            if not last_url:
                update_last_seen(game_key, title, url)
                continue

            if url != last_url:
                update_last_seen(game_key, title, url)
                await post_update(app, game, title, url)

        except Exception as e:
            print(f"[WARN] check failed for {game_key}: {e}")


def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("games", games_cmd))
    app.add_handler(CommandHandler("settopic", settopic_cmd))
    app.add_handler(CommandHandler("latest", latest_cmd))
    app.add_handler(CommandHandler("on", on_cmd))
    app.add_handler(CommandHandler("off", off_cmd))
    app.add_handler(CommandHandler("status", status_cmd))

    # Run checker every N minutes
    app.job_queue.run_repeating(check_all_job, interval=CHECK_EVERY_MIN * 60, first=10)

    print("Bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
