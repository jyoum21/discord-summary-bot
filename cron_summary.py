"""
Cron-friendly summary script — connects, generates today's summary, posts it, exits.

Schedule this with cron / Task Scheduler instead of keeping bot.py running.

Usage:
  python cron_summary.py              # summarize today
  python cron_summary.py --yesterday  # summarize yesterday

Example crontab (daily at 23:55 UTC):
  55 23 * * * cd /path/to/discord-summary-bot && python cron_summary.py

Example Windows Task Scheduler:
  Program: python
  Arguments: C:\Users\jyoum\Downloads\discord-summary-bot\cron_summary.py
  Start in: C:\Users\jyoum\Downloads\discord-summary-bot
"""

import os
import sys
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration (same as bot.py)
# ---------------------------------------------------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
SUMMARY_CHANNEL_NAME = os.getenv("SUMMARY_CHANNEL", "daily-summary")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")
MAX_TOKENS_PER_CHANNEL = int(os.getenv("MAX_TOKENS_PER_CHANNEL", "8000"))
IGNORED_CHANNELS = [
    c.strip()
    for c in os.getenv("IGNORED_CHANNELS", "").split(",")
    if c.strip()
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("cron-summary")

# ---------------------------------------------------------------------------
# Discord client (minimal — no command handling needed)
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
client = discord.Client(intents=intents)
claude = Anthropic(api_key=ANTHROPIC_API_KEY)

# ---------------------------------------------------------------------------
# Reuse the same helpers from bot.py
# ---------------------------------------------------------------------------

def truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_newline = truncated.rfind("\n[")
    if last_newline > max_chars // 2:
        truncated = truncated[:last_newline]
    return truncated + "\n... (truncated, earlier messages omitted)"


async def fetch_channel_messages(channel, after, before):
    messages = []
    try:
        async for msg in channel.history(after=after, before=before, limit=None, oldest_first=True):
            if msg.author.bot:
                continue
            timestamp = msg.created_at.strftime("%I:%M %p")
            author = msg.author.display_name
            content = msg.content or ""
            if msg.attachments:
                attachment_names = ", ".join(a.filename for a in msg.attachments)
                content += f"\n  [Attachments: {attachment_names}]"
            for embed in msg.embeds:
                if embed.title:
                    content += f"\n  [Embed: {embed.title}]"
                if embed.url:
                    content += f"\n  {embed.url}"
            reply_prefix = ""
            if msg.reference and msg.reference.resolved:
                ref = msg.reference.resolved
                if isinstance(ref, discord.Message):
                    reply_prefix = f"(replying to {ref.author.display_name}) "
            if content.strip():
                messages.append(f"[{timestamp}] {author}: {reply_prefix}{content}")
    except discord.Forbidden:
        log.warning(f"No permission to read #{channel.name}")
    except Exception as e:
        log.error(f"Error reading #{channel.name}: {e}")
    return messages


async def collect_all_messages(guild, after, before):
    channel_messages = {}
    for channel in guild.text_channels:
        if channel.name in IGNORED_CHANNELS:
            continue
        messages = await fetch_channel_messages(channel, after, before)
        if messages:
            text = "\n".join(messages)
            text = truncate_text(text, MAX_TOKENS_PER_CHANNEL * 4)
            channel_messages[channel.name] = text
            log.info(f"  #{channel.name}: {len(messages)} messages")
    return channel_messages


def build_prompt(channel_messages, date_str):
    prompt = f"""You are a helpful assistant that summarizes Discord server activity.
Below are all the messages from a Discord server on {date_str}, organized by channel.

Your job:
1. Write a concise but comprehensive daily summary.
2. Organize by topic/theme rather than by channel when themes span channels.
3. Highlight key decisions, action items, blockers, and important links shared.
4. Mention who said what when attribution matters (e.g., decisions, commitments).
5. Note any unresolved questions or debates.
6. Keep it under ~800 words. Use natural prose, not excessive bullet points.
7. If a channel had only casual/social chat, summarize it in one sentence.
8. Skip channels with no substantive content.

Here are today's messages:

"""
    for ch_name, msgs in channel_messages.items():
        prompt += f"=== #{ch_name} ===\n{msgs}\n\n"
    prompt += "Write the daily summary now."
    return prompt


def generate_summary(channel_messages, date_str):
    prompt = build_prompt(channel_messages, date_str)
    log.info(f"Sending {len(prompt)} chars to Claude ({CLAUDE_MODEL})...")
    response = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


async def split_and_send(channel, content):
    date_str = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    header = f"## 📋 Daily Summary — {date_str}\n\n"
    full_content = header + content
    if len(full_content) <= 2000:
        await channel.send(full_content)
        return
    chunks = []
    current = header
    for paragraph in content.split("\n\n"):
        if len(current) + len(paragraph) + 2 > 1900:
            chunks.append(current.strip())
            current = ""
        current += paragraph + "\n\n"
    if current.strip():
        chunks.append(current.strip())
    for i, chunk in enumerate(chunks):
        await channel.send(chunk)
        if i < len(chunks) - 1:
            await asyncio.sleep(0.5)


# ---------------------------------------------------------------------------
# Main routine — runs on_ready, does the work, then exits
# ---------------------------------------------------------------------------

@client.event
async def on_ready():
    log.info(f"Logged in as {client.user}")

    use_yesterday = "--yesterday" in sys.argv

    for guild in client.guilds:
        log.info(f"Processing guild: {guild.name}")

        now = datetime.now(timezone.utc)
        if use_yesterday:
            target_date = now - timedelta(days=1)
        else:
            target_date = now

        after = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
        before = after + timedelta(days=1)
        date_str = after.strftime("%A, %B %d, %Y")

        log.info(f"Generating summary for {date_str}...")

        channel_messages = await collect_all_messages(guild, after, before)
        if not channel_messages:
            log.info("No messages found. Skipping.")
            continue

        total_msgs = sum(text.count("\n") + 1 for text in channel_messages.values())
        log.info(f"Collected ~{total_msgs} messages across {len(channel_messages)} channels")

        summary = generate_summary(channel_messages, date_str)

        # Find target channel
        target_channel = None
        for ch in guild.text_channels:
            if ch.name == SUMMARY_CHANNEL_NAME:
                target_channel = ch
                break

        if target_channel is None:
            log.error(f"Could not find #{SUMMARY_CHANNEL_NAME} in {guild.name}")
            # Print summary to stdout as fallback
            print("\n--- SUMMARY ---\n")
            print(summary)
            continue

        await split_and_send(target_channel, summary)
        log.info(f"✅ Summary posted to #{target_channel.name} in {guild.name}")

    log.info("Done — shutting down.")
    await client.close()


def main():
    if not DISCORD_TOKEN:
        log.error("DISCORD_TOKEN not set. Check your .env file.")
        sys.exit(1)
    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set. Check your .env file.")
        sys.exit(1)

    client.run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
