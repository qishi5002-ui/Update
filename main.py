import os, re, time, sqlite3, asyncio
from dataclasses import dataclass
from typing import Optional, Dict, Tuple, List
from xml.etree import ElementTree as ET

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHECK_EVERY_MIN = int(os.getenv("CHECK_EVERY_MIN", "10"))
DB_FILE = "updates_bot.db"

FOOTER = "\n\nBot created by @RekkoOwn"

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN")

PATCH_KEYWORDS = [
    "patch", "patch notes", "update", "version", "hotfix", "bug fix", "bugfix",
    "maintenance", "optimization", "optimisation", "season", "balance", "release notes",
    "client update", "major update", "new version"
]

URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)

def strip_links(text: str) -> str:
    if not text:
        return ""
    text = URL_RE.sub("", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text

def has_patch_kw(s: str) -> bool:
    t = (s or "").lower()
    return any(k in t for k in PATCH_KEYWORDS)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

async def http_get(url: str) -> httpx.Response:
    last = None
    for _ in range(3):
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=HEADERS) as client:
                r = await client.get(url)
                r.raise_for_status()
                return r
        except Exception as e:
            last = e
            await asyncio.sleep(2)
    raise last

# ---------------- DB ----------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS bindings (
            chat_id INTEGER NOT NULL,
            thread_id INTEGER NOT NULL,
            game_key TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(chat_id, thread_id)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS last_seen (
            game_key TEXT PRIMARY KEY,
            last_id TEXT,
            last_title TEXT,
            last_ts INTEGER
        )
        """)

def ctx_ids(update: Update) -> Tuple[int, int]:
    chat_id = update.effective_chat.id
    msg = update.effective_message
    if msg and getattr(msg, "is_topic_message", False) and msg.message_thread_id:
        return chat_id, int(msg.message_thread_id)
    return chat_id, 0

def set_binding(chat_id: int, thread_id: int, game_key: str, enabled: int = 1):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO bindings(chat_id, thread_id, game_key, enabled) VALUES (?,?,?,?)",
            (chat_id, thread_id, game_key, enabled)
        )

def get_binding(chat_id: int, thread_id: int) -> Optional[Tuple[str, int]]:
    with db() as conn:
        row = conn.execute(
            "SELECT game_key, enabled FROM bindings WHERE chat_id=? AND thread_id=?",
            (chat_id, thread_id)
        ).fetchone()
    if not row:
        return None
    return str(row[0]), int(row[1])

def toggle_enabled(chat_id: int, thread_id: int) -> Optional[int]:
    cur = get_binding(chat_id, thread_id)
    if not cur:
        return None
    g, enabled = cur
    new_enabled = 0 if enabled == 1 else 1
    set_binding(chat_id, thread_id, g, new_enabled)
    return new_enabled

def all_bindings() -> List[Tuple[int, int, str, int]]:
    with db() as conn:
        rows = conn.execute("SELECT chat_id, thread_id, game_key, enabled FROM bindings").fetchall()
    return [(int(r[0]), int(r[1]), str(r[2]), int(r[3])) for r in rows]

def get_last_seen(game_key: str) -> Optional[str]:
    with db() as conn:
        row = conn.execute("SELECT last_id FROM last_seen WHERE game_key=?", (game_key,)).fetchone()
    return row[0] if row else None

def set_last_seen(game_key: str, item_id: str, title: str):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO last_seen(game_key, last_id, last_title, last_ts) VALUES (?,?,?,?)",
            (game_key, item_id, title, int(time.time()))
        )

# ---------------- Admin check (NOT hardcoded to you) ----------------
async def is_chat_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return False
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False

# ---------------- Sources ----------------
@dataclass
class WebSource:
    url: str
    base: str
    link_regex: re.Pattern

@dataclass
class YTSource:
    channel_id: str  # YouTube channel id

SOURCES: Dict[str, object] = {
    # Official channel IDs (from YouTube channel pages)
    "mlbb": YTSource(channel_id="UCqmld-BIYME2i_ooRTo1EOg"),        # Mobile Legends: Bang Bang  [oai_citation:0‡YouTube](https://www.youtube.com/channel/UCqmld-BIYME2i_ooRTo1EOg?utm_source=chatgpt.com)
    "delta": YTSource(channel_id="UC82GZluB0OeupPB4FNUuYEg"),       # Garena Delta Force SG/MY/PH  [oai_citation:1‡YouTube](https://www.youtube.com/channel/UC82GZluB0OeupPB4FNUuYEg?utm_source=chatgpt.com)
    "arena": YTSource(channel_id="UCSrq2wtp-DB6blBl4dRuzbw"),       # Arena Breakout  [oai_citation:2‡YouTube](https://www.youtube.com/channel/UCSrq2wtp-DB6blBl4dRuzbw?utm_source=chatgpt.com)
    "freefire": YTSource(channel_id="UC7qTEluetD2pDB7lUBBlKuw"),    # Garena Free Fire Global  [oai_citation:3‡YouTube](https://www.youtube.com/channel/UC7qTEluetD2pDB7lUBBlKuw?utm_source=chatgpt.com)
    "bloodstrike": YTSource(channel_id="UCqAKRVWkOrBNFcBD6I21CKw"), # Blood Strike Official  [oai_citation:4‡YouTube](https://www.youtube.com/channel/UCqAKRVWkOrBNFcBD6I21CKw?utm_source=chatgpt.com)

    # If you want CODM later (blog scraping), we can add it back.
}

GAME_NAMES = {
    "mlbb": "Mobile Legends: Bang Bang",
    "delta": "Delta Force Mobile",
    "arena": "Arena Breakout Mobile",
    "freefire": "Free Fire",
    "bloodstrike": "BloodStrike",
}

VALID_GAMES = list(GAME_NAMES.keys())

def yt_feed_url(channel_id: str) -> str:
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

# ---------------- Fetch latest patch/update from YouTube RSS ----------------
def parse_youtube_rss(xml_text: str) -> List[Tuple[str, str, str]]:
    """
    Returns list of (video_id, title, description) newest-first
    """
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
        "media": "http://search.yahoo.com/mrss/",
    }
    root = ET.fromstring(xml_text)
    out = []
    for entry in root.findall("atom:entry", ns):
        vid = entry.findtext("yt:videoId", default="", namespaces=ns).strip()
        title = entry.findtext("atom:title", default="", namespaces=ns).strip()
        desc = entry.findtext("media:group/media:description", default="", namespaces=ns).strip()
        out.append((vid, title, desc))
    return out

async def fetch_latest_patch(game_key: str) -> Optional[Tuple[Optional[str], str, str, str]]:
    src = SOURCES[game_key]
    if isinstance(src, YTSource):
        r = await http_get(yt_feed_url(src.channel_id))
        entries = parse_youtube_rss(r.text)
        if not entries:
            return None

        # Prefer patch/update-like titles first
        patch_first = [e for e in entries if has_patch_kw(e[1])]
        vid, title, desc = patch_first[0] if patch_first else entries[0]

        # Thumbnail (media)
        thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else None

        title = strip_links(title)
        desc = strip_links(desc)

        # Make description “full” but safe for Telegram
        if len(desc) > 1200:
            desc = desc[:1200].rstrip() + "..."

        unique_id = vid or title
        return thumb, title, desc, unique_id

    return None

# ---------------- Send (NO LINKS) ----------------
async def send_patch(bot, chat_id: int, thread_id: int, game_key: str,
                     media_url: Optional[str], title: str, desc: str):
    thread = None if thread_id == 0 else thread_id
    game_name = GAME_NAMES[game_key]

    body = f"🛠️ <b>{game_name} — Update / Patch</b>\n<b>{strip_links(title)}</b>"
    if desc:
        body += f"\n\n{strip_links(desc)}"
    body += f"{FOOTER}"

    if media_url:
        try:
            caption = body
            if len(caption) > 1000:
                caption = caption[:1000].rstrip() + "..."
            await bot.send_photo(
                chat_id=chat_id,
                message_thread_id=thread,
                photo=media_url,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
            return
        except Exception:
            pass

    await bot.send_message(
        chat_id=chat_id,
        message_thread_id=thread,
        text=body if len(body) <= 4096 else (body[:4096].rstrip() + "..."),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

# ---------------- Admin-only UI ----------------
def admin_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Set Up Here (Topic/Channel)", callback_data="admin:setup")],
        [InlineKeyboardButton("🔔 Alerts ON / OFF (Here)", callback_data="admin:toggle")],
        [InlineKeyboardButton("🛠️ Send Latest Patch Now (Here)", callback_data="admin:sendnow")],
    ])

def games_kb(prefix: str) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(VALID_GAMES), 2):
        row = []
        for k in VALID_GAMES[i:i+2]:
            row.append(InlineKeyboardButton(GAME_NAMES[k], callback_data=f"{prefix}:{k}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="admin:back")])
    return InlineKeyboardMarkup(rows)

# ---------------- /news (ADMIN ONLY) ----------------
async def news_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not update.effective_message:
        return

    # delete command message if possible
    try:
        if update.message:
            await update.message.delete()
    except Exception:
        pass

    if not await is_chat_admin(update, context):
        # Don’t show anything to normal users
        return

    chat_id, thread_id = ctx_ids(update)
    await context.bot.send_message(
        chat_id=chat_id,
        message_thread_id=None if thread_id == 0 else thread_id,
        text="🔧 <b>Admin Panel</b>\n(Admins only)" + FOOTER,
        parse_mode=ParseMode.HTML,
        reply_markup=admin_panel_kb()
    )

# ---------------- Admin callbacks ----------------
async def cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.message:
        return

    # Only admins of that chat can use buttons
    # (Use the callback user + message chat to check.)
    try:
        member = await context.bot.get_chat_member(q.message.chat_id, q.from_user.id)
        if member.status not in ("administrator", "creator"):
            await q.answer("Admins only.", show_alert=True)
            return
    except Exception:
        await q.answer("Admins only.", show_alert=True)
        return

    await q.answer()
    data = q.data or ""
    msg = q.message

    chat_id = msg.chat_id
    thread_id = msg.message_thread_id if getattr(msg, "is_topic_message", False) else 0

    if data == "admin:back":
        await q.edit_message_reply_markup(reply_markup=admin_panel_kb())
        return

    if data == "admin:setup":
        await q.edit_message_text(
            "Select which game this Topic/Channel is for:" + FOOTER,
            parse_mode=ParseMode.HTML,
            reply_markup=games_kb("bind")
        )
        return

    if data == "admin:toggle":
        b = get_binding(chat_id, thread_id)
        if not b:
            await q.edit_message_text("❌ Not configured yet. Use Set Up first." + FOOTER,
                                      parse_mode=ParseMode.HTML, reply_markup=admin_panel_kb())
            return
        new_enabled = toggle_enabled(chat_id, thread_id)
        await q.edit_message_text(
            f"🔔 Alerts now: <b>{'ON' if new_enabled == 1 else 'OFF'}</b>" + FOOTER,
            parse_mode=ParseMode.HTML,
            reply_markup=admin_panel_kb()
        )
        return

    if data == "admin:sendnow":
        b = get_binding(chat_id, thread_id)
        if not b:
            await q.edit_message_text("❌ Not configured yet. Use Set Up first." + FOOTER,
                                      parse_mode=ParseMode.HTML, reply_markup=admin_panel_kb())
            return
        game_key, enabled = b
        if enabled != 1:
            await q.edit_message_text("🔕 Alerts are OFF here. Toggle ON first." + FOOTER,
                                      parse_mode=ParseMode.HTML, reply_markup=admin_panel_kb())
            return

        await q.edit_message_text("⏳ Fetching latest update/patch..." + FOOTER, parse_mode=ParseMode.HTML)
        try:
            result = await fetch_latest_patch(game_key)
            if not result:
                await q.edit_message_text("❌ Could not fetch update/patch right now." + FOOTER,
                                          parse_mode=ParseMode.HTML, reply_markup=admin_panel_kb())
                return
            media, title, desc, _uid = result
            await send_patch(context.bot, chat_id, thread_id, game_key, media, title, desc)
            await q.edit_message_text("✅ Sent." + FOOTER, parse_mode=ParseMode.HTML, reply_markup=admin_panel_kb())
        except Exception as e:
            await q.edit_message_text(f"❌ Error: {type(e).__name__}: {strip_links(str(e))}" + FOOTER,
                                      parse_mode=ParseMode.HTML, reply_markup=admin_panel_kb())
        return

    if data.startswith("bind:"):
        game_key = data.split(":", 1)[1]
        if game_key not in VALID_GAMES:
            await q.edit_message_text("Unknown game." + FOOTER, parse_mode=ParseMode.HTML, reply_markup=admin_panel_kb())
            return
        set_binding(chat_id, thread_id, game_key, enabled=1)
        await q.edit_message_text(
            f"✅ Bound here to:\n<b>{GAME_NAMES[game_key]}</b>\nAlerts: <b>ON</b>" + FOOTER,
            parse_mode=ParseMode.HTML,
            reply_markup=admin_panel_kb()
        )
        return

# ---------------- Auto checker ----------------
async def check_job(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    binds = all_bindings()
    if not binds:
        return

    used_games = sorted(set(g for _, _, g, en in binds if en == 1 and g in VALID_GAMES))
    for game_key in used_games:
        try:
            result = await fetch_latest_patch(game_key)
            if not result:
                continue
            media, title, desc, uid = result

            last = get_last_seen(game_key)
            if not last:
                set_last_seen(game_key, uid, title)
                continue

            if uid != last:
                set_last_seen(game_key, uid, title)
                for chat_id, thread_id, g, enabled in binds:
                    if enabled != 1 or g != game_key:
                        continue
                    await send_patch(app.bot, chat_id, thread_id, game_key, media, title, desc)

        except Exception as e:
            print(f"[WARN] {game_key} check failed:", repr(e))

def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler(["news", "News"], news_cmd))
    app.add_handler(CallbackQueryHandler(cb))

    app.job_queue.run_repeating(check_job, interval=CHECK_EVERY_MIN * 60, first=10)

    print("Bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
