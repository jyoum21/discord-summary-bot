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
import re
import sqlite3
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
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-4-5-20251101")
MAX_TOKENS_PER_CHANNEL = int(os.getenv("MAX_TOKENS_PER_CHANNEL", "8000"))
IGNORED_CHANNELS = [
    c.strip()
    for c in os.getenv("IGNORED_CHANNELS", "").split(",")
    if c.strip()
]
DB_PATH = os.getenv("DB_PATH", "messages.db")
BACKFILL_DAYS = int(os.getenv("BACKFILL_DAYS", "30"))
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}

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

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
claude = Anthropic(api_key=ANTHROPIC_API_KEY)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_text(response) -> str:
    """Extract text from a Claude response that may contain tool-use blocks."""
    parts = []
    for block in response.content:
        if hasattr(block, "text"):
            parts.append(block.text)
    return "\n\n".join(parts) if parts else ""


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
                msg_link = (
                    f"https://discord.com/channels/"
                    f"{msg.guild.id}/{msg.channel.id}/{msg.id}"
                )
                messages.append(
                    f"[{timestamp}] {author}: {reply_prefix}{content} "
                    f"(<{msg_link}>)"
                )

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
    prompt = f"""You summarize daily progress for a Discord server on {date_str}.

Rules:
- Focus ONLY on progress toward the final product: decisions made, work completed, blockers hit, next steps.
- Keep it short. Use 1-2 sentence paragraphs max. Use bullet points for lists of items.
- Skip casual/social chatter entirely.
- Attribute decisions and commitments to specific people.
- When referencing a specific message or conversation, always include its hyperlink (provided in the context as <https://discord.com/channels/...>).
- Do NOT include a "Key Open Questions" or "Unresolved" section.
- Keep the entire summary under 400 words.

Here are today's messages:

"""
    for ch_name, msgs in channel_messages.items():
        prompt += f"=== #{ch_name} ===\n{msgs}\n\n"

    prompt += "Write the daily progress summary now."
    return prompt


def generate_summary(channel_messages: dict[str, str], date_str: str) -> str:
    """Call Claude to generate a summary."""
    prompt = build_prompt(channel_messages, date_str)

    log.info(f"Sending {len(prompt)} chars to Claude ({CLAUDE_MODEL})...")

    response = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        tools=[WEB_SEARCH_TOOL],
        messages=[{"role": "user", "content": prompt}],
    )

    return _extract_text(response)


def find_summary_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    """Find the channel to post summaries in."""
    for channel in guild.text_channels:
        if channel.name == SUMMARY_CHANNEL_NAME:
            return channel
    return None


async def get_or_create_thread(ctx: commands.Context, name: str) -> discord.abc.Messageable:
    """If already in a thread, return it. Otherwise create a new thread on the message."""
    if isinstance(ctx.channel, discord.Thread):
        return ctx.channel
    return await ctx.message.create_thread(name=name[:100])


async def split_and_send(target: discord.abc.Messageable, content: str):
    """Send a message, splitting if it exceeds Discord's 2000 char limit."""
    if len(content) <= 2000:
        await target.send(content)
        return

    chunks = []
    current = ""
    for paragraph in content.split("\n\n"):
        if len(current) + len(paragraph) + 2 > 1900:
            chunks.append(current.strip())
            current = ""
        current += paragraph + "\n\n"
    if current.strip():
        chunks.append(current.strip())

    for i, chunk in enumerate(chunks):
        await target.send(chunk)
        if i < len(chunks) - 1:
            await asyncio.sleep(0.5)


# ---------------------------------------------------------------------------
# Persistent message store (SQLite + FTS5)
# ---------------------------------------------------------------------------

_STOP_WORDS = frozenset({
    "what", "is", "the", "a", "an", "in", "on", "at", "to", "for", "of",
    "and", "or", "are", "was", "were", "how", "why", "when", "where", "who",
    "which", "that", "this", "with", "from", "by", "about", "do", "does",
    "did", "will", "would", "could", "should", "can", "has", "have", "had",
    "been", "being", "be", "it", "its", "they", "them", "their", "we", "our",
    "i", "my", "me", "you", "your", "not", "but", "if", "so", "just",
    "than", "then", "also", "into", "out", "up", "down",
})

