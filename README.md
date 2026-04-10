<div align="center">

<img src="https://raw.githubusercontent.com/NishulDhakar/Auto-Reaction-Bot/main/assets/logo.png" alt="Auto Reaction Bot" width="180"/>

# ❤️ Auto Reaction Bot

[![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python&style=flat)](https://python.org)
[![python-telegram-bot](https://img.shields.io/badge/python--telegram--bot-v20+-green?style=flat)](https://python-telegram-bot.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![DigitalOcean](https://img.shields.io/badge/Hosted%20on-DigitalOcean-0080FF?logo=digitalocean&style=flat)](https://digitalocean.com)

**A production-ready Telegram bot that automatically reacts to posts in channels and groups — built with Python & python-telegram-bot v20+**

[Features](#-features) · [Setup](#-setup) · [Deploy](#-deployment) · [Commands](#-commands) · [Report Bug](https://github.com/NishulDhakar/Auto-Reaction-Bot/issues/new)

</div>

---

## ✨ Features

- 🔁 **Auto-reacts** to every new channel post with emoji from a customizable pool
- 👥 **Multi-chat support** — works across channels and groups
- 💾 **SQLite persistence** — tracks users, channels, and reaction logs
- 🛡️ **Admin controls** — broadcast, list all channels, manage users
- 📡 **Systemd-ready** — designed for production on Linux servers
- 🧹 **Auto-cleanup** — purges old reaction logs on a schedule
- ⚡ **Built on python-telegram-bot v20+** (async, modern)

---

## 📸 Preview

> *(Add a GIF here showing the bot reacting to a channel post)*

---

## 🤖 Commands

| Command | Who | Description |
|---|---|---|
| `/start` | Everyone | Register and start the bot |
| `/help` | Everyone | Show available commands |
| `/mychannels` | Everyone | List your tracked channels |
| `/allchannels` | Admin | List all known channels |
| `/broadcasttoall` | Admin | Send a message to all users |

---

## ⚙️ Setup

### 1. Clone the repo

```bash
git clone https://github.com/NishulDhakar/Auto-Reaction-Bot.git
cd Auto-Reaction-Bot
```

### 2. Create virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | ✅ | Token from [@BotFather](https://t.me/BotFather) |
| `ADMIN_USER_IDS` | ✅ | Comma-separated Telegram user IDs |
| `DB_PATH` | ❌ | SQLite file path (default: `bot.db`) |
| `LOG_LEVEL` | ❌ | Logging level (default: `INFO`) |
| `CLEANUP_DAYS` | ❌ | Days to retain reaction logs |
| `REACTION_POOL` | ❌ | Comma-separated emoji pool |

### 5. Run

```bash
python bot.py
```

---

## 🚀 Deployment

### Systemd (Recommended for Linux/DigitalOcean)

```ini
[Unit]
Description=Auto Reaction Telegram Bot
After=network.target

[Service]
WorkingDirectory=/path/to/Auto-Reaction-Bot
ExecStart=/path/to/.venv/bin/python bot.py
Restart=always
EnvironmentFile=/path/to/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable auto-reaction-bot
sudo systemctl start auto-reaction-bot
```

---

<div align="center">
  <p>Built with ♥ by <a href="https://www.nishul.dev">Nishul Dhakar</a></p>
  <p>
    <a href="https://twitter.com/nishuldhakar">Twitter</a> •
    <a href="https://github.com/Nishuldhakar">GitHub</a>
  </p>
</div>

