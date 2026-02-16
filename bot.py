"""
Discord Daily Summary Bot — powered by Claude

Reads all messages from the day across a Discord server's text channels
and posts a Claude-generated summary to a designated channel.

Usage:
  1. Copy .env.example to .env and fill in your tokens
  2. pip install -r requirements.txt
  3. python bot.py
"""

import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord.ext import commands, tasks
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
SUMMARY_CHANNEL_NAME = os.getenv("SUMMARY_CHANNEL", "daily-summary")
SUMMARY_HOUR = int(os.getenv("SUMMARY_HOUR", "23"))  # 24h format, UTC
SUMMARY_MINUTE = int(os.getenv("SUMMARY_MINUTE", "55"))
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
log = logging.getLogger("summary-bot")

# ---------------------------------------------------------------------------
# Discord client setup
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!summary", intents=intents)
claude = Anthropic(api_key=ANTHROPIC_API_KEY)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def truncate_text(text: str, max_chars: int) -> str:
    """Truncate text to roughly max_chars, preserving complete messages."""
    if len(text) <= max_chars:
        return text
    # Cut at a message boundary (newline before a timestamp bracket)
    truncated = text[:max_chars]
    last_newline = truncated.rfind("\n[")
    if last_newline > max_chars // 2:
        truncated = truncated[:last_newline]
    return truncated + "\n... (truncated, earlier messages omitted)"


async def fetch_channel_messages(
    channel: discord.TextChannel,
    after: datetime,
    before: datetime,
) -> list[str]:
    """Fetch all messages from a channel within a time window."""
    messages = []
    try:
        async for msg in channel.history(after=after, before=before, limit=None, oldest_first=True):
            # Skip bot messages (including our own summaries)
            if msg.author.bot:
                continue

            timestamp = msg.created_at.strftime("%I:%M %p")
            author = msg.author.display_name
            content = msg.content or ""

            # Note attachments
            if msg.attachments:
                attachment_names = ", ".join(a.filename for a in msg.attachments)
                content += f"\n  [Attachments: {attachment_names}]"

            # Note embeds
            for embed in msg.embeds:
                if embed.title:
                    content += f"\n  [Embed: {embed.title}]"
                if embed.url:
                    content += f"\n  {embed.url}"

            # Note replies
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


async def collect_all_messages(
    guild: discord.Guild,
    after: datetime,
    before: datetime,
) -> dict[str, str]:
    """Collect messages from all readable text channels in a guild."""
    channel_messages: dict[str, str] = {}

    for channel in guild.text_channels:
        if channel.name in IGNORED_CHANNELS:
            continue

        messages = await fetch_channel_messages(channel, after, before)
        if messages:
            text = "\n".join(messages)
            # Rough token estimate: 1 token ≈ 4 chars
            text = truncate_text(text, MAX_TOKENS_PER_CHANNEL * 4)
            channel_messages[channel.name] = text
            log.info(f"  #{channel.name}: {len(messages)} messages")

    return channel_messages


def build_prompt(channel_messages: dict[str, str], date_str: str) -> str:
    """Build the Claude prompt from collected messages."""
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


def generate_summary(channel_messages: dict[str, str], date_str: str) -> str:
    """Call Claude to generate a summary."""
    prompt = build_prompt(channel_messages, date_str)

    log.info(f"Sending {len(prompt)} chars to Claude ({CLAUDE_MODEL})...")

    response = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )

    return response.content[0].text


def find_summary_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    """Find the channel to post summaries in."""
    for channel in guild.text_channels:
        if channel.name == SUMMARY_CHANNEL_NAME:
            return channel
    return None


async def split_and_send(channel: discord.TextChannel, content: str):
    """Send a message, splitting if it exceeds Discord's 2000 char limit."""
    # Add header
    date_str = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    header = f"## 📋 Daily Summary — {date_str}\n\n"
    full_content = header + content

    if len(full_content) <= 2000:
        await channel.send(full_content)
        return

    # Split into chunks at paragraph boundaries
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
# Core summary routine (reusable by both scheduled task and command)
# ---------------------------------------------------------------------------

