import os, re, time, sqlite3, asyncio
from dataclasses import dataclass
from typing import Optional, Dict, Tuple, List

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

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

# Patch/update keywords
PATCH_KEYWORDS = [
    "patch", "patch notes", "update", "version", "hotfix", "bug fix", "bugfix",
    "maintenance", "optimization", "optimisation", "season", "balance", "system upgrades",
    "release notes", "client update", "major update"
]

# Strip ALL links from output
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)


def strip_links(text: str) -> str:
    if not text:
        return ""
    text = URL_RE.sub("", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


def has_patch_kw(title: str) -> bool:
    t = (title or "").lower()
    return any(k in t for k in PATCH_KEYWORDS)


# ---------- DB ----------
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


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def ctx_ids(update: Update) -> Tuple[int, int]:
    chat_id = update.effective_chat.id
    msg = update.effective_message
    if msg and getattr(msg, "is_topic_message", False) and msg.message_thread_id:
        return chat_id, int(msg.message_thread_id)
    return chat_id, 0  # 0 means no topic thread (channels/normal groups)


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


# ---------- HTTP ----------
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


# ---------- Sources ----------
@dataclass
class Source:
    type: str  # "youtube" or "web"
    feed_or_url: str
    base: str = ""  # for web
    link_regex: Optional[re.Pattern] = None


# Official socials approach:
# - Most games: Official YouTube RSS (thumbnail + description) -> NO links in output
# - CODM: official blog (better patch notes) but we still send NO links

SOURCES: Dict[str, Source] = {
    # YouTube RSS format: https://www.youtube.com/feeds/videos.xml?channel_id=CHANNEL_ID
    "deltaforce": Source(type="youtube", feed_or_url="https://www.youtube.com/feeds/videos.xml?channel_id=UC82GZluB0OeupPB4FNUuYEg"),
    "arenabreakout": Source(type="youtube", feed_or_url="https://www.youtube.com/feeds/videos.xml?channel_id=UCSrq2wtp-DB6blBl4dRuzbw"),
    "mlbb": Source(type="youtube", feed_or_url="https://www.youtube.com/feeds/videos.xml?channel_id=UCqmld-BIYME2i_ooRTo1EOg"),
    "bloodstrike": Source(type="youtube", feed_or_url="https://www.youtube.com/feeds/videos.xml?channel_id=UC5ORZ2u9JoiGe9bp-cjxPIg"),

    # 8 Ball Pool: official site is mostly news/events; still best available without APIs
    "8ball": Source(
        type="web",
        feed_or_url="https://www.8ballpool.com/en/news",
        base="https://www.8ballpool.com",
        link_regex=re.compile(r"^/en/news/")
    ),

    # Free Fire: official site posts patch notes regularly
    "freefire": Source(
        type="web",
        feed_or_url="https://ff.garena.com/en/news/",
        base="https://ff.garena.com",
        link_regex=re.compile(r"/en/|/article/|/news/")
    ),

    # CODM: official mobile blog page (fix weird /content/... internally; we don't send links anyway)
    "codm": Source(
        type="web",
        feed_or_url="https://www.callofduty.com/blog/mobile",
        base="https://www.callofduty.com",
        link_regex=re.compile(r"/blog/")
    ),
}

GAME_NAMES = {
    "deltaforce": "Delta Force Mobile",
    "8ball": "8 Ball Pool Mobile",
    "arenabreakout": "Arena Breakout Mobile",
    "mlbb": "Mobile Legends: Bang Bang",
    "freefire": "Free Fire Mobile",
    "codm": "Call of Duty Mobile",
    "bloodstrike": "BloodStrike Mobile",
}

VALID_GAMES = list(GAME_NAMES.keys())


# ---------- Fetch latest patch/update (returns media_url, title, description, unique_id) ----------
async def fetch_latest_patch(game_key: str) -> Optional[Tuple[Optional[str], str, str, str]]:
    src = SOURCES[game_key]

    if src.type == "youtube":
        r = await http_get(src.feed_or_url)
        xml = r.text

        # Parse feed with BeautifulSoup (XML mode)
        soup = BeautifulSoup(xml, "xml")
        entries = soup.find_all("entry")
        if not entries:
            return None

        # Prefer patch/update-like entries first
        def entry_title(e) -> str:
            t = e.find("title")
            return t.get_text(strip=True) if t else ""

        patch_entries = [e for e in entries if has_patch_kw(entry_title(e))]
        chosen = patch_entries[0] if patch_entries else entries[0]

        title = entry_title(chosen)

        # Description: YouTube provides media:group/description
        desc_tag = chosen.find("media:description") or chosen.find("content")
        desc = desc_tag.get_text(" ", strip=True) if desc_tag else ""
        desc = strip_links(desc)

        # Thumbnail
        thumb = chosen.find("media:thumbnail")
        media_url = thumb["url"].strip() if thumb and thumb.get("url") else None

        # Unique id
        vid_id = chosen.find("yt:videoId")
        unique = (vid_id.get_text(strip=True) if vid_id else title) or title

        return media_url, title, desc, unique

    # web scraping
    list_r = await http_get(src.feed_or_url)
    soup = BeautifulSoup(list_r.text, "html.parser")

    candidates = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        if src.link_regex and not src.link_regex.search(href):
            continue
        title = (a.get_text(" ", strip=True) or a.get("title") or a.get("aria-label") or "").strip()
        title = re.sub(r"\s+", " ", title)
        if len(title) < 6:
            continue

        url = href
        if href.startswith("/"):
            url = src.base.rstrip("/") + href

        candidates.append((title, url))

    # de-dup
    seen = set()
    uniq = []
    for t, u in candidates:
        if u in seen:
            continue
        seen.add(u)
        uniq.append((t, u))

    if not uniq:
        return None

    # prefer patch/update candidates first
    patch_first = [c for c in uniq if has_patch_kw(c[0])]
    title, article_url = patch_first[0] if patch_first else uniq[0]

    # fetch article
    try:
        art_r = await http_get(article_url)
        art_soup = BeautifulSoup(art_r.text, "html.parser")

        # canonical (fix CODM internal weird URLs)
        canonical = None
        canon_tag = art_soup.find("link", rel=lambda x: x and "canonical" in x)
        if canon_tag and canon_tag.get("href"):
            canonical = canon_tag["href"].strip()

        # OG image
        og_img = None
        og = art_soup.find("meta", attrs={"property": "og:image"})
        if og and og.get("content"):
            og_img = og["content"].strip()

        # Full-ish description: collect paragraphs
        # Try <article> first; else fallback to all <p>
        text_parts = []
        article_tag = art_soup.find("article")
        p_tags = (article_tag.find_all("p") if article_tag else art_soup.find_all("p"))
        for p in p_tags:
            txt = p.get_text(" ", strip=True)
            txt = strip_links(txt)
            if txt and len(txt) > 20:
                text_parts.append(txt)

        # de-dup lines
        seen_lines = set()
        cleaned = []
        for line in text_parts:
            key = line.lower()
            if key in seen_lines:
                continue
            seen_lines.add(key)
            cleaned.append(line)

        desc = "\n\n".join(cleaned).strip()
        if not desc:
            # fallback: meta description
            md = art_soup.find("meta", attrs={"name": "description"})
            if md and md.get("content"):
                desc = strip_links(md["content"].strip())

        # Telegram caption limit ~1024 for photo captions; keep safe
        if len(desc) > 900:
            desc = desc[:900].rstrip() + "..."

        unique_id = canonical or str(art_r.url) or article_url
        return og_img, title, desc, unique_id

    except Exception:
        # fallback with no description
        return None, title, "", article_url


# ---------- Send (NO LINKS) ----------
async def send_patch(bot, chat_id: int, thread_id: int, game_key: str,
                     media_url: Optional[str], title: str, desc: str):
    thread = None if thread_id == 0 else thread_id
    game_name = GAME_NAMES[game_key]

    # Make message no-links
    title_clean = strip_links(title)
    desc_clean = strip_links(desc)

    # Build text
    body = f"🛠️ <b>{game_name} — Update / Patch</b>\n<b>{title_clean}</b>"
    if desc_clean:
        body += f"\n\n{desc_clean}"
    body += FOOTER

    # Use media if possible (thumbnail/og:image)
    if media_url:
        try:
            # Caption length is limited, keep it safe
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


# ---------- Admin-only setup UI ----------
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


# ---------- /news ----------
async def news_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not update.effective_message:
        return

    chat_id, thread_id = ctx_ids(update)
    user_id = update.effective_user.id if update.effective_user else 0

    # delete user command if possible
    try:
        if update.message:
            await update.message.delete()
    except Exception:
        pass

    # Admin sees setup panel (users never see it)
    if is_admin(user_id):
        await context.bot.send_message(
            chat_id=chat_id,
            message_thread_id=None if thread_id == 0 else thread_id,
            text="🔧 <b>Admin Panel</b>\n(Admin-only)" + FOOTER,
            parse_mode=ParseMode.HTML,
            reply_markup=admin_panel_kb(),
        )
        return

    # User: send latest patch for bound game only (no buttons)
    bind = get_binding(chat_id, thread_id)
    if not bind:
        await context.bot.send_message(
            chat_id=chat_id,
            message_thread_id=None if thread_id == 0 else thread_id,
            text="❌ This topic/channel is not configured yet." + FOOTER,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    game_key, enabled = bind
    if enabled != 1:
        await context.bot.send_message(
            chat_id=chat_id,
            message_thread_id=None if thread_id == 0 else thread_id,
            text="🔕 Updates are OFF for this topic/channel." + FOOTER,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    try:
        result = await fetch_latest_patch(game_key)
        if not result:
            await context.bot.send_message(
                chat_id=chat_id,
                message_thread_id=None if thread_id == 0 else thread_id,
                text="❌ Can't fetch updates right now. Try again later." + FOOTER,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return
        media, title, desc, _uid = result
        await send_patch(context.bot, chat_id, thread_id, game_key, media, title, desc)
    except Exception as e:
        await context.bot.send_message(
            chat_id=chat_id,
            message_thread_id=None if thread_id == 0 else thread_id,
            text=f"❌ Error: {type(e).__name__}: {strip_links(str(e))}" + FOOTER,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


# ---------- Admin callbacks ----------
async def cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.message:
        return

    user_id = q.from_user.id if q.from_user else 0
    if not is_admin(user_id):
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
            "Select game to bind here (Topic/Channel):" + FOOTER,
            parse_mode=ParseMode.HTML,
            reply_markup=games_kb("bind"),
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
            reply_markup=admin_panel_kb(),
        )
        return

    if data == "admin:sendnow":
        b = get_binding(chat_id, thread_id)
        if not b:
            await q.edit_message_text("❌ Not configured yet. Use Set Up first." + FOOTER,
                                      parse_mode=ParseMode.HTML, reply_markup=admin_panel_kb())
            return
        game_key, _enabled = b
        await q.edit_message_text("⏳ Fetching latest patch..." + FOOTER, parse_mode=ParseMode.HTML)
        try:
            result = await fetch_latest_patch(game_key)
            if not result:
                await q.edit_message_text("❌ Could not find update/patch right now." + FOOTER,
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
            reply_markup=admin_panel_kb(),
        )
        return


# ---------- Auto checker ----------
async def check_job(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    binds = all_bindings()
    if not binds:
        return

    # Only check games that are actually bound somewhere
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
                # send to every place bound to this game
                for chat_id, thread_id, g, enabled in binds:
                    if enabled != 1 or g != game_key:
                        continue
                    await send_patch(app.bot, chat_id, thread_id, game_key, media, title, desc)

        except Exception as e:
            print(f"[WARN] {game_key} check failed:", repr(e))


def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # Users: /news
    app.add_handler(CommandHandler(["news", "News"], news_cmd))
    app.add_handler(CallbackQueryHandler(cb))

    # Auto checking
    app.job_queue.run_repeating(check_job, interval=CHECK_EVERY_MIN * 60, first=10)

    print("Bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
