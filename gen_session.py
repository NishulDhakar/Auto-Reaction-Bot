"""
Run this once per userbot account to generate a Pyrogram session string.
Paste each string into USERBOT_SESSIONS in your .env (comma-separated).

Usage:
    python gen_session.py
"""
import asyncio
import os
from pathlib import Path

# Load .env so API_ID / API_HASH are available if already set
_env = Path(".env")
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

api_id_env = os.environ.get("API_ID", "")
api_hash_env = os.environ.get("API_HASH", "")


async def main() -> None:
    api_id = int(input(f"API ID [{api_id_env or 'get from my.telegram.org'}]: ").strip() or api_id_env)
    api_hash = input(f"API Hash [{api_hash_env or 'get from my.telegram.org'}]: ").strip() or api_hash_env

    from pyrogram import Client  # type: ignore

    # Use ":memory:" so no session file is written — only the string matters
    client = Client(":memory:", api_id=api_id, api_hash=api_hash)
    await client.connect()

    phone = input("Phone number (with country code, e.g. +91xxxxxxxxxx): ").strip()
    sent = await client.send_code(phone)

    code = input("Enter the Telegram code you received: ").strip()
    try:
        await client.sign_in(phone, sent.phone_code_hash, code)
    except Exception as e:
        if "password" in str(e).lower() or "SessionPasswordNeeded" in str(type(e)):
            password = input("Two-step verification password: ").strip()
            await client.check_password(password)
        else:
            raise

    session_string = await client.export_session_string()
    await client.disconnect()

    print("\n" + "=" * 60)
    print("SESSION STRING (copy this into your .env):")
    print("=" * 60)
    print(session_string)
    print("=" * 60)
    print("\nAdd to .env:")
    print(f"USERBOT_SESSIONS={session_string}")
    print("\nFor multiple accounts, comma-separate the strings:")
    print("USERBOT_SESSIONS=string1,string2,string3")


if __name__ == "__main__":
    asyncio.run(main())
