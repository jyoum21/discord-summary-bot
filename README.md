# Discord Daily Summary Bot

A Discord bot that reads all messages from your server each day and posts a Claude-generated summary to a designated channel.

## Features

- **Automatic daily summaries** at a configurable time (UTC)
- **On-demand summaries** via `!summarynow` and `!summaryyesterday`
- Organizes by theme, highlights decisions/action items/blockers
- Handles attachments, embeds, replies
- Skips bot messages (won't summarize its own summaries)
- Splits long summaries across multiple Discord messages
- Per-channel token limits to keep costs down
- Configurable channel ignore list

## Setup

### 1. Create a Discord Bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application**, give it a name (e.g., "Daily Summary")
3. Go to the **Bot** tab
4. Click **Reset Token** and copy it — you'll need this for `.env`
5. Under **Privileged Gateway Intents**, enable:
   - **Message Content Intent** ✅
   - **Server Members Intent** ✅
6. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`
   - Bot Permissions: `Read Messages/View Channels`, `Send Messages`, `Read Message History`
7. Copy the generated URL and open it in your browser to invite the bot to your server

### 2. Create a Summary Channel

Create a text channel in your Discord server called `#daily-summary` (or whatever you set in `.env`).

### 3. Get an Anthropic API Key

Get one from [console.anthropic.com](https://console.anthropic.com). Sonnet is recommended for the best cost/quality balance (~$0.05-0.20 per daily summary depending on server activity).

### 4. Configure and Run

```bash
cd discord-summary-bot

# Install dependencies
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your tokens and preferences

# Run
python bot.py
```

### 5. (Optional) Deploy

For always-on operation, deploy to any server or cloud platform:

**Systemd (Linux VPS):**
```bash
# /etc/systemd/system/summary-bot.service
[Unit]
Description=Discord Summary Bot
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/path/to/discord-summary-bot
ExecStart=/usr/bin/python3 bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now summary-bot
```

**Docker:**
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "bot.py"]
```

```bash
docker build -t summary-bot .
docker run -d --env-file .env --name summary-bot summary-bot
```

## Commands

| Command | Permission Required | Description |
|---------|-------------------|-------------|
| `!summarynow` | Manage Messages | Generate today's summary immediately |
| `!summaryyesterday` | Manage Messages | Generate yesterday's summary |
| `!summaryhelp` | None | Show available commands |

## Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `DISCORD_TOKEN` | — | Your Discord bot token (required) |
| `ANTHROPIC_API_KEY` | — | Your Anthropic API key (required) |
| `SUMMARY_CHANNEL` | `daily-summary` | Channel name to post summaries |
| `SUMMARY_HOUR` | `23` | Hour to post (UTC, 0-23) |
| `SUMMARY_MINUTE` | `55` | Minute to post (0-59) |
| `CLAUDE_MODEL` | `claude-sonnet-4-5-20250929` | Model for summaries |
| `MAX_TOKENS_PER_CHANNEL` | `8000` | Token cap per channel to control cost |
| `IGNORED_CHANNELS` | — | Comma-separated channels to skip |

## Cost

Rough estimate per daily summary based on server activity:

| Server Activity | Messages/Day | Approx. Cost |
|----------------|-------------|--------------|
| Quiet | < 100 | ~$0.02 |
| Moderate | 100–500 | ~$0.05–0.10 |
| Active | 500–2000 | ~$0.10–0.20 |
| Very Active | 2000+ | ~$0.20–0.40 |

Monthly cost for a moderately active server: **~$2-5**.
