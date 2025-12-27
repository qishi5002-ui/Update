import os
import re
import time
import json
import base64
import sqlite3
import asyncio
from typing import Optional, Dict, Any, List, Tuple

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

load_dotenv()

MAIN_BOT_TOKEN = os.getenv("MAIN_BOT_TOKEN", "").strip()
DB_FILE = os.getenv("DB_FILE", "groupfeed_platform.db").strip()
INTRO_TEXT = os.getenv(
    "INTRO_TEXT",
    "You can contact us using this bot.\n\nBot created by @GroupFeedBot"
).strip()

# This is NOT strong encryption. It's just to avoid plain-text tokens in DB dumps/logs.
SECRET_KEY = os.getenv("SECRET_KEY", "").strip()

if not MAIN_BOT_TOKEN:
    raise RuntimeError("Missing MAIN_BOT_TOKEN")

TOKEN_RE = re.compile(r"^\d{5,20}:[A-Za-z0-9_-]{20,}$")


# ------------------ Tiny obfuscation helpers ------------------
def _xor_bytes(data: bytes, key: bytes) -> bytes:
    if not key:
        return data
    out = bytearray(len(data))
    for i, b in enumerate(data):
        out[i] = b ^ key[i % len(key)]
    return bytes(out)

def protect_token(token: str) -> str:
    raw = token.encode("utf-8")
    key = SECRET_KEY.encode("utf-8") if SECRET_KEY else b""
    x = _xor_bytes(raw, key)
    return base64.urlsafe_b64encode(x).decode("utf-8")

def unprotect_token(enc: str) -> str:
    raw = base64.urlsafe_b64decode(enc.encode("utf-8"))
    key = SECRET_KEY.encode("utf-8") if SECRET_KEY else b""
    x = _xor_bytes(raw, key)
    return x.decode("utf-8")


