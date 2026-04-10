# reaction-bot

Production-oriented Telegram bot in Python using `python-telegram-bot` v20+.

## What it does

- Saves users on `/start`
- Lists help on `/help`
- Lists the current user's channels on `/mychannels`
- Lists all known channels on `/allchannels` for admins
- Broadcasts to all registered users on `/broadcasttoall` for admins
- Watches new channel posts and applies reactions from a pool of 10 different emoji

## Important Telegram limitation

Telegram currently allows a bot to keep only one selected reaction per message. This project therefore uses a pool of 10 reactions and rotates one reaction onto each new channel post.

## Project structure

```text
bot.py
handlers/
  start.py
  help.py
  channels.py
  channel_posts.py
  broadcast.py
jobs/
  maintenance.py
utils/
  db.py
  guards.py
  formatters.py
  logging.py
  reactions.py
config.py
requirements.txt
```

## Setup

1. Create and activate a virtual environment.

```bash
python3 -m venv .venv
source .venv/bin/activate
```

2. Install dependencies.

```bash
pip install -r requirements.txt
```

3. Create your environment file.

```bash
cp .env.example .env
```

4. Edit `.env`.

Required values:

- `BOT_TOKEN`: token from BotFather
- `ADMIN_USER_IDS`: comma-separated Telegram user IDs allowed to use admin commands

Optional values:

- `DB_PATH`: SQLite file path
- `LOG_LEVEL`: defaults to `INFO`
- `CLEANUP_DAYS`: how long to keep reaction attempt logs
- `REACTION_POOL`: comma-separated emoji pool

5. Run the bot.

```bash
python bot.py
```

## BotFather checklist

1. Create the bot with BotFather.
2. Disable privacy mode only if you later expand the bot to require non-command group messages. This bot does not require that for channel posts.
3. Add the bot to your channel as an administrator.

## Required channel permissions

The bot must be an administrator in the channel to reliably receive `channel_post` updates and react to posts.

## How channel tracking works

The bot stores channel metadata when Telegram sends a `my_chat_member` update for the bot inside a channel. That normally happens when the bot is added, promoted, demoted, or removed.

## Broadcast behavior

`/broadcasttoall` sends a message to all active registered private-chat users. If a user blocked the bot, that user is marked inactive automatically.

## Running in production

- Run the bot in a supervised process manager such as `systemd`, Docker, or PM2 equivalent for Python process supervision.
- Persist the SQLite database on durable storage.
- Send logs to your platform log collector.
- If you need horizontal scale, replace SQLite with PostgreSQL and run the bot through webhooks instead of polling.

## Notes for future extension

- Replace SQLite with PostgreSQL for multi-instance deployments.
- Add webhook support behind HTTPS.
- Add metrics and health endpoints.
- Add retry classification for transient Telegram API failures.