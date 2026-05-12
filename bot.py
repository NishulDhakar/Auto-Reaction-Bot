"""Auto Reaction Bot."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import psycopg2
import psycopg2.extras



from telegram import (
    Bot,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReactionTypeEmoji,
    Update,
)
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ── .env loader ──────────────────────────────────────────────────────
_env_file = Path(".env")
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# ── Config ───────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
SUPER_ADMIN_IDS = frozenset(
    int(x) for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip()
)
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL", "@NextBuilders")
REACTIONS = tuple(
    x.strip()
    for x in os.environ.get("REACTION_POOL", "👍,🔥,❤,🎉,🥰,👏,🤩,⚡,💯,😍").split(",")
    if x.strip()
)
WELCOME_IMAGE = Path("welcom.png")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# ── Userbot config ────────────────────────────────────────────────────
_API_ID   = int(os.environ["API_ID"])   if os.environ.get("API_ID")   else 0
_API_HASH = os.environ.get("API_HASH", "")
_USERBOT_SESSIONS = [
    s.strip()
    for s in os.environ.get("USERBOT_SESSIONS", "").split(",")
    if s.strip()
]
_userbot_clients: list = []   # Pyrogram Client instances — populated at startup
_channel_usernames: dict[int, str] = {}  # chat_id → username (no @)
_app: object = None  # PTB Application reference set in _post_init

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

WAITING_CHANNEL = 1
WAITING_PHONE = 10
WAITING_CODE  = 11
WAITING_2FA   = 12

_pending_add: dict[int, dict] = {}  # admin_uid → {client, phone, sent}

# ── PostgreSQL / Supabase ─────────────────────────────────────────────
_conn: "psycopg2.extensions.connection | None" = None
_lock = asyncio.Lock()


def _db():
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(DATABASE_URL)
        with _conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    uid BIGINT PRIMARY KEY,
                    name TEXT,
                    status TEXT DEFAULT 'active'
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS channels (
                    cid BIGINT PRIMARY KEY,
                    title TEXT,
                    username TEXT,
                    chat_type TEXT DEFAULT 'channel',
                    owner BIGINT,
                    status TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS userbot_sessions (
                    id SERIAL PRIMARY KEY,
                    phone TEXT UNIQUE NOT NULL,
                    session_string TEXT NOT NULL
                )
            """)
        _conn.commit()
    return _conn


def _save_user(uid: int, name: str) -> None:
    c = _db()
    with c.cursor() as cur:
        cur.execute(
            "INSERT INTO users(uid,name,status) VALUES(%s,%s,'active') "
            "ON CONFLICT(uid) DO UPDATE SET name=EXCLUDED.name, status='active'",
            (uid, name),
        )
    c.commit()


def _block_user(uid: int) -> None:
    c = _db()
    with c.cursor() as cur:
        cur.execute("UPDATE users SET status='blocked' WHERE uid=%s", (uid,))
    c.commit()


def _get_active_uids() -> list[int]:
    c = _db()
    with c.cursor() as cur:
        cur.execute("SELECT uid FROM users WHERE status='active'")
        return [r[0] for r in cur.fetchall()]


def _count_users() -> int:
    c = _db()
    with c.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM users WHERE status='active'")
        return (cur.fetchone() or (0,))[0]


def _count_users_all() -> dict:
    c = _db()
    with c.cursor() as cur:
        cur.execute("SELECT status, COUNT(*) FROM users GROUP BY status")
        counts: dict = {"active": 0, "blocked": 0, "total": 0}
        for status, count in cur.fetchall():
            counts[status] = count
            counts["total"] += count
    return counts


def _save_channel(cid: int, title: str, username: str | None,
                  chat_type: str, owner: int | None, status: str) -> None:
    c = _db()
    with c.cursor() as cur:
        cur.execute(
            "INSERT INTO channels(cid,title,username,chat_type,owner,status) VALUES(%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(cid) DO UPDATE SET title=EXCLUDED.title, username=EXCLUDED.username, "
            "chat_type=EXCLUDED.chat_type, owner=COALESCE(EXCLUDED.owner,channels.owner), status=EXCLUDED.status",
            (cid, title, username, chat_type, owner, status),
        )
    c.commit()


def _get_channels(owner: int | None = None) -> list[dict]:
    c = _db()
    with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if owner is not None:
            cur.execute("SELECT cid,title,username,chat_type,status FROM channels WHERE owner=%s", (owner,))
        else:
            cur.execute("SELECT cid,title,username,chat_type,status FROM channels")
        return [dict(r) for r in cur.fetchall()]


def _count_channels() -> int:
    c = _db()
    with c.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM channels")
        return (cur.fetchone() or (0,))[0]


def _get_all_channel_ids() -> list[int]:
    c = _db()
    with c.cursor() as cur:
        cur.execute("SELECT cid FROM channels")
        return [r[0] for r in cur.fetchall()]