_backfill_done: set[int] = set()


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Create tables, FTS5 index, and sync triggers."""
    conn = _get_db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            guild_id TEXT NOT NULL,
            channel_id TEXT NOT NULL DEFAULT '',
            channel_name TEXT NOT NULL,
            author TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
    """)
    try:
        c.execute("ALTER TABLE messages ADD COLUMN channel_id TEXT NOT NULL DEFAULT ''")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_messages_guild_time
        ON messages(guild_id, timestamp DESC)
    """)
    c.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            channel_name, author, content,
            content=messages, content_rowid=rowid
        )
    """)
    c.executescript("""
        CREATE TRIGGER IF NOT EXISTS fts_insert AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, channel_name, author, content)
            VALUES (new.rowid, new.channel_name, new.author, new.content);
        END;
        CREATE TRIGGER IF NOT EXISTS fts_delete AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, channel_name, author, content)
            VALUES ('delete', old.rowid, old.channel_name, old.author, old.content);
        END;
        CREATE TRIGGER IF NOT EXISTS fts_update AFTER UPDATE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, channel_name, author, content)
            VALUES ('delete', old.rowid, old.channel_name, old.author, old.content);
            INSERT INTO messages_fts(rowid, channel_name, author, content)
            VALUES (new.rowid, new.channel_name, new.author, new.content);
        END;
    """)

    msg_count = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    if msg_count > 0:
        fts_count = c.execute(
            "SELECT COUNT(*) FROM messages_fts").fetchone()[0]
        if fts_count == 0:
            log.info(f"Rebuilding FTS index for {msg_count} messages...")
            c.execute(
                "INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')")

    conn.commit()
    conn.close()
    log.info(f"Message database ready ({DB_PATH})")


def store_message(msg_id: str, guild_id: str, channel_id: str,
                  channel_name: str, author: str, content: str,
                  timestamp: str):
    """Persist a single message (no-op on duplicate)."""
    conn = _get_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO messages "
            "(id, guild_id, channel_id, channel_name, author, content, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (msg_id, guild_id, channel_id, channel_name, author, content, timestamp),
        )
        conn.commit()
    finally:
        conn.close()


def store_messages_batch(rows: list[tuple]):
    """Bulk-insert messages (skips duplicates)."""
    conn = _get_db()
    try:
        conn.executemany(
            "INSERT OR IGNORE INTO messages "
            "(id, guild_id, channel_id, channel_name, author, content, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", rows
        )
        conn.commit()
    finally:
        conn.close()


def _build_fts_query(question: str) -> str:
    """Convert a natural-language question into an FTS5 OR-query."""
    cleaned = re.sub(r"[^\w\s]", " ", question)
    words = [w for w in cleaned.lower().split()
             if w not in _STOP_WORDS and len(w) > 1]
    if not words:
        words = [w for w in cleaned.lower().split() if len(w) > 1][:3]
    if not words:
        return ""
    return " OR ".join(f'"{w}"' for w in words[:10])


def search_messages(guild_id: str, question: str,
                    limit: int = 80) -> list[dict]:
    """Full-text search for messages relevant to *question*."""
    fts_q = _build_fts_query(question)
    if not fts_q:
        return []
    conn = _get_db()
    try:
        rows = conn.execute("""
            SELECT m.id, m.guild_id, m.channel_id,
                   m.channel_name, m.author, m.content, m.timestamp
            FROM messages m
            JOIN messages_fts ON messages_fts.rowid = m.rowid
            WHERE messages_fts MATCH ? AND m.guild_id = ?
            ORDER BY rank
            LIMIT ?
        """, (fts_q, guild_id, limit)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"FTS search failed: {e}")
        return []
    finally:
        conn.close()


