"""Auto Reaction Bot."""
from __future__ import annotations

import asyncio
import logging
import os
import random
import sqlite3
from pathlib import Path

from telegram import (
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

WAITING_CHANNEL = 1

# ── SQLite ────────────────────────────────────────────────────────────
_conn: sqlite3.Connection | None = None
_lock = asyncio.Lock()


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect("bot.db", check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users(
                uid INTEGER PRIMARY KEY, name TEXT, active INT DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS channels(
                cid INTEGER PRIMARY KEY, title TEXT, username TEXT,
                chat_type TEXT DEFAULT 'channel', owner INT, status TEXT
            );
        """)
        # Add chat_type column to existing databases that predate it
        cols = {r[1] for r in _conn.execute("PRAGMA table_info(channels)").fetchall()}
        if "chat_type" not in cols:
            _conn.execute("ALTER TABLE channels ADD COLUMN chat_type TEXT DEFAULT 'channel'")
            _conn.commit()
    return _conn


def _save_user(uid: int, name: str) -> None:
    c = _db()
    c.execute(
        "INSERT INTO users(uid,name,active) VALUES(?,?,1) "
        "ON CONFLICT(uid) DO UPDATE SET name=excluded.name, active=1",
        (uid, name),
    )
    c.commit()


def _deactivate_user(uid: int) -> None:
    c = _db()
    c.execute("UPDATE users SET active=0 WHERE uid=?", (uid,))
    c.commit()


def _get_active_uids() -> list[int]:
    return [r[0] for r in _db().execute("SELECT uid FROM users WHERE active=1").fetchall()]


def _count_users() -> int:
    return _db().execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]


def _save_channel(cid: int, title: str, username: str | None,
                  chat_type: str, owner: int | None, status: str) -> None:
    c = _db()
    c.execute(
        "INSERT INTO channels(cid,title,username,chat_type,owner,status) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(cid) DO UPDATE SET title=excluded.title, username=excluded.username, "
        "chat_type=excluded.chat_type, owner=COALESCE(excluded.owner,channels.owner), status=excluded.status",
        (cid, title, username, chat_type, owner, status),
    )
    c.commit()


def _get_channels(owner: int | None = None) -> list[dict]:
    if owner is not None:
        rows = _db().execute(
            "SELECT cid,title,username,chat_type,status FROM channels WHERE owner=?", (owner,)
        )
    else:
        rows = _db().execute("SELECT cid,title,username,chat_type,status FROM channels")
    return [dict(r) for r in rows.fetchall()]


def _count_channels() -> int:
    return _db().execute("SELECT COUNT(*) FROM channels").fetchone()[0]


def _get_all_channel_ids() -> list[int]:
    return [r[0] for r in _db().execute("SELECT cid FROM channels").fetchall()]


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


def _home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add My Channel", callback_data="add_channel")],
        [InlineKeyboardButton("📋 My Channels", callback_data="my_channels")],
        [InlineKeyboardButton("❓ How It Works", callback_data="how_it_works")],
    ])


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


async def _send_welcome(bot, chat_id: int) -> None:
    await _send_photo_msg(bot, chat_id, WELCOME_TEXT, _home_keyboard())


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
    await _send_welcome(ctx.bot, u.id)


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

    # Swap the join gate caption/keyboard into the welcome screen in-place
    try:
        if msg.photo:
            await msg.edit_caption(caption=WELCOME_TEXT, parse_mode="Markdown", reply_markup=_home_keyboard())
        else:
            await msg.edit_text(WELCOME_TEXT, parse_mode="Markdown", reply_markup=_home_keyboard())
    except TelegramError:
        # Fallback: delete old message and send fresh welcome
        chat_id = msg.chat.id
        try:
            await msg.delete()
        except TelegramError:
            pass
        await _send_welcome(ctx.bot, chat_id)


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
    if not query:
        return
    msg = _msg(query)
    if not msg:
        return
    await query.answer()
    if msg.photo:
        await msg.edit_caption(caption=WELCOME_TEXT, parse_mode="Markdown", reply_markup=_home_keyboard())
    else:
        await msg.edit_text(WELCOME_TEXT, parse_mode="Markdown", reply_markup=_home_keyboard())


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

    uname_display = f" (@{username})" if username else ""
    warn = (
        "\n\n⚠️ *Bot is not admin yet!* Add it as admin with Post Messages permission."
        if status not in ("administrator", "creator") else ""
    )

    await msg.reply_text(
        f"✅ *Channel registered!*\n\n📢 *{title}*{uname_display}\n🆔 `{chat_id}`{warn}",
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
    await msg.reply_text(
        f"📊 *Stats*\n\n"
        f"👥 Users: *{await _run(_count_users)}*\n"
        f"📢 Channels: *{await _run(_count_channels)}*",
        parse_mode="Markdown",
    )


async def cmd_broadcast_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    msg = update.effective_message
    if not u or not msg or u.id not in SUPER_ADMIN_IDS:
        return
    text = " ".join(ctx.args or []).strip()
    if not text:
        await msg.reply_text("Usage: `/broadcast Your message`", parse_mode="Markdown")
        return
    uids = await _run(_get_active_uids)
    ok = fail = 0
    status_msg = await msg.reply_text(f"Sending to {len(uids)} users…")
    for uid in uids:
        try:
            await ctx.bot.send_message(uid, f"📣 *Announcement*\n\n{text}", parse_mode="Markdown")
            ok += 1
        except Forbidden:
            await _run(_deactivate_user, uid)
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
    text = " ".join(ctx.args or []).strip()
    if not text:
        await msg.reply_text("Usage: `/broadcastchannels Your message`", parse_mode="Markdown")
        return
    cids = await _run(_get_all_channel_ids)
    ok = fail = 0
    status_msg = await msg.reply_text(f"Broadcasting to {len(cids)} channels…")
    for cid in cids:
        try:
            await ctx.bot.send_message(cid, f"📣 *Announcement*\n\n{text}", parse_mode="Markdown")
            ok += 1
        except TelegramError as e:
            log.warning("Chan broadcast %s: %s", cid, e)
            fail += 1
        await asyncio.sleep(0.1)
    await status_msg.edit_text(f"✅ Done — Sent: {ok}, Failed: {fail}")


# ── Channel membership events ─────────────────────────────────────────
async def on_membership(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    m = update.my_chat_member
    if not m or m.chat.type not in (ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP):
        return
    ch = m.chat
    actor = m.from_user
    await _run(
        _save_channel,
        ch.id, ch.title or str(ch.id), ch.username, ch.type,
        actor.id if actor else None,
        m.new_chat_member.status,
    )
    log.info("%s %s (%s) → %s", ch.type, ch.title, ch.id, m.new_chat_member.status)


# ── Auto-react ────────────────────────────────────────────────────────
async def on_channel_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.channel_post
    if msg:
        await _react(ctx, msg.chat_id, msg.message_id)


async def on_group_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg:
        await _react(ctx, msg.chat_id, msg.message_id)


async def _react(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    # Telegram bots are limited to 1 reaction per message
    emoji = random.choice(REACTIONS)
    try:
        await ctx.bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
    except TelegramError as e:
        log.warning("React failed %s/%s: %s", chat_id, message_id, e)


# ── Entry point ───────────────────────────────────────────────────────
async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start", "Welcome screen"),
        BotCommand("addchannel", "Register a channel"),
        BotCommand("mychannels", "Your channels"),
        BotCommand("help", "Help & commands"),
    ])


def main() -> None:
    _db()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
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

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("mychannels", cmd_mychannels))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast_users))
    app.add_handler(CommandHandler("broadcastchannels", cmd_broadcast_channels))
    app.add_handler(add_channel_conv)

    app.add_handler(CallbackQueryHandler(cb_check_join, pattern="^check_join$"))
    app.add_handler(CallbackQueryHandler(cb_how_it_works, pattern="^how_it_works$"))
    app.add_handler(CallbackQueryHandler(cb_my_channels, pattern="^my_channels$"))
    app.add_handler(CallbackQueryHandler(cb_back_home, pattern="^back_home$"))

    app.add_handler(ChatMemberHandler(on_membership, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, on_channel_post))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, on_group_message))

    app.run_polling(
        allowed_updates=["message", "channel_post", "my_chat_member", "callback_query"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