def _save_userbot_session(phone: str, session_string: str) -> None:
    c = _db()
    with c.cursor() as cur:
        cur.execute(
            "INSERT INTO userbot_sessions(phone, session_string) VALUES(%s,%s) "
            "ON CONFLICT(phone) DO UPDATE SET session_string=EXCLUDED.session_string",
            (phone, session_string),
        )
    c.commit()


def _get_db_userbot_sessions() -> list[dict]:
    c = _db()
    with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT id, phone, session_string FROM userbot_sessions")
        return [dict(r) for r in cur.fetchall()]


def _get_all_channels_brief() -> list[tuple[int, str | None]]:
    c = _db()
    with c.cursor() as cur:
        cur.execute("SELECT cid, username FROM channels")
        return [(r[0], r[1]) for r in cur.fetchall()]


async def _run(fn, *args):
    async with _lock:
        return await asyncio.to_thread(fn, *args)


# ── Helpers ───────────────────────────────────────────────────────────
async def _is_subscribed(bot, uid: int) -> bool | None:
    """True=subscribed, False=not subscribed, None=check failed (bot not admin of channel)."""
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, uid)
        return member.status not in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED)
    except TelegramError as e:
        log.warning("_is_subscribed(%s): %s", uid, e)
        return None


def _msg(query) -> Message | None:
    """Narrow query.message from MaybeInaccessibleMessage to Message."""
    return query.message if isinstance(query.message, Message) else None


def _home_keyboard(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("➕ Add My Channel", callback_data="add_channel")],
        [InlineKeyboardButton("📋 My Channels", callback_data="my_channels")],
        [InlineKeyboardButton("❓ How It Works", callback_data="how_it_works")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("🔧 Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)


def _join_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}")],
        [InlineKeyboardButton("✅ I've Joined — Check Now", callback_data="check_join")],
    ])


JOIN_TEXT = (
    "👋 *Welcome to Auto Reactions Bot!*\n\n"
    "To use this bot, you must first join our channel:\n"
    f"➡️ {REQUIRED_CHANNEL}\n\n"
    "Tap *Join Channel* below, then tap *I've Joined*."
)

WELCOME_TEXT = (
    "✅ *You're in! Welcome to Auto Reactions Bot* 🤖\n\n"
    "━━━━━━━━━━━━━━━━━\n"
    "🚀 *How to get reactions on your posts:*\n\n"
    "1️⃣ Tap *Add My Channel* below\n"
    "2️⃣ Make this bot an *Admin* in your channel\n"
    "3️⃣ Post anything — reactions appear automatically ❤️🔥😍👍\n\n"
    "━━━━━━━━━━━━━━━━━\n"
    "👇 Get started:"
)


async def _send_photo_msg(bot, chat_id: int, text: str, keyboard: InlineKeyboardMarkup) -> None:
    """Send a photo+caption message, or plain text if image missing."""
    if WELCOME_IMAGE.exists():
        with WELCOME_IMAGE.open("rb") as f:
            await bot.send_photo(
                chat_id, photo=f,
                caption=text, parse_mode="Markdown",
                reply_markup=keyboard,
            )
    else:
        await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=keyboard)


async def _send_join_gate(bot, chat_id: int) -> None:
    await _send_photo_msg(bot, chat_id, JOIN_TEXT, _join_keyboard())


async def _send_welcome(bot, chat_id: int, is_admin: bool = False) -> None:
    await _send_photo_msg(bot, chat_id, WELCOME_TEXT, _home_keyboard(is_admin))


# ── /start ────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    msg = update.effective_message
    if not u or not msg:
        return

    subscribed = await _is_subscribed(ctx.bot, u.id)
    if subscribed is not True:
        # False = not joined, None = check failed (bot not admin of required channel)
        if subscribed is None:
            log.error("Subscription check failed uid=%s — is bot admin of %s?", u.id, REQUIRED_CHANNEL)
        await _send_join_gate(ctx.bot, u.id)
        return

    await _run(_save_user, u.id, u.first_name)
    await _send_welcome(ctx.bot, u.id, is_admin=u.id in SUPER_ADMIN_IDS)