async def run_summary(guild: discord.Guild, target_date: Optional[datetime] = None):
    """Generate and post summary for a given date (defaults to today)."""
    now = datetime.now(timezone.utc)
    if target_date is None:
        target_date = now

    after = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
    before = after + timedelta(days=1)

    date_str = after.strftime("%A, %B %d, %Y")
    log.info(f"Generating summary for {date_str}...")

    # Collect messages
    channel_messages = await collect_all_messages(guild, after, before)

    if not channel_messages:
        log.info("No messages found today. Skipping summary.")
        return None

    total_msgs = sum(
        text.count("\n") + 1 for text in channel_messages.values()
    )
    log.info(f"Collected ~{total_msgs} messages across {len(channel_messages)} channels")

    # Generate summary
    summary = generate_summary(channel_messages, date_str)

    # Post it
    target_channel = find_summary_channel(guild)
    if target_channel is None:
        log.error(
            f"Could not find #{SUMMARY_CHANNEL_NAME}. "
            f"Create the channel or set SUMMARY_CHANNEL in .env"
        )
        return summary

    await split_and_send(target_channel, summary)
    log.info(f"Summary posted to #{target_channel.name}")
    return summary


# ---------------------------------------------------------------------------
# Scheduled daily task
# ---------------------------------------------------------------------------

@tasks.loop(minutes=1)
async def daily_summary_loop():
    """Check every minute if it's time to post the daily summary."""
    now = datetime.now(timezone.utc)
    if now.hour == SUMMARY_HOUR and now.minute == SUMMARY_MINUTE:
        for guild in bot.guilds:
            try:
                await run_summary(guild)
            except Exception as e:
                log.error(f"Failed to generate summary for {guild.name}: {e}")
        # Sleep 60s to avoid double-triggering
        await asyncio.sleep(60)


@daily_summary_loop.before_loop
async def before_daily_summary():
    await bot.wait_until_ready()
    log.info(
        f"Daily summary scheduled for {SUMMARY_HOUR:02d}:{SUMMARY_MINUTE:02d} UTC"
    )


# ---------------------------------------------------------------------------
# Bot commands
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    log.info(f"Connected to {len(bot.guilds)} guild(s)")
    for guild in bot.guilds:
        log.info(f"  - {guild.name} ({guild.member_count} members)")
        target = find_summary_channel(guild)
        if target:
            log.info(f"    Summary channel: #{target.name}")
        else:
            log.warning(
                f"    ⚠ No #{SUMMARY_CHANNEL_NAME} channel found! "
                f"Create one or set SUMMARY_CHANNEL in .env"
            )
    if not daily_summary_loop.is_running():
        daily_summary_loop.start()


@bot.command(name="now")
async def summary_now(ctx: commands.Context):
    """!summarynow — Generate a summary for today immediately."""
    if not ctx.author.guild_permissions.manage_messages:
        await ctx.reply("You need Manage Messages permission to trigger a summary.")
        return

    await ctx.reply("⏳ Generating summary... this may take a minute.")
    try:
        summary = await run_summary(ctx.guild)
        if summary is None:
            await ctx.reply("No messages found today.")
    except Exception as e:
        log.error(f"Manual summary failed: {e}")
        await ctx.reply(f"❌ Error generating summary: {e}")


@bot.command(name="yesterday")
async def summary_yesterday(ctx: commands.Context):
    """!summaryyesterday — Generate a summary for yesterday."""
    if not ctx.author.guild_permissions.manage_messages:
        await ctx.reply("You need Manage Messages permission to trigger a summary.")
        return

    await ctx.reply("⏳ Generating yesterday's summary...")
    try:
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        summary = await run_summary(ctx.guild, target_date=yesterday)
        if summary is None:
            await ctx.reply("No messages found yesterday.")
    except Exception as e:
        log.error(f"Manual summary failed: {e}")
        await ctx.reply(f"❌ Error generating summary: {e}")


@bot.command(name="summaryhelp")
async def summary_help(ctx: commands.Context):
    """!summaryhelp — Show available commands."""
    help_text = """**📋 Summary Bot Commands**

`!summarynow` — Generate today's summary immediately
`!summaryyesterday` — Generate yesterday's summary
`!summaryhelp` — Show this message

The bot also posts an automatic summary every day at {hour:02d}:{minute:02d} UTC in #{channel}.
""".format(
        hour=SUMMARY_HOUR,
        minute=SUMMARY_MINUTE,
        channel=SUMMARY_CHANNEL_NAME,
    )
    await ctx.reply(help_text)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    if not DISCORD_TOKEN:
        log.error("DISCORD_TOKEN not set. Check your .env file.")
        return
    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set. Check your .env file.")
        return

    log.info("Starting Summary Bot...")
    bot.run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
