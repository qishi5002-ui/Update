import os
import time
import sqlite3
from typing import Optional, Dict, Any, Tuple

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
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

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DB_FILE = os.getenv("DB_FILE", "submission_bot.db").strip()

INTRO_TEXT = "You can contact us using this bot.\n\nBot created by @GroupFeedBot"

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN")
if OWNER_ID == 0:
    raise RuntimeError("Missing OWNER_ID")


# ---------------------- DB ----------------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS destinations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            target_chat_id INTEGER NOT NULL,
            target_thread_id INTEGER NOT NULL,   -- 0 = no topic
            created_ts INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            UNIQUE(owner_id, target_chat_id, target_thread_id)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,          -- text | photo | video
            file_id TEXT,                -- for photo/video
            text TEXT,                   -- text or caption
            status TEXT NOT NULL DEFAULT 'pending', -- pending/approved/rejected
            created_ts INTEGER NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_map (
            owner_id INTEGER NOT NULL,
            admin_msg_id INTEGER NOT NULL,
            submission_id INTEGER NOT NULL,
            PRIMARY KEY(owner_id, admin_msg_id)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS user_intro (
            user_id INTEGER PRIMARY KEY,
            shown INTEGER NOT NULL DEFAULT 1
        )
        """)

def upsert_destination(owner_id: int, target_chat_id: int, target_thread_id: int):
    with db() as conn:
        conn.execute("""
        INSERT OR IGNORE INTO destinations(owner_id, target_chat_id, target_thread_id, created_ts, active)
        VALUES (?,?,?,?,1)
        """, (owner_id, target_chat_id, target_thread_id, int(time.time())))
        conn.execute("""
        UPDATE destinations SET active=1 WHERE owner_id=? AND target_chat_id=? AND target_thread_id=?
        """, (owner_id, target_chat_id, target_thread_id))

def list_destinations(owner_id: int) -> list[Tuple[int, int]]:
    with db() as conn:
        rows = conn.execute("""
        SELECT target_chat_id, target_thread_id FROM destinations
        WHERE owner_id=? AND active=1
        ORDER BY id ASC
        """, (owner_id,)).fetchall()
    return [(int(r[0]), int(r[1])) for r in rows]

def create_submission(user_id: int, kind: str, file_id: Optional[str], text: str) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO submissions(user_id, kind, file_id, text, status, created_ts) "
            "VALUES (?,?,?,?, 'pending', ?)",
            (user_id, kind, file_id, text or "", int(time.time()))
        )
        return int(cur.lastrowid)

def get_submission(sub_id: int) -> Optional[Dict[str, Any]]:
    with db() as conn:
        row = conn.execute(
            "SELECT id, user_id, kind, file_id, text, status FROM submissions WHERE id=?",
            (sub_id,)
        ).fetchone()
    if not row:
        return None
    return {
        "id": int(row[0]),
        "user_id": int(row[1]),
        "kind": row[2],
        "file_id": row[3],
        "text": row[4] or "",
        "status": row[5],
    }

def set_submission_status(sub_id: int, status: str):
    with db() as conn:
        conn.execute("UPDATE submissions SET status=? WHERE id=?", (status, sub_id))

def map_admin_message(owner_id: int, admin_msg_id: int, submission_id: int):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO admin_map(owner_id, admin_msg_id, submission_id) VALUES (?,?,?)",
            (owner_id, admin_msg_id, submission_id)
        )

def submission_from_admin_reply(owner_id: int, replied_msg_id: int) -> Optional[int]:
    with db() as conn:
        row = conn.execute(
            "SELECT submission_id FROM admin_map WHERE owner_id=? AND admin_msg_id=?",
            (owner_id, replied_msg_id)
        ).fetchone()
    return int(row[0]) if row else None

def intro_shown(user_id: int) -> bool:
    with db() as conn:
        row = conn.execute("SELECT 1 FROM user_intro WHERE user_id=?", (user_id,)).fetchone()
    return bool(row)

def mark_intro_shown(user_id: int):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO user_intro(user_id, shown) VALUES (?,1)", (user_id,))


# ---------------------- Helpers ----------------------
def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID

async def is_chat_admin(chat_id: int, user_id: int, bot) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False

def get_thread_id(update: Update) -> int:
    msg = update.effective_message
    if msg and getattr(msg, "is_topic_message", False) and msg.message_thread_id:
        return int(msg.message_thread_id)
    return 0

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

    # IMPORTANT: no footer appended here (per your request)
    if kind == "text":
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    elif kind == "photo":
        await bot.send_photo(chat_id=chat_id, photo=file_id, caption=safe_caption(text), **kwargs)
    elif kind == "video":
        await bot.send_video(chat_id=chat_id, video=file_id, caption=safe_caption(text), **kwargs)

async def post_approved_to_all(context: ContextTypes.DEFAULT_TYPE, sub: Dict[str, Any]):
    destinations = list_destinations(OWNER_ID)
    if not destinations:
        await context.bot.send_message(chat_id=OWNER_ID, text="❌ No destinations connected. Use /connect in a group/topic.")
        return

    content = (sub["text"] or "").strip()
    out_text = content  # no footer

    for dest in destinations:
        try:
            await send_to_dest(context.bot, dest, sub["kind"], sub["file_id"], out_text)
        except Exception as e:
            await context.bot.send_message(chat_id=OWNER_ID, text=f"❌ Failed posting to {dest}: {e}")

async def broadcast_to_all(context: ContextTypes.DEFAULT_TYPE, kind: str, file_id: Optional[str], text: str):
    destinations = list_destinations(OWNER_ID)
    if not destinations:
        await context.bot.send_message(chat_id=OWNER_ID, text="❌ No destinations connected. Use /connect in a group/topic.")
        return

    out_text = (text or "").strip()  # no footer
    for dest in destinations:
        try:
            await send_to_dest(context.bot, dest, kind, file_id, out_text)
        except Exception as e:
            await context.bot.send_message(chat_id=OWNER_ID, text=f"❌ Failed broadcasting to {dest}: {e}")


# ---------------------- Commands ----------------------
async def connect_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Run /connect inside the target group/channel/topic to add it as a destination.
    Only OWNER can connect.
    """
    if not update.effective_chat or not update.effective_user or not update.effective_message:
        return

    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in ("group", "supergroup", "channel"):
        return

    if not is_owner(user.id):
        return

    # must be admin in that chat to post
    if not await is_chat_admin(chat.id, user.id, context.bot):
        return

    thread_id = get_thread_id(update)
    upsert_destination(owner_id=OWNER_ID, target_chat_id=chat.id, target_thread_id=thread_id)

    # delete /connect command if possible
    try:
        if update.message:
            await update.message.delete()
    except Exception:
        pass

    where = "this topic" if thread_id != 0 else "this chat"
    await context.bot.send_message(
        chat_id=chat.id,
        message_thread_id=thread_id if thread_id != 0 else None,
        text=f"✅ Connected to {where}."
    )

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Only show intro message here (per your request)
    if not update.message or not update.effective_user:
        return
    if update.effective_chat.type != "private":
        return

    uid = update.effective_user.id
    if not intro_shown(uid):
        mark_intro_shown(uid)
        await update.message.reply_text(INTRO_TEXT)

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    OWNER in DM: reply to any message/photo/video with /broadcast to send it to all connected destinations.
    """
    if not update.message or not update.effective_user:
        return
    if update.effective_chat.type != "private":
        return
    if not is_owner(update.effective_user.id):
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a message/photo/video with /broadcast")
        return

    src = update.message.reply_to_message

    if src.text and not src.text.startswith("/"):
        await broadcast_to_all(context, "text", None, src.text)
        await update.message.reply_text("✅ Broadcast sent.")
        return

    if src.photo:
        caption = src.caption or ""
        await broadcast_to_all(context, "photo", src.photo[-1].file_id, caption)
        await update.message.reply_text("✅ Broadcast sent.")
        return

    if src.video:
        caption = src.caption or ""
        await broadcast_to_all(context, "video", src.video.file_id, caption)
        await update.message.reply_text("✅ Broadcast sent.")
        return

    await update.message.reply_text("Reply to a text/photo/video.")


# ---------------------- User submissions (DM) ----------------------
async def user_private_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    if update.effective_chat.type != "private":
        return

    user_id = update.effective_user.id
    msg = update.message

    # Show intro only once, then stay silent forever (no other replies)
    if not intro_shown(user_id):
        mark_intro_shown(user_id)
        await msg.reply_text(INTRO_TEXT)
        # continue processing submission after showing intro once

    # create submission and send to owner DM for approval
    if msg.text and not msg.text.startswith("/"):
        sub_id = create_submission(user_id=user_id, kind="text", file_id=None, text=msg.text)
        sub = get_submission(sub_id)
        header = f"📥 <b>Submission</b>\nID: <code>{sub_id}</code>\nType: <b>TEXT</b>"
        sent = await context.bot.send_message(
            chat_id=OWNER_ID,
            text=f"{header}\n\n{sub['text']}",
            parse_mode=ParseMode.HTML,
            reply_markup=approve_kb(sub_id)
        )
        map_admin_message(OWNER_ID, sent.message_id, sub_id)
        return

    if msg.photo:
        file_id = msg.photo[-1].file_id
        caption = msg.caption or ""
        sub_id = create_submission(user_id=user_id, kind="photo", file_id=file_id, text=caption)
        header = f"📥 <b>Submission</b>\nID: <code>{sub_id}</code>\nType: <b>PHOTO</b>"
        sent = await context.bot.send_photo(
            chat_id=OWNER_ID,
            photo=file_id,
            caption=f"{header}\n\n{caption}" if caption else header,
            parse_mode=ParseMode.HTML,
            reply_markup=approve_kb(sub_id)
        )
        map_admin_message(OWNER_ID, sent.message_id, sub_id)
        return

    if msg.video:
        file_id = msg.video.file_id
        caption = msg.caption or ""
        sub_id = create_submission(user_id=user_id, kind="video", file_id=file_id, text=caption)
        header = f"📥 <b>Submission</b>\nID: <code>{sub_id}</code>\nType: <b>VIDEO</b>"
        sent = await context.bot.send_video(
            chat_id=OWNER_ID,
            video=file_id,
            caption=f"{header}\n\n{caption}" if caption else header,
            parse_mode=ParseMode.HTML,
            reply_markup=approve_kb(sub_id)
        )
        map_admin_message(OWNER_ID, sent.message_id, sub_id)
        return

    # everything else ignored silently


# ---------------------- Admin approve/reject ----------------------
async def approve_reject_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.from_user:
        return

    if q.from_user.id != OWNER_ID:
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
    if not sub:
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

        # post to all connected destinations (NO footer)
        await post_approved_to_all(context, sub)
        return


# ---------------------- Admin reply relay -> user ----------------------
async def admin_reply_relay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    OWNER replies in DM to the bot's submission message -> bot relays reply to the original user.
    """
    msg = update.message
    if not msg or not update.effective_user:
        return
    if update.effective_chat.type != "private":
        return
    if update.effective_user.id != OWNER_ID:
        return

    if not msg.reply_to_message:
        return

    sub_id = submission_from_admin_reply(OWNER_ID, msg.reply_to_message.message_id)
    if not sub_id:
        return

    sub = get_submission(sub_id)
    if not sub:
        return

    user_id = sub["user_id"]

    # Relay admin reply (copy)
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


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # Connect destinations
    app.add_handler(CommandHandler("connect", connect_cmd))

    # Intro
    app.add_handler(CommandHandler("start", start_cmd))

    # User submissions (private)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.TEXT | filters.PHOTO | filters.VIDEO), user_private_handler))

    # Owner broadcast
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))

    # Approve/Reject
    app.add_handler(CallbackQueryHandler(approve_reject_cb, pattern=r"^(approve|reject):\d+$"))

    # Owner reply relay
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.REPLY & (filters.TEXT | filters.PHOTO | filters.VIDEO), admin_reply_relay))

    print("Bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