# ── Callback: check join ──────────────────────────────────────────────
async def cb_check_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    u = update.effective_user
    if not query or not u:
        return
    msg = _msg(query)
    if not msg:
        return

    subscribed = await _is_subscribed(ctx.bot, u.id)
    if subscribed is not True:
        if subscribed is None:
            log.error("Subscription check failed uid=%s — bot not admin of %s?", u.id, REQUIRED_CHANNEL)
        await query.answer(
            "❌ You haven't joined yet!\nJoin the channel first then tap this button.",
            show_alert=True,
        )
        return

    await query.answer("✅ Verified! Welcome aboard.")
    await _run(_save_user, u.id, u.first_name)
    kb = _home_keyboard(is_admin=u.id in SUPER_ADMIN_IDS)

    # Swap the join gate caption/keyboard into the welcome screen in-place
    try:
        if msg.photo:
            await msg.edit_caption(caption=WELCOME_TEXT, parse_mode="Markdown", reply_markup=kb)
        else:
            await msg.edit_text(WELCOME_TEXT, parse_mode="Markdown", reply_markup=kb)
    except TelegramError:
        # Fallback: delete old message and send fresh welcome
        chat_id = msg.chat.id
        try:
            await msg.delete()
        except TelegramError:
            pass
        await _send_welcome(ctx.bot, chat_id, is_admin=u.id in SUPER_ADMIN_IDS)


# ── Callback: how it works ────────────────────────────────────────────
async def cb_how_it_works(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    msg = _msg(query)
    if not msg:
        return
    await query.answer()
    text = (
        "❓ *How It Works*\n\n"
        "Every new post in your channel gets reacted to automatically:\n"
        "👍 🔥 ❤️ 🎉 🥰 👏 🤩 ⚡ 💯 😍\n\n"
        "📋 *Requirements:*\n"
        "• Bot must be an *Admin* in your channel\n"
        "• Channel must be registered via *Add My Channel*\n\n"
        "💡 Works with public/private channels and supergroups."
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_home")]])
    if msg.photo:
        await msg.edit_caption(caption=text, parse_mode="Markdown", reply_markup=kb)
    else:
        await msg.edit_text(text, parse_mode="Markdown", reply_markup=kb)


# ── Callback: my channels ─────────────────────────────────────────────
async def cb_my_channels(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    u = update.effective_user
    if not query or not u:
        return
    msg = _msg(query)
    if not msg:
        return
    await query.answer()

    rows = await _run(_get_channels, u.id)
    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_home")]])

    if not rows:
        text = "📋 *Your Channels*\n\nNo channels added yet. Tap *Add My Channel* to get started."
        if msg.photo:
            await msg.edit_caption(caption=text, parse_mode="Markdown", reply_markup=back_kb)
        else:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=back_kb)
        return

    # Refresh live admin status for each channel and update DB
    lines = []
    for r in rows:
        try:
            member = await ctx.bot.get_chat_member(r["cid"], ctx.bot.id)
            status = member.status
            await _run(_save_channel, r["cid"], r["title"], r.get("username"), r["chat_type"], u.id, status)
        except TelegramError:
            status = r["status"]
        uname = f" @{r['username']}" if r.get("username") else ""
        icon = "✅" if status == "administrator" else "⚠️"
        lines.append(f"{icon} *{r['title']}*{uname}")

    text = "📋 *Your Channels:*\n\n" + "\n".join(lines) + "\n\n_✅ Active  ⚠️ Bot Should be Admin_"
    if msg.photo:
        await msg.edit_caption(caption=text, parse_mode="Markdown", reply_markup=back_kb)
    else:
        await msg.edit_text(text, parse_mode="Markdown", reply_markup=back_kb)


# ── Callback: back home ───────────────────────────────────────────────
async def cb_back_home(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    u = update.effective_user
    if not query:
        return
    msg = _msg(query)
    if not msg:
        return
    await query.answer()
    kb = _home_keyboard(is_admin=bool(u and u.id in SUPER_ADMIN_IDS))
    if msg.photo:
        await msg.edit_caption(caption=WELCOME_TEXT, parse_mode="Markdown", reply_markup=kb)
    else:
        await msg.edit_text(WELCOME_TEXT, parse_mode="Markdown", reply_markup=kb)


# ── Add channel flow ──────────────────────────────────────────────────
_ADD_PROMPT = (
    "📢 *Add Your Channel*\n\n"
    "Send me the channel's *numeric ID* or *forward any message* from your channel.\n\n"
    "📌 *How to get the ID:*\n"
    "Forward a message from your channel to @userinfobot — it'll show the ID.\n\n"
    "❌ Type /cancel to abort."
)


async def cb_add_channel_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    u = update.effective_user
    if not query or not u:
        return ConversationHandler.END
    msg = _msg(query)
    if not msg:
        return ConversationHandler.END

    subscribed = await _is_subscribed(ctx.bot, u.id)
    if subscribed is False:
        await query.answer("❌ Join our channel first!", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    if msg.photo:
        await msg.edit_caption(caption=_ADD_PROMPT, parse_mode="Markdown")
    else:
        await msg.edit_text(_ADD_PROMPT, parse_mode="Markdown")
    return WAITING_CHANNEL


async def cmd_add_channel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    u = update.effective_user
    msg = update.effective_message
    if not u or not msg:
        return ConversationHandler.END

    if await _is_subscribed(ctx.bot, u.id) is False:
        await msg.reply_text("⚠️ Join our channel first!", reply_markup=_join_keyboard())
        return ConversationHandler.END

    await msg.reply_text(_ADD_PROMPT, parse_mode="Markdown")
    return WAITING_CHANNEL


async def receive_channel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    u = update.effective_user
    if not msg or not u:
        return ConversationHandler.END

    chat_id: int | None = None
    title: str | None = None
    username: str | None = None
    chat_type = "channel"

    fwd = msg.forward_origin
    if fwd and hasattr(fwd, "chat"):
        ch = fwd.chat  # type: ignore[attr-defined]
        chat_id, title, username, chat_type = ch.id, ch.title or str(ch.id), ch.username, ch.type

    if chat_id is None and msg.text:
        try:
            chat_id = int(msg.text.strip())
        except ValueError:
            await msg.reply_text(
                "❌ Invalid ID. Send a number like `-1001234567890` or forward a message."
            )
            return WAITING_CHANNEL

    if chat_id is None:
        await msg.reply_text("❌ Could not read channel. Send a numeric ID or forward a message.")
        return WAITING_CHANNEL

    try:
        chat = await ctx.bot.get_chat(chat_id)
        title, username, chat_type = chat.title or str(chat_id), chat.username, chat.type
        status = (await ctx.bot.get_chat_member(chat_id, ctx.bot.id)).status
    except TelegramError as e:
        log.warning("get_chat(%s): %s", chat_id, e)
        title = title or str(chat_id)
        status = "unknown"

    await _run(_save_channel, chat_id, title, username, chat_type, u.id, status)
    _cache_username(chat_id, username)
    if status in ("administrator", "creator"):
        await _userbot_join(chat_id, username, main_bot=ctx.bot)

    uname_display = f" (@{username})" if username else ""

    if status not in ("administrator", "creator"):
        body = (
            f"✅ *Channel registered!*\n\n📢 *{title}*{uname_display}\n🆔 `{chat_id}`\n\n"
            "⚠️ *Bot is not admin yet!*\n"
            "Add the main bot as admin with *Post Messages* permission, then re-register."
        )
        await msg.reply_text(body, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Home", callback_data="back_home")],
            ]))
        return ConversationHandler.END

    # Main bot is admin — show userbot join status
    if _userbot_clients:
        worker_note = f"\n\n🤖 *{len(_userbot_clients)} userbot(s) are joining your channel automatically…*"
    else:
        worker_note = "\n\n⚠️ No userbots configured. Add `USERBOT_SESSIONS` to .env for extra reactions."

    await msg.reply_text(
        f"✅ *Channel registered!*\n\n📢 *{title}*{uname_display}\n🆔 `{chat_id}`{worker_note}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 My Channels", callback_data="my_channels")],
            [InlineKeyboardButton("🏠 Home", callback_data="back_home")],
        ]),
    )
    return ConversationHandler.END


