"""Reaction Bot — lightweight single-file Telegram bot."""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from pathlib import Path

from telegram import BotCommand, ReactionTypeEmoji, Update
from telegram.constants import ChatType
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ── Inline .env loader (no python-dotenv needed) ────────────────────
_env_file = Path(".env")
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# ── Config ──────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = frozenset(
    int(x) for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip()
)
REACTIONS = tuple(
    x.strip()
    for x in os.environ.get(
        "REACTION_POOL", "👍,🔥,❤,🎉,🥰,👏,🤩,⚡,💯,😍"
    ).split(",")
    if x.strip()
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

# ── SQLite (2 tiny tables, no ORM) ─────────────────────────────────
_conn: sqlite3.Connection | None = None
_lock = asyncio.Lock()


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect("bot.db", check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users(
                uid INTEGER PRIMARY KEY, name TEXT, active INT DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS channels(
                cid INTEGER PRIMARY KEY, title TEXT, username TEXT,
                chat_type TEXT DEFAULT 'channel', owner INT, status TEXT
            );
            """
        )
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


def _save_channel(cid: int, title: str, username: str | None, chat_type: str, owner: int | None, status: str) -> None:
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
        rows = _db().execute("SELECT title,username,chat_type,status FROM channels WHERE owner=?", (owner,))
    else:
        rows = _db().execute("SELECT title,username,chat_type,status FROM channels")
    return [dict(r) for r in rows.fetchall()]


async def _run(fn, *args):
    async with _lock:
        return await asyncio.to_thread(fn, *args)


# ── Command handlers ───────────────────────────────────────────────
async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u or not update.effective_message:
        return
    await _run(_save_user, u.id, u.first_name)
    await update.effective_message.reply_text(
        f"✅ Registered! Add me to a channel as admin — I'll react to every post."
    )


async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            "/start – Register\n"
            "/help – Commands\n"
            "/mychannels – Your channels\n"
            "/allchannels – All channels (admin)\n"
            "/broadcasttoall <msg> – Broadcast (admin)"
        )


async def cmd_mychannels(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u or not update.effective_message:
        return
    rows = await _run(_get_channels, u.id)
    if not rows:
        await update.effective_message.reply_text("No channels/groups yet.")
        return
    lines = [
        f"• {r['title']}" + (f" @{r['username']}" if r.get("username") else "")
        + f" ({r.get('chat_type', 'channel')})"
        for r in rows
    ]
    await update.effective_message.reply_text("\n".join(lines))


async def cmd_allchannels(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u or not update.effective_message:
        return
    if u.id not in ADMIN_IDS:
        await update.effective_message.reply_text("Admin only.")
        return
    rows = await _run(_get_channels)
    if not rows:
        await update.effective_message.reply_text("No channels/groups yet.")
        return
    lines = [
        f"• {r['title']}" + (f" @{r['username']}" if r.get("username") else "")
        + f" ({r.get('chat_type', 'channel')}) [{r['status']}]"
        for r in rows
    ]
    await update.effective_message.reply_text("\n".join(lines))


async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u or not update.effective_message:
        return
    if u.id not in ADMIN_IDS:
        await update.effective_message.reply_text("Admin only.")
        return
    text = " ".join(ctx.args or []).strip()
    if not text:
        await update.effective_message.reply_text("Usage: /broadcasttoall <message>")
        return
    uids = await _run(_get_active_uids)
    ok = fail = 0
    for uid in uids:
        try:
            await ctx.bot.send_message(uid, text)
            ok += 1
        except Forbidden:
            fail += 1
            await _run(_deactivate_user, uid)
        except TelegramError:
            fail += 1
        await asyncio.sleep(0.05)
    await update.effective_message.reply_text(f"Sent: {ok} | Failed: {fail}")


# ── Channel events ─────────────────────────────────────────────────
async def on_membership(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    m = update.my_chat_member
    if not m or m.chat.type not in (ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP):
        return
    ch = m.chat
    actor = m.from_user
    await _run(
        _save_channel,
        ch.id,
        ch.title or str(ch.id),
        ch.username,
        ch.type,
        actor.id if actor else None,
        m.new_chat_member.status,
    )
    log.info("%s %s (%s) → %s", ch.type, ch.title, ch.id, m.new_chat_member.status)


async def on_channel_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.channel_post
    if not msg:
        return
    await _react(ctx, msg.chat_id, msg.message_id)


async def on_group_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg:
        return
    await _react(ctx, msg.chat_id, msg.message_id)


async def _react(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    emoji = REACTIONS[(message_id - 1) % len(REACTIONS)]
    try:
        await ctx.bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
    except TelegramError as e:
        log.warning("React failed %s/%s: %s", chat_id, message_id, e)


# ── Entry point ────────────────────────────────────────────────────
async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start", "Register"),
        BotCommand("help", "Commands"),
        BotCommand("mychannels", "Your channels"),
        BotCommand("allchannels", "All channels (admin)"),
        BotCommand("broadcasttoall", "Broadcast (admin)"),
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
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("mychannels", cmd_mychannels))
    app.add_handler(CommandHandler("allchannels", cmd_allchannels))
    app.add_handler(CommandHandler("broadcasttoall", cmd_broadcast))
    app.add_handler(ChatMemberHandler(on_membership, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, on_channel_post))
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & ~filters.COMMAND, on_group_message
    ))
    app.run_polling(
        allowed_updates=["message", "channel_post", "my_chat_member"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
