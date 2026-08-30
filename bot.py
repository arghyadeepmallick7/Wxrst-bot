"""
Wxrst DM Bot
------------
This bot sends a Direct Message (DM) to every member of your server
who has a specific role (like "wxrst"). You (or another admin) type
a slash command in your server, and the bot quietly messages everyone
with that role, one by one, in their DMs.

You do NOT need to touch this file to use the bot day-to-day.
You only touch the .env file to set your secret token and role name.
"""

import os
import datetime
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
from gtts import gTTS
import yt_dlp
import imageio_ffmpeg

# This gives us a working ffmpeg program bundled inside Python itself, so audio
# playback works even if the hosting server doesn't have ffmpeg installed.
FFMPEG_EXECUTABLE = imageio_ffmpeg.get_ffmpeg_exe()

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
ROLE_ID = int(os.getenv("ROLE_ID", "0"))
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

import json

CONFIG_FILE = "config.json"


def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def save_config(data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_guild_settings(guild_id: int) -> dict:
    config = load_config()
    return config.get(str(guild_id), {})


def set_guild_setting(guild_id: int, key: str, value):
    config = load_config()
    guild_key = str(guild_id)
    if guild_key not in config:
        config[guild_key] = {}
    config[guild_key][key] = value
    save_config(config)


def fill_placeholders(text: str, member: discord.Member) -> str:
    return (
        text.replace("{user}", member.mention)
        .replace("{username}", member.name)
        .replace("{server}", member.guild.name)
        .replace("{membercount}", str(member.guild.member_count))
        .replace("{ordinal}", ordinal(member.guild.member_count))
        .replace("{joindate}", member.created_at.strftime("%d/%b/%Y"))
    )


def ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ready to work!)")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"⚠️ Could not sync commands: {e}")


@bot.event
async def on_guild_join(guild: discord.Guild):
    if GUILD_ID and guild.id != GUILD_ID:
        print(f"🚪 Leaving unauthorized server: {guild.name} ({guild.id})")
        await guild.leave()


@bot.tree.interaction_check
async def block_other_servers(interaction: discord.Interaction) -> bool:
    if GUILD_ID and interaction.guild and interaction.guild.id != GUILD_ID:
        await interaction.response.send_message(
            "This bot is private and only works in its home server.", ephemeral=True
        )
        return False
    return True


@bot.tree.command(name="notify", description="DM everyone who has the special role")
@app_commands.describe(message="What do you want to tell them? (e.g. 'Come to voice chat now!')")
async def notify(interaction: discord.Interaction, message: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "Sorry, only a server admin can use this command.", ephemeral=True
        )
        return

    role = interaction.guild.get_role(ROLE_ID)
    if role is None:
        await interaction.response.send_message(
            f"I couldn't find a role with ID {ROLE_ID} in this server. "
            f"Double check the ROLE_ID value in your .env file.",
            ephemeral=True,
        )
        return

    if len(role.members) == 0:
        await interaction.response.send_message(
            f"Nobody currently has the '{role.name}' role, so there's nobody to message.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message("Sending DMs now... 📨", ephemeral=True)

    sent = 0
    failed = 0
    for member in role.members:
        if member.bot:
            continue
        try:
            await member.send(
                f"📢 **Message from {interaction.guild.name}:**\n\n{message}"
            )
            sent += 1
        except discord.Forbidden:
            failed += 1

    await interaction.followup.send(
        f"Done! ✅ Sent to **{sent}** members.\n"
        f"❌ Could not reach **{failed}** members (their DMs are probably closed).",
        ephemeral=True,
    )


class WelcomeModal(discord.ui.Modal, title="Welcome Message Setup"):
    embed_title = discord.ui.TextInput(
        label="Title",
        placeholder="WELCOME TO {server}",
        required=False,
        max_length=100,
    )
    description = discord.ui.TextInput(
        label="Description (press Enter for new lines)",
        style=discord.TextStyle.paragraph,
        placeholder="〻 WELCOME {username}\n» You joined {server}\n» {membercount} members now",
        required=False,
        max_length=1000,
    )
    ping_text = discord.ui.TextInput(
        label="Text shown above the box (optional)",
        placeholder="{user} Welcome",
        required=False,
        max_length=200,
    )
    banner_url = discord.ui.TextInput(
        label="Banner image link (optional)",
        required=False,
        max_length=300,
    )

    def __init__(self, channel: discord.TextChannel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        set_guild_setting(interaction.guild.id, "welcome_channel", self.channel.id)
        if self.embed_title.value:
            set_guild_setting(interaction.guild.id, "welcome_title", self.embed_title.value)
        if self.description.value:
            set_guild_setting(interaction.guild.id, "welcome_message", self.description.value)
        if self.ping_text.value:
            set_guild_setting(interaction.guild.id, "welcome_ping", self.ping_text.value)
        if self.banner_url.value:
            set_guild_setting(interaction.guild.id, "welcome_banner", self.banner_url.value)

        await interaction.response.send_message(
            f"✅ Welcome messages are set up in {self.channel.mention}!", ephemeral=True
        )


class GoodbyeModal(discord.ui.Modal, title="Goodbye Message Setup"):
    description = discord.ui.TextInput(
        label="Message (press Enter for new lines)",
        style=discord.TextStyle.paragraph,
        placeholder="〻 GOODBYE {username}\n» You have left {server}\n» {membercount} members remain",
        required=False,
        max_length=1000,
    )

    def __init__(self, channel: discord.TextChannel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        set_guild_setting(interaction.guild.id, "goodbye_channel", self.channel.id)
        if self.description.value:
            set_guild_setting(interaction.guild.id, "goodbye_message", self.description.value)

        await interaction.response.send_message(
            f"✅ Goodbye messages are set up in {self.channel.mention}!", ephemeral=True
        )


def resolve_channel(guild: discord.Guild, channel_input: str):
    cleaned = channel_input.strip().strip("<#>")
    if cleaned.isdigit():
        return guild.get_channel(int(cleaned))
    return None


@bot.tree.command(name="setwelcome", description="Set up a fancy welcome embed for new members")
@app_commands.describe(channel_id="The channel's ID number (right-click the channel → Copy Channel ID)")
async def setwelcome(interaction: discord.Interaction, channel_id: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only a server admin can set this up.", ephemeral=True)
        return

     channel = resolve_channel(interaction.guild, channel_id)