async def cmd_cancel(update: Update, _: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_message:
        await update.effective_message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


# ── User commands ─────────────────────────────────────────────────────
async def cmd_mychannels(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    msg = update.effective_message
    if not u or not msg:
        return
    rows = await _run(_get_channels, u.id)
    if not rows:
        await msg.reply_text("No channels yet. Use /addchannel.")
        return
    lines = [
        f"{'✅' if r['status'] == 'administrator' else '⚠️'} {r['title']}"
        + (f" @{r['username']}" if r.get("username") else "")
        for r in rows
    ]
    await msg.reply_text("📋 *Your Channels:*\n\n" + "\n".join(lines), parse_mode="Markdown")


async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    await msg.reply_text(
        "📖 *Commands:*\n\n"
        "/start — Welcome screen\n"
        "/addchannel — Register a channel\n"
        "/mychannels — Your channels\n"
        "/cancel — Cancel current action",
        parse_mode="Markdown",
    )


# ── Super-admin commands (silent to non-admins) ───────────────────────
async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    msg = update.effective_message
    if not u or not msg or u.id not in SUPER_ADMIN_IDS:
        return
    uc = await _run(_count_users_all)
    await msg.reply_text(
        f"📊 *Stats*\n\n"
        f"👥 Total users: *{uc['total']}*\n"
        f"✅ Active: *{uc['active']}*\n"
        f"🚫 Blocked bot: *{uc['blocked']}*\n"
        f"📢 Channels: *{await _run(_count_channels)}*",
        parse_mode="Markdown",
    )


async def cmd_broadcast_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    msg = update.effective_message
    if not u or not msg or u.id not in SUPER_ADMIN_IDS:
        return

    reply = msg.reply_to_message
    text = " ".join(ctx.args or []).strip()

    if not reply and not text:
        await msg.reply_text(
            "Usage:\n"
            "• Reply to any message with `/broadcast` to forward it to all users\n"
            "• Or: `/broadcast Your text here`",
            parse_mode="Markdown",
        )
        return

    uids = await _run(_get_active_uids)
    ok = fail = 0
    status_msg = await msg.reply_text(f"Sending to {len(uids)} users…")

    for uid in uids:
        try:
            if reply:
                await reply.copy(uid)
            else:
                await ctx.bot.send_message(uid, f"📣 *Announcement*\n\n{text}", parse_mode="Markdown")
            ok += 1
        except Forbidden:
            await _run(_block_user, uid)
            fail += 1
        except TelegramError:
            fail += 1
        await asyncio.sleep(0.05)

    await status_msg.edit_text(f"✅ Done — Sent: {ok}, Failed: {fail}")


async def cmd_broadcast_channels(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    msg = update.effective_message
    if not u or not msg or u.id not in SUPER_ADMIN_IDS:
        return

    reply = msg.reply_to_message
    text = " ".join(ctx.args or []).strip()

    if not reply and not text:
        await msg.reply_text(
            "Usage:\n"
            "• Reply to any message with `/broadcastchannels` to forward it to all channels\n"
            "• Or: `/broadcastchannels Your text here`",
            parse_mode="Markdown",
        )
        return

    cids = await _run(_get_all_channel_ids)
    ok = fail = 0
    status_msg = await msg.reply_text(f"Broadcasting to {len(cids)} channels…")

    for cid in cids:
        try:
            if reply:
                await reply.copy(cid)
            else:
                await ctx.bot.send_message(cid, f"📣 *Announcement*\n\n{text}", parse_mode="Markdown")
            ok += 1
        except TelegramError as e:
            log.warning("Chan broadcast %s: %s", cid, e)
            fail += 1
        await asyncio.sleep(0.1)

    await status_msg.edit_text(f"✅ Done — Sent: {ok}, Failed: {fail}")


# ── Admin panel ───────────────────────────────────────────────────────
def _admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Userbot", callback_data="admin_add_bot")],
        [InlineKeyboardButton("📱 List Userbots", callback_data="admin_list_bots")],
        [InlineKeyboardButton("📊 Stats", callback_data="admin_stats")],
    ])


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    msg = update.effective_message
    if not u or not msg or u.id not in SUPER_ADMIN_IDS:
        return
    await msg.reply_text(
        f"🔧 *Admin Panel*\n\n🤖 Active userbots: *{len(_userbot_clients)}*",
        parse_mode="Markdown",
        reply_markup=_admin_keyboard(),
    )


async def cb_admin_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    u = update.effective_user
    if not query or not u or u.id not in SUPER_ADMIN_IDS:
        if query:
            await query.answer()
        return
    msg = _msg(query)
    if not msg:
        return
    await query.answer()
    text = f"🔧 *Admin Panel*\n\n🤖 Active userbots: *{len(_userbot_clients)}*"
    try:
        if msg.photo:
            await msg.edit_caption(caption=text, parse_mode="Markdown", reply_markup=_admin_keyboard())
        else:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=_admin_keyboard())
    except TelegramError:
        await msg.reply_text(text, parse_mode="Markdown", reply_markup=_admin_keyboard())


