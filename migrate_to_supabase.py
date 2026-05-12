"""One-time migration: bot.db (SQLite) → Supabase (PostgreSQL)."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values

# ── load .env ─────────────────────────────────────────────────────────
_env_file = Path(".env")
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    raise SystemExit("❌ DATABASE_URL not set in .env")

# ── connect ────────────────────────────────────────────────────────────
print("Connecting to Supabase...")
pg = psycopg2.connect(DATABASE_URL)
pg.autocommit = False
cur = pg.cursor()

print("Opening bot.db...")
sq = sqlite3.connect("bot.db")
sq.row_factory = sqlite3.Row

# ── create tables in Supabase ──────────────────────────────────────────
print("Creating tables...")
cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        uid BIGINT PRIMARY KEY,
        name TEXT,
        status TEXT DEFAULT 'active'
    );
""")
cur.execute("""
    CREATE TABLE IF NOT EXISTS channels (
        cid BIGINT PRIMARY KEY,
        title TEXT,
        username TEXT,
        chat_type TEXT DEFAULT 'channel',
        owner BIGINT,
        status TEXT
    );
""")
pg.commit()
print("✅ Tables ready")

# ── migrate users ──────────────────────────────────────────────────────
users = sq.execute("SELECT uid, name, active FROM users").fetchall()
if users:
    rows = []
    for u in users:
        status = "active" if u["active"] == 1 else "blocked"
        rows.append((u["uid"], u["name"], status))
    execute_values(cur, """
        INSERT INTO users (uid, name, status) VALUES %s
        ON CONFLICT (uid) DO UPDATE SET name = EXCLUDED.name, status = EXCLUDED.status
    """, rows)
    pg.commit()
    print(f"✅ Migrated {len(rows)} users")
else:
    print("No users found in bot.db")

# ── migrate channels ───────────────────────────────────────────────────
channels = sq.execute("SELECT cid, title, username, chat_type, owner, status FROM channels").fetchall()
if channels:
    rows = [(c["cid"], c["title"], c["username"], c["chat_type"], c["owner"], c["status"]) for c in channels]
    execute_values(cur, """
        INSERT INTO channels (cid, title, username, chat_type, owner, status) VALUES %s
        ON CONFLICT (cid) DO UPDATE SET
            title = EXCLUDED.title,
            username = EXCLUDED.username,
            chat_type = EXCLUDED.chat_type,
            owner = EXCLUDED.owner,
            status = EXCLUDED.status
    """, rows)
    pg.commit()
    print(f"✅ Migrated {len(rows)} channels")
else:
    print("No channels found in bot.db")

# ── done ───────────────────────────────────────────────────────────────
cur.close()
pg.close()
sq.close()
print("\n🎉 Migration complete! All data is now in Supabase.")