def get_recent_messages(guild_id: str, days: int = 7,
                        limit: int = 500) -> list[dict]:
    """Return the most recent messages from the last *days* days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = _get_db()
    try:
        rows = conn.execute("""
            SELECT id, guild_id, channel_id,
                   channel_name, author, content, timestamp
            FROM messages
            WHERE guild_id = ? AND timestamp >= ?
            ORDER BY timestamp DESC
            LIMIT ?
        """, (guild_id, cutoff, limit)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"Recent-messages query failed: {e}")
        return []
    finally:
        conn.close()


def _format_db_messages(rows: list[dict], header: str,
                        max_chars: int = 60000) -> str:
    """Format DB rows into a readable context block for Claude."""
    if not rows:
        return ""
    by_channel: dict[str, list[str]] = {}
    for r in rows:
        ch = r["channel_name"]
        if ch not in by_channel:
            by_channel[ch] = []
        try:
            ts = datetime.fromisoformat(
                r["timestamp"]).strftime("%Y-%m-%d %I:%M %p")
        except (ValueError, TypeError):
            ts = r["timestamp"]
        msg_link = ""
        if r.get("channel_id") and r.get("id") and r.get("guild_id"):
            msg_link = (
                f" (<https://discord.com/channels/"
                f"{r['guild_id']}/{r['channel_id']}/{r['id']}>)"
            )
        by_channel[ch].append(
            f"[{ts}] {r['author']}: {r['content']}{msg_link}"
        )

    parts = [f"=== {header} ==="]
    for ch, msgs in by_channel.items():
        parts.append(f"\n## #{ch}")
        parts.extend(msgs)
    text = "\n".join(parts)
    return truncate_text(text, max_chars)


async def backfill_messages(guild: discord.Guild):
    """One-time backfill of message history from all readable channels."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=BACKFILL_DAYS)
    total = 0
    log.info(
        f"Backfilling up to {BACKFILL_DAYS} days of history "
        f"for {guild.name}..."
    )

    for channel in guild.text_channels:
        if channel.name in IGNORED_CHANNELS:
            continue
        batch: list[tuple] = []
        try:
            async for msg in channel.history(
                after=cutoff, limit=None, oldest_first=True
            ):
                if msg.author.bot or not msg.content.strip():
                    continue
                batch.append((
                    str(msg.id), str(guild.id), str(channel.id),
                    channel.name, msg.author.display_name,
                    msg.content, msg.created_at.isoformat(),
                ))
                if len(batch) >= 500:
                    store_messages_batch(batch)
                    total += len(batch)
                    batch = []
            if batch:
                store_messages_batch(batch)
                total += len(batch)
        except discord.Forbidden:
            log.warning(f"  Backfill: no permission for #{channel.name}")
        except Exception as e:
            log.error(f"  Backfill error in #{channel.name}: {e}")
        await asyncio.sleep(1)

    log.info(f"Backfill complete for {guild.name}: {total} messages stored")


# ---------------------------------------------------------------------------
# Project context — reads all past summaries from the summary channel
# ---------------------------------------------------------------------------

async def fetch_project_context(guild: discord.Guild) -> str:
    """Read all past bot-posted summaries from the summary channel."""
    channel = find_summary_channel(guild)
    if channel is None:
        return ""

    summaries = []
    async for msg in channel.history(limit=200, oldest_first=True):
        if msg.author.id == bot.user.id and msg.content.strip():
            summaries.append(msg.content)

    return "\n\n---\n\n".join(summaries)


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

    date_str_header = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    header = f"## 📋 Daily Summary — {date_str_header}\n\n"
    await split_and_send(target_channel, header + summary)
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
        if guild.id not in _backfill_done:
            _backfill_done.add(guild.id)
            bot.loop.create_task(backfill_messages(guild))
    if not daily_summary_loop.is_running():
        daily_summary_loop.start()


@bot.event
async def on_message(msg: discord.Message):
    """Capture every non-bot message into the database in real time."""
    if not msg.author.bot and msg.guild and msg.content.strip():
        channel_name = getattr(msg.channel, "name", None)
        if channel_name and channel_name not in IGNORED_CHANNELS:
            store_message(
                str(msg.id), str(msg.guild.id),
                str(msg.channel.id), channel_name,
                msg.author.display_name, msg.content,
                msg.created_at.isoformat(),
            )
    await bot.process_commands(msg)


@bot.command(name="today")
async def cmd_today(ctx: commands.Context):
    """!today — Generate a summary for today immediately."""
    thread = await get_or_create_thread(ctx, "Today's Summary")
    await thread.send("⏳ Generating summary... this may take a minute.")
    try:
        summary = await run_summary(ctx.guild)
        if summary is None:
            await thread.send("No messages found today.")
        else:
            await thread.send("✅ Summary posted to #{}".format(SUMMARY_CHANNEL_NAME))
    except Exception as e:
        log.error(f"Manual summary failed: {e}")
        await thread.send(f"❌ Error generating summary: {e}")


@bot.command(name="yesterday")
async def cmd_yesterday(ctx: commands.Context):
    """!yesterday — Generate a summary for yesterday."""
    thread = await get_or_create_thread(ctx, "Yesterday's Summary")
    await thread.send("⏳ Generating yesterday's summary...")
    try:
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        summary = await run_summary(ctx.guild, target_date=yesterday)
        if summary is None:
            await thread.send("No messages found yesterday.")
        else:
            await thread.send("✅ Summary posted to #{}".format(SUMMARY_CHANNEL_NAME))
    except Exception as e:
        log.error(f"Manual summary failed: {e}")
        await thread.send(f"❌ Error generating summary: {e}")