async def cb_admin_list_bots(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    u = update.effective_user
    if not query or not u or u.id not in SUPER_ADMIN_IDS:
        if query:
            await query.answer()
        return
    msg = _msg(query)
    if not msg:
        return
    await query.answer()
    rows = await _run(_get_db_userbot_sessions)
    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin_panel")]])
    if not rows:
        text = "📱 *Userbots*\n\nNo userbots added via bot yet.\n\n_(Sessions from .env are not listed here)_"
    else:
        lines = [f"• `{r['phone']}`" for r in rows]
        text = f"📱 *Userbots ({len(rows)} in DB, {len(_userbot_clients)} active)*\n\n" + "\n".join(lines)
    try:
        if msg.photo:
            await msg.edit_caption(caption=text, parse_mode="Markdown", reply_markup=back_kb)
        else:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=back_kb)
    except TelegramError:
        await msg.reply_text(text, parse_mode="Markdown", reply_markup=back_kb)


async def cb_admin_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    u = update.effective_user
    if not query or not u or u.id not in SUPER_ADMIN_IDS:
        if query:
            await query.answer()
        return
    msg = _msg(query)
    if not msg:
        return
    await query.answer()
    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin_panel")]])
    uc = await _run(_count_users_all)
    text = (
        f"📊 *Stats*\n\n"
        f"👥 Total users: *{uc['total']}*\n"
        f"✅ Active: *{uc['active']}*\n"
        f"🚫 Blocked bot: *{uc['blocked']}*\n"
        f"📢 Channels: *{await _run(_count_channels)}*\n"
        f"🤖 Active userbots: *{len(_userbot_clients)}*"
    )
    try:
        if msg.photo:
            await msg.edit_caption(caption=text, parse_mode="Markdown", reply_markup=back_kb)
        else:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=back_kb)
    except TelegramError:
        await msg.reply_text(text, parse_mode="Markdown", reply_markup=back_kb)


