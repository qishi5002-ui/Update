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
        link_regex=re.compile(r"/news/"),
    ),
}


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

def get_bound_topics(game_key: str) -> List[Tuple[int, int, int]]:
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
# HTTP + Scraping
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

def extract_latest_article_from_list(html: str, source: GameSource) -> Optional[Tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    candidates: List[Tuple[str, str]] = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        if not source.link_regex.search(href):
            continue

        url = absolutize(source.base, href)
        title = (a.get_text(" ", strip=True) or a.get("title") or a.get("aria-label") or "").strip()
        title = re.sub(r"\s+", " ", title)

        if len(title) < 6:
            continue
        candidates.append((title, url))

    seen = set()
    uniq = []
    for t, u in candidates:
        if u in seen:
            continue
        seen.add(u)
        uniq.append((t, u))

    return uniq[0] if uniq else None

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

async def fetch_latest(source: GameSource) -> Optional[Tuple[str, str, Optional[str], str]]:
    list_resp = await http_get(source.url)
    latest = extract_latest_article_from_list(list_resp.text, source)
    if not latest:
        return None

    fallback_title, article_url = latest

    try:
        art_resp = await http_get(article_url)
        title, url, img, desc = parse_article_meta(art_resp.text, fallback_title, article_url)
        return title, url, img, desc
    except Exception:
        return fallback_title, article_url, None, ""


async def send_update_media(bot, chat_id: int, thread_id: Optional[int], game: GameSource,
                            title: str, url: str, img: Optional[str], desc: str):
    caption_lines = [
        f"🆕 <b>{escape_html(game.name)}</b>",
        f"<b>{escape_html(title)}</b>",
    ]
    if desc:
        caption_lines.append(escape_html(desc))
    caption_lines.append(escape_html(url))
    caption = "\n".join(caption_lines)

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
# UI (Buttons)
# ----------------------------
def main_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Setup Topic", callback_data="panel:setup")],
        [InlineKeyboardButton("🆕 Latest Updates", callback_data="panel:latest")],
        [InlineKeyboardButton("🔔 Alerts ON / OFF", callback_data="panel:alerts")],
        [InlineKeyboardButton("📋 Games List", callback_data="panel:games")],
    ])