@bot.command(name="query")
async def cmd_query(ctx: commands.Context, *, question: str = ""):
    """!query <question> — Ask a question about the project."""
    if not question:
        await ctx.reply("Usage: `!query <your question>`")
        return

    thread = await get_or_create_thread(ctx, question[:100])
    await thread.send("🤔 Searching project history...")
    try:
        guild_id = str(ctx.guild.id)

        relevant = search_messages(guild_id, question, limit=80)
        recent = get_recent_messages(guild_id, days=7, limit=300)
        summaries = await fetch_project_context(ctx.guild)

        relevant_block = _format_db_messages(
            relevant, "MESSAGES MATCHING YOUR QUERY")
        recent_block = _format_db_messages(
            recent, "RECENT SERVER ACTIVITY (last 7 days)")
        summary_block = (
            f"=== DAILY PROGRESS SUMMARIES ===\n{summaries}"
            if summaries else ""
        )

        context = "\n\n".join(
            b for b in [relevant_block, recent_block, summary_block] if b
        )

        if not context.strip():
            await thread.send(
                "No project history found yet. The bot captures messages "
                "automatically — check back after some activity, or run "
                "`!today` to generate the first summary."
            )
            return

        prompt = f"""You are a knowledgeable assistant for a project team's Discord server. You have access to three sources of context:

1. Messages that matched the user's query (most relevant)
2. Recent server activity (last 7 days of raw messages)
3. Daily progress summaries (high-level project history)

Use ALL available context to answer the question accurately and concisely.
If the answer isn't fully covered, say what you can determine and note any gaps.
When referencing specific messages, always include the message hyperlink (provided in the context as <https://discord.com/channels/...>).

{context}

Question: {question}

Answer concisely."""

        log.info(f"!query: {question} ({len(prompt)} chars)")
        response = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            tools=[WEB_SEARCH_TOOL],
            messages=[{"role": "user", "content": prompt}],
        )
        answer = _extract_text(response)
        await split_and_send(thread, f"**Q:** {question}\n\n{answer}")
    except Exception as e:
        log.error(f"Query command failed: {e}")
        await thread.send(f"❌ Error: {e}")


@bot.command(name="summary")
async def cmd_summary(ctx: commands.Context):
    """!summary — Generate a high-level overview of the entire project."""
    thread = await get_or_create_thread(ctx, "Project Overview")
    await thread.send("📊 Generating project overview...")
    try:
        guild_id = str(ctx.guild.id)
        summaries = await fetch_project_context(ctx.guild)
        recent = get_recent_messages(guild_id, days=14, limit=500)
        recent_block = _format_db_messages(
            recent, "RECENT SERVER ACTIVITY (last 14 days)")

        context = ""
        if summaries:
            context += f"=== DAILY PROGRESS SUMMARIES ===\n{summaries}\n\n"
        if recent_block:
            context += recent_block

        if not context.strip():
            await thread.send(
                "No project history found yet. The bot captures messages "
                "automatically — check back after some activity."
            )
            return

        prompt = f"""You are a project analyst. Below is the full context from a project team's Discord server, including daily summaries and recent raw messages. Write a high-level project overview that covers:

1. **The problem** the team is trying to solve (1-2 sentences)
2. **The approach / solution** being built (short paragraph)
3. **Subteams / workstreams** and who is working on what (bullet points)
4. **Current status** — what's done, what's in progress (bullet points)
5. **Key milestones** reached so far (bullet points)

Keep it concise and factual. Use only information from the provided context.
When referencing specific messages or conversations, always include the message hyperlink (provided in the context as <https://discord.com/channels/...>).

{context}

Write the project overview now."""

        log.info("Generating total project summary...")
        response = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            tools=[WEB_SEARCH_TOOL],
            messages=[{"role": "user", "content": prompt}],
        )
        overview = _extract_text(response)
        await split_and_send(thread, f"## 📊 Project Overview\n\n{overview}")
    except Exception as e:
        log.error(f"Total summary failed: {e}")
        await thread.send(f"❌ Error: {e}")


@bot.command(name="help")
async def cmd_help(ctx: commands.Context):
    """!help — Show available commands."""
    help_text = """**📋 Summary Bot Commands**

`!today` — Generate today's summary
`!yesterday` — Generate yesterday's summary
`!query <question>` — Ask a question about the project (searches all message history)
`!summary` — High-level overview of the entire project
`!help` — Show this message

The bot captures all server messages for context and posts an automatic summary every day at {hour:02d}:{minute:02d} UTC in #{channel}.
""".format(
        hour=SUMMARY_HOUR,
        minute=SUMMARY_MINUTE,
        channel=SUMMARY_CHANNEL_NAME,
    )
    await ctx.send(help_text)


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

    init_db()
    log.info("Starting Summary Bot...")
    bot.run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