# ── Add-userbot conversation ──────────────────────────────────────────
async def cb_admin_add_bot_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    u = update.effective_user
    if not query or not u or u.id not in SUPER_ADMIN_IDS:
        if query:
            await query.answer()
        return ConversationHandler.END
    await query.answer()
    await query.message.reply_text(  # type: ignore[union-attr]
        "📱 *Add Userbot*\n\n"
        "Send the phone number with country code:\n`+91xxxxxxxxxx`\n\n"
        "/cancel to abort.",
        parse_mode="Markdown",
    )
    return WAITING_PHONE


async def receive_bot_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    u = update.effective_user
    if not msg or not u or u.id not in SUPER_ADMIN_IDS:
        return ConversationHandler.END
    phone = (msg.text or "").strip()
    if not phone.startswith("+") or len(phone) < 8:
        await msg.reply_text(
            "❌ Invalid. Use format `+CountryCodeNumber` e.g. `+91xxxxxxxxxx`\n/cancel to abort.",
            parse_mode="Markdown",
        )
        return WAITING_PHONE
    if not _API_ID or not _API_HASH:
        await msg.reply_text("❌ API_ID / API_HASH not set in .env — cannot start userbot.")
        return ConversationHandler.END
    status = await msg.reply_text("⏳ Sending OTP…")
    try:
        from pyrogram import Client as _PyroClient  # type: ignore
        client = _PyroClient(":memory:", api_id=_API_ID, api_hash=_API_HASH)
        await client.connect()
        sent = await client.send_code(phone)
        _pending_add[u.id] = {"client": client, "phone": phone, "sent": sent}
        await status.edit_text("✅ OTP sent!\n\nEnter the Telegram code you received:\n\n/cancel to abort.")
        return WAITING_CODE
    except Exception as e:
        log.error("send_code %s: %s", phone, e)
        await status.edit_text(f"❌ Failed to send OTP: `{e}`", parse_mode="Markdown")
        return ConversationHandler.END


async def receive_bot_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    u = update.effective_user
    if not msg or not u or u.id not in SUPER_ADMIN_IDS:
        return ConversationHandler.END
    pending = _pending_add.get(u.id)
    if not pending:
        await msg.reply_text("❌ Session expired. Start again from /admin.")
        return ConversationHandler.END
    code = (msg.text or "").strip()
    client = pending["client"]
    phone = pending["phone"]
    sent = pending["sent"]
    try:
        await client.sign_in(phone, sent.phone_code_hash, code)
    except Exception as e:
        err = str(e)
        if "password" in err.lower() or "SessionPasswordNeeded" in str(type(e)):
            await msg.reply_text("🔐 Two-step verification required.\nEnter your 2FA password:\n\n/cancel to abort.")
            return WAITING_2FA
        log.error("sign_in %s: %s", phone, e)
        await msg.reply_text(f"❌ Sign-in failed: `{e}`\n/cancel to abort.", parse_mode="Markdown")
        return ConversationHandler.END
    return await _finish_add_userbot(u.id, msg)


async def receive_bot_2fa(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    u = update.effective_user
    if not msg or not u or u.id not in SUPER_ADMIN_IDS:
        return ConversationHandler.END
    pending = _pending_add.get(u.id)
    if not pending:
        await msg.reply_text("❌ Session expired. Start again from /admin.")
        return ConversationHandler.END
    password = (msg.text or "").strip()
    try:
        await pending["client"].check_password(password)
    except Exception as e:
        log.error("check_password: %s", e)
        await msg.reply_text(f"❌ Wrong password: `{e}`\nTry again or /cancel.", parse_mode="Markdown")
        return WAITING_2FA
    return await _finish_add_userbot(u.id, msg)


async def _finish_add_userbot(admin_uid: int, msg) -> int:
    pending = _pending_add.pop(admin_uid, None)
    if not pending:
        return ConversationHandler.END
    client = pending["client"]
    phone = pending["phone"]
    try:
        session_string = await client.export_session_string()
        me = await client.get_me()
        await client.disconnect()
        await _run(_save_userbot_session, phone, session_string)
        from pyrogram import Client as _PyroClient  # type: ignore
        idx = len(_userbot_clients)
        new_client = _PyroClient(
            name=f":userbot_{idx}:",
            api_id=_API_ID,
            api_hash=_API_HASH,
            session_string=session_string,
        )
        await new_client.start()
        _userbot_clients.append(new_client)
        log.info("New userbot %d added: @%s (%s)", idx, me.username or me.id, me.id)

        # Join new userbot into every registered channel immediately
        main_bot = _app.bot if _app else None  # type: ignore[union-attr]
        for cid, uname in await asyncio.to_thread(_get_all_channels_brief):
            _cache_username(cid, uname)
            try:
                join_target: str = uname.lstrip("@") if uname else ""
                if not join_target and main_bot:
                    inv = await main_bot.create_chat_invite_link(cid)
                    join_target = inv.invite_link
                if join_target:
                    await new_client.join_chat(join_target)
                    log.info("New userbot %d joined %s", idx, join_target)
            except Exception as join_err:
                err = str(join_err)
                if "USER_ALREADY_PARTICIPANT" not in err and "already" not in err.lower():
                    log.warning("New userbot %d join %s: %s", idx, cid, join_err)

        await msg.reply_text(
            f"✅ *Userbot added!*\n\n"
            f"📱 Phone: `{phone}`\n"
            f"👤 Account: @{me.username or me.id}\n"
            f"🤖 Total active userbots: *{len(_userbot_clients)}*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔧 Admin Panel", callback_data="admin_panel"),
            ]]),
        )
    except Exception as e:
        log.error("finish_add_userbot: %s", e)
        await msg.reply_text(f"❌ Failed to activate userbot: `{e}`", parse_mode="Markdown")
    return ConversationHandler.END


