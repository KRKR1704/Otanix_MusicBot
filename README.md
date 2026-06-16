# Otanix — Discord Music Bot

A self-hosted Discord music bot that searches and streams audio from YouTube directly into voice channels. Uses `discord.py`, `yt-dlp`, and `FFmpeg`.

---

## Requirements

Before you start, install these on your system:

| Tool | Download |
|------|----------|
| Python 3.10+ | https://python.org |
| Node.js (LTS) | https://nodejs.org |
| FFmpeg | `winget install Gyan.FFmpeg` (Windows) or `sudo apt install ffmpeg` (Linux) |
| Git | https://git-scm.com |

---

## Setup (Local)

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/otanix.git
cd otanix
```

### 2. Create a virtual environment

```bash
python -m venv .venv
```

Activate it:

- **Windows:** `.venv\Scripts\activate`
- **Linux/Mac:** `source .venv/bin/activate`

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Create your Discord bot

1. Go to https://discord.com/developers/applications
2. Click **New Application** → name it `Otanix`
3. Go to **Bot** → click **Add Bot**
4. Under **Privileged Gateway Intents**, enable:
   - **Message Content Intent**
   - **Server Members Intent** (optional but recommended)
5. Click **Reset Token** → copy your token

### 5. Configure environment variables

```bash
cp .env.example .env
```

Open `.env` and fill in:

```env
DISCORD_TOKEN=your_bot_token_here
```

### 6. Run the bot

```bash
python bot.py
```

You should see `Logged in as Otanix#XXXX` in the terminal.

---

## Invite the bot to a server

1. Go to https://discord.com/developers/applications → your app
2. Click **OAuth2** → **URL Generator**
3. Under **Scopes**, check: `bot`
4. Under **Bot Permissions**, check:
   - Connect
   - Speak
   - Send Messages
   - Embed Links
   - Read Message History
5. Copy the URL at the bottom and open it in your browser to invite the bot

Share this URL with friends so they can add Otanix to their own servers too.

---

## Commands

| Command | Aliases | Description |
|---------|---------|-------------|
| `!play <query>` | `!p` | Play a song by name or YouTube URL |
| `!pause` | | Pause the current track |
| `!resume` | `!r` | Resume the paused track |
| `!skip` | `!next`, `!s` | Skip to the next track |
| `!queue` | `!q` | Show the queue |
| `!nowplaying` | `!np` | Show the current track |
| `!stop` | | Stop playback and clear the queue |
| `!leave` | `!dc`, `!disconnect` | Disconnect from voice |
| `!loop` | `!l` | Toggle loop mode |
| `!remove <position>` | | Remove a track from the queue |
| `!clear` | | Clear the queue without stopping |
| `!volume <0-100>` | `!vol` | Set the volume |
| `!shuffle` | | Shuffle the queue |
| `!musichelp` | `!mhelp` | Show all commands |

---

## Voice channel permissions

If the bot joins but doesn't play, make sure it has these permissions in the voice channel:

- **Connect**
- **Speak**

Set these in: Channel Settings → Permissions → add the bot's role.

---

## Project structure

```
discord-music-bot/
├── bot.py              # Entry point
├── cogs/
│   ├── __init__.py
│   └── music.py        # All music commands and playback logic
├── .env                # Your secrets (never commit this)
├── .env.example        # Template for .env
├── .gitignore
├── requirements.txt
└── README.md
```
