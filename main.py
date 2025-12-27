import os
import time
import sqlite3
import secrets
from typing import Optional, Dict, Any, Tuple, List

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

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_FILE = os.getenv("DB_FILE", "groupfeed_saas.db").strip()
INTRO_TEXT = os.getenv(
    "INTRO_TEXT",
    "You can contact us using this bot.\n\nBot created by @GroupFeedBot"
).strip()
SILENT_AFTER_INTRO = os.getenv("SILENT_AFTER_INTRO", "1").strip() in ("1", "true", "True", "yes", "YES")

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN")


# ---------------- DB ----------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            group_id INTEGER PRIMARY KEY,
            token TEXT NOT NULL,
            target_chat_id INTEGER NOT NULL,
            target_thread_id INTEGER NOT NULL, -- 0 = no topic
            created_ts INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_bindings (
            group_id INTEGER NOT NULL,
            admin_id INTEGER NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(group_id, admin_id)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS user_group (
            user_id INTEGER PRIMARY KEY,
            group_id INTEGER NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,     -- text | photo | video
            file_id TEXT,
            text TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_ts INTEGER NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_msg_map (
            admin_id INTEGER NOT NULL,
            admin_msg_id INTEGER NOT NULL,
            submission_id INTEGER NOT NULL,
            PRIMARY KEY(admin_id, admin_msg_id)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS user_intro (
            user_id INTEGER PRIMARY KEY,
            shown INTEGER NOT NULL DEFAULT 1
        )
        """)

def set_user_intro_shown(user_id: int):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO user_intro(user_id, shown) VALUES (?,1)", (user_id,))

def user_intro_shown(user_id: int) -> bool:
    with db() as conn:
        row = conn.execute("SELECT 1 FROM user_intro WHERE user_id=?", (user_id,)).fetchone()
    return bool(row)

def upsert_group(group_id: int, target_chat_id: int, target_thread_id: int) -> str:
    token = secrets.token_urlsafe(10)
    with db() as conn:
        conn.execute("""
        INSERT OR REPLACE INTO groups(group_id, token, target_chat_id, target_thread_id, created_ts, active)
        VALUES (?,?,?,?,?,1)
        """, (group_id, token, target_chat_id, target_thread_id, int(time.time())))
    return token

def get_group(group_id: int) -> Optional[Dict[str, Any]]:
    with db() as conn:
        row = conn.execute("""
        SELECT group_id, token, target_chat_id, target_thread_id, active
        FROM groups WHERE group_id=?
        """, (group_id,)).fetchone()
    if not row:
        return None
    return {
        "group_id": int(row[0]),
        "token": str(row[1]),
        "target_chat_id": int(row[2]),
        "target_thread_id": int(row[3]),
        "active": int(row[4]) == 1,
    }

def get_group_by_token(token: str) -> Optional[Dict[str, Any]]:
    with db() as conn:
        row = conn.execute("""
        SELECT group_id, token, target_chat_id, target_thread_id, active
        FROM groups WHERE token=?
        """, (token,)).fetchone()
    if not row:
        return None
    return {
        "group_id": int(row[0]),
        "token": str(row[1]),
        "target_chat_id": int(row[2]),
        "target_thread_id": int(row[3]),
        "active": int(row[4]) == 1,
    }

def bind_admin(group_id: int, admin_id: int):
    with db() as conn:
        conn.execute("""
        INSERT OR REPLACE INTO admin_bindings(group_id, admin_id, enabled)
        VALUES (?,?,1)
        """, (group_id, admin_id))

def set_admin_enabled(group_id: int, admin_id: int, enabled: int):
    with db() as conn:
        conn.execute("""
        INSERT OR REPLACE INTO admin_bindings(group_id, admin_id, enabled)
        VALUES (?,?,?)
        """, (group_id, admin_id, int(enabled)))

def list_enabled_admins(group_id: int) -> List[int]:
    with db() as conn:
        rows = conn.execute("""
        SELECT admin_id FROM admin_bindings
        WHERE group_id=? AND enabled=1
        """, (group_id,)).fetchall()
    return [int(r[0]) for r in rows]

def set_user_group(user_id: int, group_id: int):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO user_group(user_id, group_id) VALUES (?,?)", (user_id, group_id))

def get_user_group(user_id: int) -> Optional[int]:
    with db() as conn:
        row = conn.execute("SELECT group_id FROM user_group WHERE user_id=?", (user_id,)).fetchone()
    return int(row[0]) if row else None

def create_submission(group_id: int, user_id: int, kind: str, file_id: Optional[str], text: str) -> int:
    with db() as conn:
        cur = conn.execute("""
        INSERT INTO submissions(group_id, user_id, kind, file_id, text, status, created_ts)
        VALUES (?,?,?,?,?,'pending',?)
        """, (group_id, user_id, kind, file_id, text or "", int(time.time())))
        return int(cur.lastrowid)

def get_submission(sub_id: int) -> Optional[Dict[str, Any]]:
    with db() as conn:
        row = conn.execute("""
        SELECT id, group_id, user_id, kind, file_id, text, status
        FROM submissions WHERE id=?
        """, (sub_id,)).fetchone()
    if not row:
        return None
    return {
        "id": int(row[0]),
        "group_id": int(row[1]),
        "user_id": int(row[2]),
        "kind": str(row[3]),
        "file_id": row[4],
        "text": row[5] or "",
        "status": str(row[6]),
    }

def set_submission_status(sub_id: int, status: str):
    with db() as conn:
        conn.execute("UPDATE submissions SET status=? WHERE id=?", (status, sub_id))

def map_admin_msg(admin_id: int, admin_msg_id: int, submission_id: int):
    with db() as conn:
        conn.execute("""
        INSERT OR REPLACE INTO admin_msg_map(admin_id, admin_msg_id, submission_id)
        VALUES (?,?,?)
        """, (admin_id, admin_msg_id, submission_id))

def submission_from_admin_reply(admin_id: int, replied_msg_id: int) -> Optional[int]:
    with db() as conn:
        row = conn.execute("""
        SELECT submission_id FROM admin_msg_map WHERE admin_id=? AND admin_msg_id=?
        """, (admin_id, replied_msg_id)).fetchone()
    return int(row[0]) if row else None


# ---------------- Helpers ----------------
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

def submit_button(bot_username: str, token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Submit anonymously", url=f"https://t.me/{bot_username}?start=g_{token}")]
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


async def post_to_group_destination(bot, group_cfg: Dict[str, Any], sub: Dict[str, Any]):
    # IMPORTANT: NO footer appended (per your requirement)
    chat_id = group_cfg["target_chat_id"]
    thread_id = group_cfg["target_thread_id"]
    kwargs = {}
    if thread_id != 0:
        kwargs["message_thread_id"] = thread_id

    content = (sub["text"] or "").strip()

    if sub["kind"] == "text":
        await bot.send_message(chat_id=chat_id, text=content, **kwargs)
    elif sub["kind"] == "photo":
        await bot.send_photo(chat_id=chat_id, photo=sub["file_id"], caption=safe_caption(content), **kwargs)
    elif sub["kind"] == "video":
        await bot.send_video(chat_id=chat_id, video=sub["file_id"], caption=safe_caption(content), **kwargs)


async def send_to_group_admins(bot, group_id: int, sub: Dict[str, Any]):
    admins = list_enabled_admins(group_id)
    if not admins:
        return  # no registered admins; submission will just wait

    header = f"📥 <b>Submission</b>\nID: <code>{sub['id']}</code>\nGroup: <code>{group_id}</code>\nType: <b>{sub['kind'].upper()}</b>"
    caption_text = (sub["text"] or "").strip()

    for admin_id in admins:
        try:
            if sub["kind"] == "text":
                sent = await bot.send_message(
                    chat_id=admin_id,
                    text=f"{header}\n\n{caption_text if caption_text else '(empty)'}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=approve_kb(sub["id"])
                )
                map_admin_msg(admin_id, sent.message_id, sub["id"])

            elif sub["kind"] == "photo":
                cap = f"{header}\n\n{caption_text}" if caption_text else header
                sent = await bot.send_photo(
                    chat_id=admin_id,
                    photo=sub["file_id"],
                    caption=cap,
                    parse_mode=ParseMode.HTML,
                    reply_markup=approve_kb(sub["id"])
                )
                map_admin_msg(admin_id, sent.message_id, sub["id"])

            elif sub["kind"] == "video":
                cap = f"{header}\n\n{caption_text}" if caption_text else header
                sent = await bot.send_video(
                    chat_id=admin_id,
                    video=sub["file_id"],
                    caption=cap,
                    parse_mode=ParseMode.HTML,
                    reply_markup=approve_kb(sub["id"])
                )
                map_admin_msg(admin_id, sent.message_id, sub["id"])

        except Exception:
            # admin hasn't started bot / can't be messaged
            continue


# ---------------- Commands ----------------
async def connect_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Group/channel admin runs /connect in the target topic/chat.
    - Saves destination for that group
    - Registers the admin (so they receive approvals in DM)
    - Posts the "Submit anonymously" button
    """
    if not update.effective_chat or not update.effective_user or not update.effective_message:
        return

    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in ("group", "supergroup", "channel"):
        return

    # must be chat admin to connect
    if not await is_chat_admin(chat.id, user.id, context.bot):
        return

    thread_id = get_thread_id(update)
    token = upsert_group(group_id=chat.id, target_chat_id=chat.id, target_thread_id=thread_id)

    # admin must /start bot once to receive DM approvals
    bind_admin(chat.id, user.id)

    me = await context.bot.get_me()
    kb = submit_button(me.username, token)

    # Try delete /connect command
    try:
        if update.message:
            await update.message.delete()
    except Exception:
        pass

    where = "this topic" if thread_id != 0 else "this chat"
    await context.bot.send_message(
        chat_id=chat.id,
        message_thread_id=thread_id if thread_id != 0 else None,
        text=(
            f"✅ Connected to {where}.\n\n"
            "Admins: open the bot once in DM (/start) to receive approvals.\n"
            "Users: tap the button below to submit anonymously."
        ),
        reply_markup=kb
    )

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    if update.effective_chat.type != "private":
        return

    uid = update.effective_user.id

    # If deep-link contains group token, bind user to that group
    args = context.args or []
    if args and args[0].startswith("g_"):
        token = args[0][2:]
        g = get_group_by_token(token)
        if g and g["active"]:
            set_user_group(uid, g["group_id"])

    # Show intro once
    if not user_intro_shown(uid):
        set_user_intro_shown(uid)
        await update.message.reply_text(INTRO_TEXT)


async def admin_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin can enable approvals for themselves in a group: /admin_on <group_id> (optional)"""
    if not update.message or not update.effective_user:
        return
    if update.effective_chat.type != "private":
        return

    uid = update.effective_user.id
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /admin_on <group_id>\n(Then you will receive approvals for that group.)")
        return

    try:
        gid = int(args[0])
    except Exception:
        await update.message.reply_text("Invalid group_id.")
        return

    g = get_group(gid)
    if not g:
        await update.message.reply_text("Group not found (not connected yet).")
        return

    # Only real chat admins should enable themselves
    if not await is_chat_admin(gid, uid, context.bot):
        await update.message.reply_text("You are not an admin of that group.")
        return

    bind_admin(gid, uid)
    await update.message.reply_text("✅ Enabled. You will receive approvals for that group in DM.")


# ---------------- User submissions ----------------
async def user_private_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    if update.effective_chat.type != "private":
        return

    msg = update.message
    uid = update.effective_user.id

    # Show intro once (then silent)
    if not user_intro_shown(uid):
        set_user_intro_shown(uid)
        await msg.reply_text(INTRO_TEXT)

    group_id = get_user_group(uid)
    if not group_id:
        # Not linked to a group yet; stay silent or minimal message.
        if not SILENT_AFTER_INTRO:
            await msg.reply_text("Open the group’s Submit button first, then send your message here.")
        return

    g = get_group(group_id)
    if not g or not g["active"]:
        return

    # Create submission and send to admins
    if msg.text and not msg.text.startswith("/"):
        sid = create_submission(group_id, uid, "text", None, msg.text)
        sub = get_submission(sid)
        await send_to_group_admins(context.bot, group_id, sub)
        return

    if msg.photo:
        file_id = msg.photo[-1].file_id
        caption = msg.caption or ""
        sid = create_submission(group_id, uid, "photo", file_id, caption)
        sub = get_submission(sid)
        await send_to_group_admins(context.bot, group_id, sub)
        return

    if msg.video:
        file_id = msg.video.file_id
        caption = msg.caption or ""
        sid = create_submission(group_id, uid, "video", file_id, caption)
        sub = get_submission(sid)
        await send_to_group_admins(context.bot, group_id, sub)
        return


# ---------------- Admin approve/reject ----------------
async def approve_reject_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.from_user:
        return

    admin_id = q.from_user.id
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

    group_id = sub["group_id"]

    # Must be admin of that group
    if not await is_chat_admin(group_id, admin_id, context.bot):
        await q.answer("Admins only (for that group).", show_alert=True)
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

        g = get_group(group_id)
        if not g:
            await q.message.reply_text("❌ Group destination missing.")
            return

        try:
            await post_to_group_destination(context.bot, g, sub)
        except Exception as e:
            await q.message.reply_text(f"❌ Failed to post to group: {e}")
        return


# ---------------- Admin reply relay -> user ----------------
async def admin_reply_relay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin replies in DM to the bot's submission message -> bot sends that message to the user.
    """
    msg = update.message
    if not msg or not update.effective_user:
        return
    if update.effective_chat.type != "private":
        return

    admin_id = update.effective_user.id
    if not msg.reply_to_message:
        return

    sub_id = submission_from_admin_reply(admin_id, msg.reply_to_message.message_id)
    if not sub_id:
        return

    sub = get_submission(sub_id)
    if not sub:
        return

    group_id = sub["group_id"]
    # Ensure admin is admin of that group
    if not await is_chat_admin(group_id, admin_id, context.bot):
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


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("connect", connect_cmd))
    app.add_handler(CommandHandler("admin_on", admin_on_cmd))

    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.TEXT | filters.PHOTO | filters.VIDEO), user_private_handler))
    app.add_handler(CallbackQueryHandler(approve_reject_cb, pattern=r"^(approve|reject):\d+$"))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.REPLY & (filters.TEXT | filters.PHOTO | filters.VIDEO), admin_reply_relay))

    print("GroupFeed multi-tenant bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