async def cmd_cancel_bot_add(update: Update, _: ContextTypes.DEFAULT_TYPE) -> int:
    u = update.effective_user
    if u:
        pending = _pending_add.pop(u.id, None)
        if pending:
            try:
                await pending["client"].disconnect()
            except Exception:
                pass
    if update.effective_message:
        await update.effective_message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


# ── Channel membership events ─────────────────────────────────────────
async def on_membership(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    m = update.my_chat_member
    if not m or m.chat.type not in (ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP):
        return
    ch = m.chat
    actor = m.from_user
    new_status = m.new_chat_member.status
    await _run(
        _save_channel,
        ch.id, ch.title or str(ch.id), ch.username, ch.type,
        actor.id if actor else None,
        new_status,
    )
    log.info("%s %s (%s) → %s", ch.type, ch.title, ch.id, new_status)
    _cache_username(ch.id, ch.username)
    if new_status == "administrator":
        await _userbot_join(ch.id, ch.username, main_bot=ctx.bot)


# ── Auto-react ────────────────────────────────────────────────────────
async def on_channel_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.channel_post
    if msg:
        _cache_username(msg.chat_id, msg.chat.username)
        await _react(ctx, msg.chat_id, msg.message_id)


async def on_group_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg:
        await _react(ctx, msg.chat_id, msg.message_id)


async def _userbot_react_and_view(client, idx: int, emoji: str,
                                  chat_id: int, message_id: int, delay: float) -> None:
    await asyncio.sleep(delay)
    target = f"@{_channel_usernames[chat_id]}" if chat_id in _channel_usernames else chat_id
    from pyrogram.raw import functions, types as rt  # type: ignore
    try:
        peer = await client.resolve_peer(target)
    except Exception as e:
        log.warning("Userbot %d resolve_peer %s: %s", idx, target, e)
        return

    try:
        await client.invoke(
            functions.messages.SendReaction(
                peer=peer, msg_id=message_id,
                reaction=[rt.ReactionEmoji(emoticon=emoji)],
            )
        )
    except Exception as e:
        err = str(e)
        if "REACTION_INVALID" in err:
            log.debug("Userbot %d: emoji %s not allowed in %s", idx, emoji, chat_id)
        elif "MESSAGE_NOT_MODIFIED" not in err:
            log.warning("Userbot %d react %s/%s: %s", idx, chat_id, message_id, e)

    try:
        await client.invoke(
            functions.messages.GetMessagesViews(  # type: ignore[attr-defined]
                peer=peer, id=[message_id], increment=True,
            )
        )
    except Exception as e:
        log.debug("Userbot %d view %s/%s: %s", idx, chat_id, message_id, e)


async def _react(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    emojis = list(REACTIONS)
    tasks = []

    # Main bot takes slot 0
    async def _main_react(e: str = emojis[0]) -> None:
        try:
            await ctx.bot.set_message_reaction(
                chat_id=chat_id, message_id=message_id,
                reaction=[ReactionTypeEmoji(emoji=e)],
            )
        except TelegramError as err:
            log.warning("Main react failed %s/%s: %s", chat_id, message_id, err)

    tasks.append(_main_react())

    # Each userbot gets a unique emoji with a small staggered delay
    for i, client in enumerate(_userbot_clients, start=1):
        emoji = emojis[i % len(emojis)]
        tasks.append(_userbot_react_and_view(client, i, emoji, chat_id, message_id, delay=i * 0.5))

    await asyncio.gather(*tasks, return_exceptions=True)


def _cache_username(chat_id: int, username: str | None) -> None:
    if username:
        _channel_usernames[chat_id] = username.lstrip("@")


async def _userbot_join(chat_id: int, username: str | None, main_bot=None) -> None:
    """Join all userbots into the channel. Public = @username, private = invite link."""
    if not _userbot_clients:
        return
    if username:
        target: str = username.lstrip("@")
    elif main_bot:
        try:
            inv = await main_bot.create_chat_invite_link(chat_id)
            target = inv.invite_link
        except TelegramError as e:
            log.warning("Cannot create invite link for %s: %s", chat_id, e)
            return
    else:
        log.warning("Skipping private channel %s — no username", chat_id)
        return

    for i, client in enumerate(_userbot_clients):
        try:
            await client.join_chat(target)
            log.info("Userbot %d joined %s", i, target)
        except Exception as e:
            err = str(e)
            if "USER_ALREADY_PARTICIPANT" in err or "already" in err.lower():
                log.debug("Userbot %d already in %s", i, target)
            else:
                log.warning("Userbot %d join %s: %s", i, target, e)


# ── Entry point ───────────────────────────────────────────────────────
async def _post_init(app: Application) -> None:
    global _app
    _app = app
    # Collect all session strings: .env first, then DB
    if _API_ID and _API_HASH:
        from pyrogram import Client as _PyroClient  # type: ignore
        all_sessions: list[str] = list(_USERBOT_SESSIONS)
        for row in await asyncio.to_thread(_get_db_userbot_sessions):
            if row["session_string"] not in all_sessions:
                all_sessions.append(row["session_string"])
        for i, sess in enumerate(all_sessions):
            try:
                client = _PyroClient(
                    name=f":userbot_{i}:",
                    api_id=_API_ID,
                    api_hash=_API_HASH,
                    session_string=sess,
                )
                await client.start()
                me = await client.get_me()
                _userbot_clients.append(client)
                log.info("Userbot %d ready: @%s (%s)", i, me.username or me.id, me.id)
            except Exception as e:
                log.error("Userbot %d init failed: %s", i, e)

    if not _userbot_clients:
        log.warning("No userbots active — only main bot will react")

    # Cache usernames and auto-join userbots into all existing channels
    for cid, uname in await asyncio.to_thread(_get_all_channels_brief):
        _cache_username(cid, uname)
        await _userbot_join(cid, uname, main_bot=app.bot)

    await app.bot.set_my_commands([
        BotCommand("start", "Welcome screen"),
        BotCommand("addchannel", "Register a channel"),
        BotCommand("mychannels", "Your channels"),
        BotCommand("help", "Help & commands"),
    ])


def main() -> None:
    _db()
    from telegram.request import HTTPXRequest

    request = HTTPXRequest(
        connection_pool_size = 8,
        connect_timeout      = 60,
        read_timeout         = 60,
        write_timeout        = 60,
        pool_timeout         = 60,
        http_version         = "1.1",
    )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .job_queue(None)
        .post_init(_post_init)
        .build()
    )

    add_channel_conv = ConversationHandler(
        entry_points=[
            CommandHandler("addchannel", cmd_add_channel),
            CallbackQueryHandler(cb_add_channel_start, pattern="^add_channel$"),
        ],
        states={
            WAITING_CHANNEL: [
                MessageHandler(filters.TEXT | filters.FORWARDED, receive_channel),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_message=False,
    )

    add_userbot_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_admin_add_bot_start, pattern="^admin_add_bot$")],
        states={
            WAITING_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_bot_phone)],
            WAITING_CODE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_bot_code)],
            WAITING_2FA:   [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_bot_2fa)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel_bot_add)],
        per_message=False,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("mychannels", cmd_mychannels))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast_users))
    app.add_handler(CommandHandler("broadcastchannels", cmd_broadcast_channels))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(add_channel_conv)
    app.add_handler(add_userbot_conv)

    app.add_handler(CallbackQueryHandler(cb_check_join, pattern="^check_join$"))
    app.add_handler(CallbackQueryHandler(cb_how_it_works, pattern="^how_it_works$"))
    app.add_handler(CallbackQueryHandler(cb_my_channels, pattern="^my_channels$"))
    app.add_handler(CallbackQueryHandler(cb_back_home, pattern="^back_home$"))
    app.add_handler(CallbackQueryHandler(cb_admin_panel, pattern="^admin_panel$"))
    app.add_handler(CallbackQueryHandler(cb_admin_list_bots, pattern="^admin_list_bots$"))
    app.add_handler(CallbackQueryHandler(cb_admin_stats, pattern="^admin_stats$"))

    app.add_handler(ChatMemberHandler(on_membership, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, on_channel_post))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, on_group_message))

    app.run_polling(
        allowed_updates=[
            "message",
            "channel_post",
            "my_chat_member",
            "callback_query",
        ],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
