"""
Quick connectivity test — logs in as the bot, sends a hello message
to the configured SUMMARY_CHANNEL, then exits.

Usage:  python test_connection.py
"""

import os
import discord
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
SUMMARY_CHANNEL = os.getenv("SUMMARY_CHANNEL", "claude-summary")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"✅ Logged in as {client.user}  (ID: {client.user.id})")
    print(f"   Connected to {len(client.guilds)} server(s):\n")

    for guild in client.guilds:
        print(f"   • {guild.name}  (ID: {guild.id})")

    # Try to find the summary channel and send a test message
    target = None
    for guild in client.guilds:
        for channel in guild.text_channels:
            if channel.name == SUMMARY_CHANNEL:
                target = channel
                break
        if target:
            break

    if target is None:
        print(f"\n❌ Could not find a channel named #{SUMMARY_CHANNEL}")
        print("   Available text channels:")
        for guild in client.guilds:
            for ch in guild.text_channels:
                print(f"     - #{ch.name}")
    else:
        print(f"\n📨 Sending test message to #{target.name} ...")
        await target.send("👋 Hello! Bot connection test successful.")
        print("✅ Message sent!")

    print("\nDone — shutting down.")
    await client.close()


print("Connecting to Discord...")
client.run(DISCORD_TOKEN)