# ------------------ DB ------------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS hosted_bots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            bot_username TEXT NOT NULL,
            bot_id INTEGER NOT NULL,
            token_enc TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_ts INTEGER NOT NULL,
            UNIQUE(owner_id, bot_id)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS hosted_destinations (
            bot_id INTEGER NOT NULL,
            target_chat_id INTEGER NOT NULL,
            target_thread_id INTEGER NOT NULL, -- 0 no topic
            created_ts INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            UNIQUE(bot_id, target_chat_id, target_thread_id)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS hosted_user_intro (
            bot_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            shown INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(bot_id, user_id)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS hosted_submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id INTEGER NOT NULL,
            owner_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,     -- text|photo|video
            file_id TEXT,
            text TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_ts INTEGER NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS hosted_admin_map (
            bot_id INTEGER NOT NULL,
            owner_id INTEGER NOT NULL,
            admin_msg_id INTEGER NOT NULL,
            submission_id INTEGER NOT NULL,
            PRIMARY KEY(bot_id, owner_id, admin_msg_id)
        )
        """)

def add_hosted_bot(owner_id: int, bot_id: int, bot_username: str, token: str):
    with db() as conn:
        conn.execute("""
        INSERT OR REPLACE INTO hosted_bots(owner_id, bot_username, bot_id, token_enc, active, created_ts)
        VALUES (?,?,?,?,1,?)
        """, (owner_id, bot_username, bot_id, protect_token(token), int(time.time())))

def set_hosted_bot_active(owner_id: int, bot_id: int, active: int):
    with db() as conn:
        conn.execute("""
        UPDATE hosted_bots SET active=? WHERE owner_id=? AND bot_id=?
        """, (int(active), owner_id, bot_id))

def remove_hosted_bot(owner_id: int, bot_id: int):
    with db() as conn:
        conn.execute("DELETE FROM hosted_bots WHERE owner_id=? AND bot_id=?", (owner_id, bot_id))
        conn.execute("DELETE FROM hosted_destinations WHERE bot_id=?", (bot_id,))
        conn.execute("DELETE FROM hosted_user_intro WHERE bot_id=?", (bot_id,))
        conn.execute("DELETE FROM hosted_submissions WHERE bot_id=?", (bot_id,))
        conn.execute("DELETE FROM hosted_admin_map WHERE bot_id=?", (bot_id,))

def list_owner_bots(owner_id: int) -> List[Dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("""
        SELECT bot_id, bot_username, token_enc, active FROM hosted_bots
        WHERE owner_id=? ORDER BY created_ts DESC
        """, (owner_id,)).fetchall()
    out = []
    for r in rows:
        out.append({
            "bot_id": int(r[0]),
            "bot_username": str(r[1]),
            "token_enc": str(r[2]),
            "active": int(r[3]) == 1,
        })
    return out

def list_all_active_bots() -> List[Dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("""
        SELECT owner_id, bot_id, bot_username, token_enc FROM hosted_bots
        WHERE active=1
        """).fetchall()
    out = []
    for r in rows:
        out.append({
            "owner_id": int(r[0]),
            "bot_id": int(r[1]),
            "bot_username": str(r[2]),
            "token": unprotect_token(str(r[3])),
        })
    return out

def upsert_destination(bot_id: int, target_chat_id: int, target_thread_id: int):
    with db() as conn:
        conn.execute("""
        INSERT OR IGNORE INTO hosted_destinations(bot_id, target_chat_id, target_thread_id, created_ts, active)
        VALUES (?,?,?,?,1)
        """, (bot_id, target_chat_id, target_thread_id, int(time.time())))
        conn.execute("""
        UPDATE hosted_destinations SET active=1 WHERE bot_id=? AND target_chat_id=? AND target_thread_id=?
        """, (bot_id, target_chat_id, target_thread_id))

def list_destinations(bot_id: int) -> List[Tuple[int, int]]:
    with db() as conn:
        rows = conn.execute("""
        SELECT target_chat_id, target_thread_id FROM hosted_destinations
        WHERE bot_id=? AND active=1
        ORDER BY created_ts ASC
        """, (bot_id,)).fetchall()
    return [(int(r[0]), int(r[1])) for r in rows]

def disable_destination(bot_id: int, target_chat_id: int, target_thread_id: int):
    with db() as conn:
        conn.execute("""
        UPDATE hosted_destinations SET active=0 WHERE bot_id=? AND target_chat_id=? AND target_thread_id=?
        """, (bot_id, target_chat_id, target_thread_id))

def intro_shown(bot_id: int, user_id: int) -> bool:
    with db() as conn:
        row = conn.execute("""
        SELECT 1 FROM hosted_user_intro WHERE bot_id=? AND user_id=?
        """, (bot_id, user_id)).fetchone()
    return bool(row)

def mark_intro_shown(bot_id: int, user_id: int):
    with db() as conn:
        conn.execute("""
        INSERT OR REPLACE INTO hosted_user_intro(bot_id, user_id, shown) VALUES (?,?,1)
        """, (bot_id, user_id))

def create_submission(bot_id: int, owner_id: int, user_id: int, kind: str, file_id: Optional[str], text: str) -> int:
    with db() as conn:
        cur = conn.execute("""
        INSERT INTO hosted_submissions(bot_id, owner_id, user_id, kind, file_id, text, status, created_ts)
        VALUES (?,?,?,?,?,?,'pending',?)
        """, (bot_id, owner_id, user_id, kind, file_id, text or "", int(time.time())))
        return int(cur.lastrowid)

def get_submission(sub_id: int) -> Optional[Dict[str, Any]]:
    with db() as conn:
        row = conn.execute("""
        SELECT id, bot_id, owner_id, user_id, kind, file_id, text, status
        FROM hosted_submissions WHERE id=?
        """, (sub_id,)).fetchone()
    if not row:
        return None
    return {
        "id": int(row[0]),
        "bot_id": int(row[1]),
        "owner_id": int(row[2]),
        "user_id": int(row[3]),
        "kind": str(row[4]),
        "file_id": row[5],
        "text": row[6] or "",
        "status": str(row[7]),
    }

def set_submission_status(sub_id: int, status: str):
    with db() as conn:
        conn.execute("UPDATE hosted_submissions SET status=? WHERE id=?", (status, sub_id))

def map_admin_msg(bot_id: int, owner_id: int, admin_msg_id: int, submission_id: int):
    with db() as conn:
        conn.execute("""
        INSERT OR REPLACE INTO hosted_admin_map(bot_id, owner_id, admin_msg_id, submission_id)
        VALUES (?,?,?,?)
        """, (bot_id, owner_id, admin_msg_id, submission_id))

def submission_from_admin_reply(bot_id: int, owner_id: int, replied_msg_id: int) -> Optional[int]:
    with db() as conn:
        row = conn.execute("""
        SELECT submission_id FROM hosted_admin_map WHERE bot_id=? AND owner_id=? AND admin_msg_id=?
        """, (bot_id, owner_id, replied_msg_id)).fetchone()
    return int(row[0]) if row else None


# ------------------ Hosted bot logic ------------------
async def is_chat_admin(chat_id: int, user_id: int, bot) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in ("administrator", "creator")
    except Exception:
        return False

def get_thread_id(update: Update) -> int:
    msg = update.effective_message
    if msg and getattr(msg, "is_topic_message", False) and msg.message_thread_id:
        return int(msg.message_thread_id)
    return 0

def owner_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Connect to group", callback_data="owner:connect")],
        [InlineKeyboardButton("📌 My groups", callback_data="owner:groups")],
        [InlineKeyboardButton("⛔ Disconnect bot", callback_data="owner:disconnect")],
    ])

def approve_kb(sub_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{sub_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject:{sub_id}"),
        ]
    ])

def safe_caption(text: str, limit: int = 950) -> str:
    t = (text or "").strip()
    if len(t) > limit:
        t = t[:limit].rstrip() + "..."
    return t

async def send_to_dest(bot, dest: Tuple[int, int], kind: str, file_id: Optional[str], text: str):
    chat_id, thread_id = dest
    kwargs = {}
    if thread_id != 0:
        kwargs["message_thread_id"] = thread_id

    # No footer on approved posts
    if kind == "text":
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    elif kind == "photo":
        await bot.send_photo(chat_id=chat_id, photo=file_id, caption=safe_caption(text), **kwargs)
    elif kind == "video":
        await bot.send_video(chat_id=chat_id, video=file_id, caption=safe_caption(text), **kwargs)

async def hosted_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    if update.effective_chat.type != "private":
        return

    bot_id = context.application.bot.id
    owner_id = context.application.bot_data.get("owner_id")

    uid = update.effective_user.id

    # Show intro once per user per hosted-bot
    if not intro_shown(bot_id, uid):
        mark_intro_shown(bot_id, uid)
        await update.message.reply_text(INTRO_TEXT)

    # Owner sees menu buttons
    if owner_id and uid == owner_id:
        await update.message.reply_text("Owner menu:", reply_markup=owner_menu_kb())

async def hosted_owner_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.from_user:
        return

    bot_id = context.application.bot.id
    owner_id = context.application.bot_data.get("owner_id")
    if q.from_user.id != owner_id:
        await q.answer("Not allowed.", show_alert=True)
        return

    if q.data == "owner:connect":
        await q.answer()
        await q.message.reply_text(
            "To connect this bot to a group/topic:\n\n"
            "1) Add this bot to your group/channel\n"
            "2) Make it admin\n"
            "3) Go to the topic you want (optional)\n"
            "4) Type /connect there\n\n"
            "After that, approvals will post to that chat/topic."
        )
        return

    if q.data == "owner:groups":
        await q.answer()
        dests = list_destinations(bot_id)
        if not dests:
            await q.message.reply_text("No groups connected yet. Add bot to a group and run /connect inside it.")
            return

        lines = ["📌 Connected destinations:"]
        for (chat_id, thread_id) in dests:
            if thread_id:
                lines.append(f"• Chat {chat_id} (topic {thread_id})")
            else:
                lines.append(f"• Chat {chat_id}")
        await q.message.reply_text("\n".join(lines))
        return

    if q.data == "owner:disconnect":
        await q.answer()
        # Disable this bot in platform DB by matching owner+bot_id
        set_hosted_bot_active(owner_id, bot_id, 0)
        await q.message.reply_text("✅ Disconnected. This hosted bot will stop soon.")
        # Stop this hosted bot instance
        try:
            await context.application.stop()
        except Exception:
            pass
        return

async def hosted_connect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Run inside group/channel/topic by owner to connect destination
    if not update.effective_chat or not update.effective_user or not update.effective_message:
        return

    chat = update.effective_chat
    user = update.effective_user
    bot_id = context.application.bot.id
    owner_id = context.application.bot_data.get("owner_id")

    if user.id != owner_id:
        return

    if chat.type not in ("group", "supergroup", "channel"):
        return

    # owner must be admin in that chat to connect
    if not await is_chat_admin(chat.id, user.id, context.bot):
        return

    thread_id = get_thread_id(update)
    upsert_destination(bot_id, chat.id, thread_id)

    # Try delete /connect
    try:
        if update.message:
            await update.message.delete()
    except Exception:
        pass

    where = "this topic" if thread_id else "this chat"
    await context.bot.send_message(
        chat_id=chat.id,
        message_thread_id=thread_id if thread_id else None,
        text=f"✅ Connected to {where}."
    )

async def hosted_user_dm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Users DM hosted bot -> send to owner for approval. No user replies (except intro once).
    if not update.message or not update.effective_user:
        return
    if update.effective_chat.type != "private":
        return

    bot_id = context.application.bot.id
    owner_id = context.application.bot_data.get("owner_id")

    uid = update.effective_user.id
    msg = update.message

    # show intro once
    if not intro_shown(bot_id, uid):
        mark_intro_shown(bot_id, uid)
        await msg.reply_text(INTRO_TEXT)

    # Owner DM messages should be handled elsewhere
    if uid == owner_id:
        return

    # Create submission and send to owner DM
    if msg.text and not msg.text.startswith("/"):
        sid = create_submission(bot_id, owner_id, uid, "text", None, msg.text)
        header = f"📥 <b>Submission</b>\nID: <code>{sid}</code>\nType: <b>TEXT</b>"
        sent = await context.bot.send_message(
            chat_id=owner_id,
            text=f"{header}\n\n{msg.text}",
            parse_mode=ParseMode.HTML,
            reply_markup=approve_kb(sid)
        )
        map_admin_msg(bot_id, owner_id, sent.message_id, sid)
        return

    if msg.photo:
        file_id = msg.photo[-1].file_id
        cap = msg.caption or ""
        sid = create_submission(bot_id, owner_id, uid, "photo", file_id, cap)
        header = f"📥 <b>Submission</b>\nID: <code>{sid}</code>\nType: <b>PHOTO</b>"
        sent = await context.bot.send_photo(
            chat_id=owner_id,
            photo=file_id,
            caption=f"{header}\n\n{cap}" if cap else header,
            parse_mode=ParseMode.HTML,
            reply_markup=approve_kb(sid)
        )
        map_admin_msg(bot_id, owner_id, sent.message_id, sid)
        return

    if msg.video:
        file_id = msg.video.file_id
        cap = msg.caption or ""
        sid = create_submission(bot_id, owner_id, uid, "video", file_id, cap)
        header = f"📥 <b>Submission</b>\nID: <code>{sid}</code>\nType: <b>VIDEO</b>"
        sent = await context.bot.send_video(
            chat_id=owner_id,
            video=file_id,
            caption=f"{header}\n\n{cap}" if cap else header,
            parse_mode=ParseMode.HTML,
            reply_markup=approve_kb(sid)
        )
        map_admin_msg(bot_id, owner_id, sent.message_id, sid)
        return

async def hosted_approve_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.from_user:
        return

    bot_id = context.application.bot.id
    owner_id = context.application.bot_data.get("owner_id")

    if q.from_user.id != owner_id:
        await q.answer("Owner only.", show_alert=True)
        return

    data = q.data or ""
    try:
        action, sid = data.split(":", 1)
        sub_id = int(sid)
    except Exception:
        await q.answer()
        return

    sub = get_submission(sub_id)
    if not sub or sub["bot_id"] != bot_id:
        await q.answer("Not found.", show_alert=True)
        return
    if sub["status"] != "pending":
        await q.answer(f"Already {sub['status']}.", show_alert=True)
        return

    if action == "reject":
        set_submission_status(sub_id, "rejected")
        await q.answer("Rejected.")
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    if action == "approve":
        set_submission_status(sub_id, "approved")
        await q.answer("Approved.")
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        dests = list_destinations(bot_id)
        if not dests:
            await q.message.reply_text("❌ No destination connected. Add bot to group/topic and run /connect there.")
            return

        content = (sub["text"] or "").strip()
        for dest in dests:
            try:
                await send_to_dest(context.bot, dest, sub["kind"], sub["file_id"], content)
            except Exception as e:
                await q.message.reply_text(f"❌ Failed to post to {dest}: {e}")
        return

async def hosted_owner_reply_relay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Owner replies in DM to the submission message -> relay reply to original user
    msg = update.message
    if not msg or not update.effective_user:
        return
    if update.effective_chat.type != "private":
        return

    bot_id = context.application.bot.id
    owner_id = context.application.bot_data.get("owner_id")

    if update.effective_user.id != owner_id:
        return
    if not msg.reply_to_message:
        return

    sub_id = submission_from_admin_reply(bot_id, owner_id, msg.reply_to_message.message_id)
    if not sub_id:
        return

    sub = get_submission(sub_id)
    if not sub or sub["bot_id"] != bot_id:
        return

    user_id = sub["user_id"]

    if msg.text and not msg.text.startswith("/"):
        await context.bot.send_message(chat_id=user_id, text=msg.text)
        return
    if msg.photo:
        cap = msg.caption or ""
        await context.bot.send_photo(chat_id=user_id, photo=msg.photo[-1].file_id, caption=cap)
        return
    if msg.video:
        cap = msg.caption or ""
        await context.bot.send_video(chat_id=user_id, video=msg.video.file_id, caption=cap)
        return


def build_hosted_app(token: str, owner_id: int) -> Application:
    app = Application.builder().token(token).build()
    app.bot_data["owner_id"] = owner_id

    app.add_handler(CommandHandler("start", hosted_start))
    app.add_handler(CallbackQueryHandler(hosted_owner_buttons, pattern=r"^owner:"))
    app.add_handler(CommandHandler("connect", hosted_connect))
    app.add_handler(CallbackQueryHandler(hosted_approve_reject, pattern=r"^(approve|reject):\d+$"))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.REPLY & (filters.TEXT | filters.PHOTO | filters.VIDEO), hosted_owner_reply_relay))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.TEXT | filters.PHOTO | filters.VIDEO), hosted_user_dm))

    return app


# ------------------ Main platform bot ------------------
def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Bot (Paste Token)", callback_data="main:add")],
        [InlineKeyboardButton("🤖 My Bots", callback_data="main:my")],
    ])

def my_bots_kb(bots: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = []
    for b in bots[:20]:
        label = f"@{b['bot_username']} {'✅' if b['active'] else '⛔'}"
        rows.append([InlineKeyboardButton(label, callback_data=f"main:bot:{b['bot_id']}")])
    rows.append([InlineKeyboardButton("⬅ Back", callback_data="main:back")])
    return InlineKeyboardMarkup(rows)

def bot_actions_kb(bot_id: int, active: bool) -> InlineKeyboardMarkup:
    rows = []
    if active:
        rows.append([InlineKeyboardButton("⛔ Disconnect (Stop)", callback_data=f"main:stop:{bot_id}")])
    else:
        rows.append([InlineKeyboardButton("▶ Start (Enable)", callback_data=f"main:start:{bot_id}")])
    rows.append([InlineKeyboardButton("🗑 Delete", callback_data=f"main:del:{bot_id}")])
    rows.append([InlineKeyboardButton("⬅ Back", callback_data="main:my")])
    return InlineKeyboardMarkup(rows)

async def main_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    await update.message.reply_text("GroupFeed Platform", reply_markup=main_menu())

async def main_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.from_user:
        return

    uid = q.from_user.id
    data = q.data or ""

    if data == "main:back":
        await q.answer()
        await q.message.edit_text("GroupFeed Platform", reply_markup=main_menu())
        return

    if data == "main:add":
        await q.answer()
        context.user_data["awaiting_token"] = True
        await q.message.reply_text(
            "Send your BotFather token now.\n\n"
            "Format example:\n123456789:AAAbbbCCCdddEEEfff\n\n"
            "⚠️ Only send tokens to bots/services you trust."
        )
        return

    if data == "main:my":
        await q.answer()
        bots = list_owner_bots(uid)
        if not bots:
            await q.message.reply_text("You have no hosted bots yet. Click Add Bot and paste your token.")
            return
        await q.message.reply_text("Your bots:", reply_markup=my_bots_kb(bots))
        return

    if data.startswith("main:bot:"):
        await q.answer()
        bot_id = int(data.split(":")[-1])
        bots = list_owner_bots(uid)
        b = next((x for x in bots if x["bot_id"] == bot_id), None)
        if not b:
            await q.message.reply_text("Bot not found.")
            return
        txt = f"Bot: @{b['bot_username']}\nStatus: {'Active' if b['active'] else 'Stopped'}"
        await q.message.reply_text(txt, reply_markup=bot_actions_kb(bot_id, b["active"]))
        return

    if data.startswith("main:stop:"):
        await q.answer()
        bot_id = int(data.split(":")[-1])
        set_hosted_bot_active(uid, bot_id, 0)
        await q.message.reply_text("✅ Disconnected. (It may take a short moment to fully stop.)")
        return

    if data.startswith("main:start:"):
        await q.answer()
        bot_id = int(data.split(":")[-1])
        set_hosted_bot_active(uid, bot_id, 1)
        await q.message.reply_text("▶ Enabled. (It may take a short moment to start.)")
        return

    if data.startswith("main:del:"):
        await q.answer()
        bot_id = int(data.split(":")[-1])
        remove_hosted_bot(uid, bot_id)
        await q.message.reply_text("🗑 Deleted.")
        return

async def main_receive_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    uid = update.effective_user.id

    if not context.user_data.get("awaiting_token"):
        return

    token = (update.message.text or "").strip()
    context.user_data["awaiting_token"] = False

    if not TOKEN_RE.match(token):
        await update.message.reply_text("❌ Invalid token format. Click Add Bot again and paste the correct token.")
        return

    # Validate token by calling getMe using a temp app bot object
    try:
        tmp_app = Application.builder().token(token).build()
        await tmp_app.initialize()
        me = await tmp_app.bot.get_me()
        await tmp_app.shutdown()
    except Exception as e:
        await update.message.reply_text(f"❌ Token failed: {e}")
        return

    add_hosted_bot(owner_id=uid, bot_id=me.id, bot_username=me.username, token=token)
    await update.message.reply_text(
        f"✅ Bot hosted!\n\n"
        f"Your bot: @{me.username}\n\n"
        f"Now open @{me.username} and press /start.\n"
        f"As owner, you will see: Connect to group / My groups / Disconnect bot."
    )


# ------------------ Runner to start/stop hosted bots ------------------
class HostedRunner:
    def __init__(self):
        self.apps: Dict[int, Application] = {}   # bot_id -> app
        self.tasks: Dict[int, asyncio.Task] = {} # bot_id -> polling task

    async def start_hosted(self, owner_id: int, bot_id: int, token: str):
        if bot_id in self.tasks:
            return
        app = build_hosted_app(token=token, owner_id=owner_id)
        self.apps[bot_id] = app

        async def _run():
            await app.initialize()
            await app.start()
            # Start polling without blocking forever
            await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            # Keep alive until stopped
            try:
                while True:
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                pass
            finally:
                try:
                    await app.updater.stop()
                except Exception:
                    pass
                try:
                    await app.stop()
                except Exception:
                    pass
                try:
                    await app.shutdown()
                except Exception:
                    pass

        self.tasks[bot_id] = asyncio.create_task(_run())

    async def stop_hosted(self, bot_id: int):
        t = self.tasks.get(bot_id)
        if not t:
            return
        t.cancel()
        try:
            await t
        except Exception:
            pass
        self.tasks.pop(bot_id, None)
        self.apps.pop(bot_id, None)

    async def sync_from_db(self):
        # Ensure active bots are running; inactive bots are stopped
        active = list_all_active_bots()
        active_ids = set(b["bot_id"] for b in active)

        # stop any running that aren't active anymore
        for running_id in list(self.tasks.keys()):
            if running_id not in active_ids:
                await self.stop_hosted(running_id)

        # start any active not running
        for b in active:
            if b["bot_id"] not in self.tasks:
                await self.start_hosted(b["owner_id"], b["bot_id"], b["token"])


async def main_async():
    init_db()

    runner = HostedRunner()

    # Start main platform bot
    main_app = Application.builder().token(MAIN_BOT_TOKEN).build()
    main_app.add_handler(CommandHandler("start", main_start))
    main_app.add_handler(CallbackQueryHandler(main_buttons, pattern=r"^main:"))
    main_app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, main_receive_token))

    await main_app.initialize()
    await main_app.start()
    await main_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    # Start hosted bots already in DB + keep syncing
    await runner.sync_from_db()

    try:
        while True:
            await asyncio.sleep(5)
            await runner.sync_from_db()
    except asyncio.CancelledError:
        pass
    finally:
        # stop hosted
        for bid in list(runner.tasks.keys()):
            await runner.stop_hosted(bid)

        # stop main
        try:
            await main_app.updater.stop()
        except Exception:
            pass
        try:
            await main_app.stop()
        except Exception:
            pass
        try:
            await main_app.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main_async())