def games_keyboard(prefix: str) -> InlineKeyboardMarkup:
    rows = []
    keys = list(GAMES.keys())
    # 2 per row
    for i in range(0, len(keys), 2):
        row = []
        for k in keys[i:i+2]:
            row.append(InlineKeyboardButton(GAMES[k].name, callback_data=f"{prefix}:{k}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="panel:back")])
    return InlineKeyboardMarkup(rows)


# ----------------------------
# Commands (only /start to open panel)
# ----------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    await msg.reply_text(
        "✅ Multi-Game Updates Control Panel\n\n"
        "Use the buttons below.\n"
        "Tip: Run /start inside a Topic to control that topic.",
        reply_markup=main_panel()
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()

    data = q.data or ""
    msg = q.message

    # Determine current topic context
    chat_id = msg.chat_id if msg else None
    thread_id = msg.message_thread_id if msg and msg.is_topic_message else None

    # Panel navigation
    if data == "panel:back":
        await q.edit_message_reply_markup(reply_markup=main_panel())
        return

    if data == "panel:games":
        text = "🎮 Games supported:\n" + "\n".join([f"- {g.name} (<code>{k}</code>)" for k, g in GAMES.items()])
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=main_panel())
        return

    if data == "panel:setup":
        if thread_id is None:
            await q.edit_message_text(
                "⚠️ Please run /start inside the Topic you want to bind.\n"
                "Then press Setup Topic again.",
                reply_markup=main_panel()
            )
            return
        await q.edit_message_text("Select which game this Topic is for:", reply_markup=games_keyboard("setup"))
        return

    if data == "panel:latest":
        await q.edit_message_text("Pick a game to fetch its latest update:", reply_markup=games_keyboard("latest"))
        return

    if data == "panel:alerts":
        if thread_id is None:
            await q.edit_message_text(
                "⚠️ Please run /start inside the Topic you want to control.\n"
                "Then press Alerts again.",
                reply_markup=main_panel()
            )
            return
        await q.edit_message_text("Pick a game to toggle alerts for THIS topic:", reply_markup=games_keyboard("toggle"))
        return

    # Setup binding
    if data.startswith("setup:"):
        if thread_id is None:
            await q.edit_message_text("⚠️ Run /start inside a Topic first.", reply_markup=main_panel())
            return
        game_key = data.split(":", 1)[1]
        if game_key not in GAMES:
            await q.edit_message_text("Unknown game.", reply_markup=main_panel())
            return
        set_topic_binding(game_key, chat_id, thread_id, enabled=1)
        await q.edit_message_text(
            f"✅ This Topic is now bound to:\n<b>{escape_html(GAMES[game_key].name)}</b>\n\nAlerts: ON",
            parse_mode=ParseMode.HTML,
            reply_markup=main_panel()
        )
        return

    # Latest on demand (posts media+words into same topic if you launched panel there)
    if data.startswith("latest:"):
        game_key = data.split(":", 1)[1]
        if game_key not in GAMES:
            await q.edit_message_text("Unknown game.", reply_markup=main_panel())
            return

        game = GAMES[game_key]

        # Show loading
        await q.edit_message_text(f"⏳ Fetching latest for {game.name}...", reply_markup=main_panel())

        try:
            latest = await fetch_latest(game)
            if not latest:
                await q.edit_message_text("Couldn't find latest (site layout may have changed).", reply_markup=main_panel())
                return
            title, url, img, desc = latest

            # Send update into same chat/topic as panel message
            await send_update_media(context.bot, chat_id, thread_id, game, title, url, img, desc)

            # Restore panel
            await q.edit_message_text("✅ Sent the latest update.\nChoose another action:", reply_markup=main_panel())
        except Exception as e:
            await q.edit_message_text(f"Error fetching latest: {type(e).__name__}: {e}", reply_markup=main_panel())
        return

    # Toggle alerts for current topic
    if data.startswith("toggle:"):
        if thread_id is None:
            await q.edit_message_text("⚠️ Run /start inside a Topic first.", reply_markup=main_panel())
            return

        game_key = data.split(":", 1)[1]
        if game_key not in GAMES:
            await q.edit_message_text("Unknown game.", reply_markup=main_panel())
            return

        current = get_binding(game_key, chat_id, thread_id)
        new_enabled = 0 if current == 1 else 1
        set_topic_binding(game_key, chat_id, thread_id, enabled=new_enabled)

        await q.edit_message_text(
            f"🔔 Alerts for <b>{escape_html(GAMES[game_key].name)}</b> in THIS topic: "
            f"<b>{'ON' if new_enabled else 'OFF'}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_panel()
        )
        return


# ----------------------------
# Background Job
# ----------------------------
async def check_all_job(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    print("[JOB] check cycle", time.strftime("%Y-%m-%d %H:%M:%S"))

    for game_key, game in GAMES.items():
        try:
            latest = await fetch_latest(game)
            if not latest:
                continue
            title, url, img, desc = latest

            last_url = get_last_seen(game_key)

            # baseline store only
            if not last_url:
                update_last_seen(game_key, title, url)
                continue

            if url != last_url:
                update_last_seen(game_key, title, url)
                for chat_id, thread_id, enabled in get_bound_topics(game_key):
                    if not enabled:
                        continue
                    await send_update_media(app.bot, chat_id, thread_id, game, title, url, img, desc)

        except Exception as e:
            print(f"[WARN] {game_key} check failed:", repr(e))


# ----------------------------
# Main
# ----------------------------
def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # Only /start + buttons
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))

    app.job_queue.run_repeating(check_all_job, interval=CHECK_EVERY_MIN * 60, first=10)

    print("Bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
