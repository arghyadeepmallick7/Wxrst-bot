"""
Wxrst DM and community bot with a per-server music player.

Secrets belong in .env; this file never stores a Discord token, cookies, or API keys.
"""

import asyncio
import datetime
import html
import io
import json
import logging
import os
import random
import re
import shutil
import time
from pathlib import Path
from typing import Any, Optional

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
ROLE_ID = int(os.getenv("ROLE_ID", "0"))
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
CONFIG_FILE = "config.json"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("wxrst_bot")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


# ---------------------------------------------------------------------------
# Existing configuration helpers
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, "r", encoding="utf-8") as file:
        return json.load(file)


def save_config(data: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)


def get_guild_settings(guild_id: int) -> dict:
    return load_config().get(str(guild_id), {})


def set_guild_setting(guild_id: int, key: str, value: Any) -> None:
    config = load_config()
    guild_key = str(guild_id)
    config.setdefault(guild_key, {})[key] = value
    save_config(config)


def ordinal(n: int) -> str:
    if 11 <= n % 100 <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def fill_placeholders(text: str, member: discord.Member) -> str:
    return (
        text.replace("{user}", member.mention)
        .replace("{username}", member.name)
        .replace("{server}", member.guild.name)
        .replace("{membercount}", str(member.guild.member_count))
        .replace("{ordinal}", ordinal(member.guild.member_count))
        .replace("{joindate}", member.created_at.strftime("%d/%b/%Y"))
    )


def resolve_channel(guild: discord.Guild, channel_input: str) -> Optional[discord.abc.GuildChannel]:
    cleaned = channel_input.strip().strip("<#>")
    return guild.get_channel(int(cleaned)) if cleaned.isdigit() else None


# ---------------------------------------------------------------------------
# Music player
# ---------------------------------------------------------------------------

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "default_search": "ytsearch",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "socket_timeout": 15,
    "source_address": "0.0.0.0",
}
FFMPEG_BEFORE_OPTIONS = "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"


class MusicPlayer:
    """In-memory state for exactly one Discord guild."""

    def __init__(self) -> None:
        self.queue: list[dict[str, Any]] = []
        self.current: Optional[dict[str, Any]] = None
        self.loop_current = False
        self.volume = 0.5
        self.lock = asyncio.Lock()
        self.starting = False
        self.advance_after_stop = False
        self.announcement_channel: Optional[discord.abc.Messageable] = None


music_players: dict[int, MusicPlayer] = {}


def get_player(guild_id: int) -> MusicPlayer:
    return music_players.setdefault(guild_id, MusicPlayer())


def find_ffmpeg() -> Optional[str]:
    """Use an explicit path, system FFmpeg, then imageio-ffmpeg's bundled binary."""
    configured = os.getenv("FFMPEG_PATH")
    if configured and (os.path.isfile(configured) or shutil.which(configured)):
        return configured

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    try:
        import imageio_ffmpeg

        bundled_ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        return bundled_ffmpeg if os.path.isfile(bundled_ffmpeg) else None
    except Exception as error:  # Missing optional binary must not stop the bot.
        logger.warning("FFmpeg was not found: %s", error)
        return None


FFMPEG_EXECUTABLE = find_ffmpeg()


def format_duration(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return "Unknown/live"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


def extract_track(query: str, requester: discord.abc.User) -> dict[str, Any]:
    """Blocking yt-dlp work; callers must use asyncio.to_thread()."""
    with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
        data = ydl.extract_info(query, download=False)

    if data is None:
        raise RuntimeError("No results were returned.")
    if "entries" in data:
        data = next((entry for entry in data["entries"] if entry), None)
    if not data:
        raise RuntimeError("No playable result was returned.")

    stream_url = data.get("url")
    webpage_url = data.get("webpage_url") or data.get("original_url") or query
    if not stream_url:
        raise RuntimeError("yt-dlp did not return an audio stream for that result.")

    return {
        "id": f"{requester.id}:{data.get('id', webpage_url)}:{time.monotonic()}",
        "title": data.get("title") or "Unknown title",
        "webpage_url": webpage_url,
        "stream_url": stream_url,
        "requester_id": requester.id,
        "requester_name": getattr(requester, "display_name", str(requester)),
        "duration": data.get("duration"),
    }


async def notify_music_channel(player: MusicPlayer, message: str) -> None:
    if player.announcement_channel is None:
        return
    try:
        await player.announcement_channel.send(message)
    except (discord.HTTPException, discord.Forbidden):
        pass


async def begin_current_track(
    guild_id: int,
    voice_client: discord.VoiceClient,
    track: dict[str, Any],
) -> None:
    """Create the FFmpeg stream for a prepared track without blocking Discord."""
    player = music_players.get(guild_id)
    if player is None:
        return

    if not FFMPEG_EXECUTABLE:
        async with player.lock:
            if player.current is track:
                player.current = None
                player.starting = False
        await notify_music_channel(
            player,
            "⚠️ I cannot play music because FFmpeg is unavailable. Ask the host to install FFmpeg or set `FFMPEG_PATH`.",
        )
        return

    async with player.lock:
        if (
            player.current is not track
            or not player.starting
            or not voice_client.is_connected()
        ):
            return
        volume = player.volume

    try:
        source = discord.FFmpegPCMAudio(
            track["stream_url"],
            executable=FFMPEG_EXECUTABLE,
            before_options=FFMPEG_BEFORE_OPTIONS,
            options=FFMPEG_OPTIONS,
        )
        audio = discord.PCMVolumeTransformer(source, volume=volume)

        def after_playback(error: Optional[Exception]) -> None:
            future = asyncio.run_coroutine_threadsafe(
                after_track(guild_id, voice_client, track, error), bot.loop
            )
            future.add_done_callback(log_playback_callback_error)

        voice_client.play(audio, after=after_playback)
    except (discord.ClientException, OSError, TypeError) as error:
        logger.exception("Could not begin playback in guild %s", guild_id)
        async with player.lock:
            if player.current is track:
                player.current = None
                player.starting = False
        await notify_music_channel(player, f"⚠️ I couldn't start **{track['title']}**. Skipping it.")
        await start_next_track(guild_id, voice_client)
        return

    async with player.lock:
        if player.current is track:
            player.starting = False
    await notify_music_channel(
        player,
        f"▶️ Now playing: **{track['title']}** — requested by **{track['requester_name']}**",
    )


def log_playback_callback_error(future: "asyncio.Future[Any]") -> None:
    try:
        future.result()
    except Exception:
        logger.exception("Music playback callback failed")


async def start_next_track(guild_id: int, voice_client: discord.VoiceClient) -> None:
    player = music_players.get(guild_id)
    if player is None or not voice_client.is_connected():
        return

    async with player.lock:
        if player.starting or player.current is not None or voice_client.is_playing() or voice_client.is_paused():
            return
        if not player.queue:
            return
        track = player.queue.pop(0)
        player.current = track
        player.starting = True

    await begin_current_track(guild_id, voice_client, track)


async def after_track(
    guild_id: int,
    voice_client: discord.VoiceClient,
    finished_track: dict[str, Any],
    error: Optional[Exception],
) -> None:
    if error:
        logger.warning("Playback error in guild %s: %s", guild_id, error)

    player = music_players.get(guild_id)
    if player is None or not voice_client.is_connected():
        return

    replay_track: Optional[dict[str, Any]] = None
    start_next = False
    async with player.lock:
        if player.current is not finished_track:
            if player.advance_after_stop:
                player.advance_after_stop = False
                start_next = True
            else:
                return
        elif player.loop_current:
            replay_track = player.current
            player.starting = True
        else:
            player.current = None
            player.starting = False
            start_next = True

    if replay_track is not None:
        await begin_current_track(guild_id, voice_client, replay_track)
    elif start_next:
        await start_next_track(guild_id, voice_client)


async def connect_to_user_voice(
    interaction: discord.Interaction,
) -> tuple[Optional[discord.VoiceClient], Optional[str]]:
    """Safely return the guild voice client, joining only when there is none."""
    guild = interaction.guild
    if guild is None:
        return None, "Music commands can only be used inside a server."

    voice_client = guild.voice_client
    if voice_client and voice_client.is_connected():
        return voice_client, None

    voice_state = getattr(interaction.user, "voice", None)
    if voice_state is None or voice_state.channel is None:
        return None, "You need to join a voice channel first."

    try:
        if voice_client:
            await voice_client.disconnect(force=True)
        return await voice_state.channel.connect(timeout=20, reconnect=True), None
    except (discord.ClientException, discord.HTTPException, OSError, asyncio.TimeoutError) as error:
        logger.warning("Voice connection failed in guild %s: %s", guild.id, error)
        failed_client = guild.voice_client
        if failed_client and not failed_client.is_connected():
            try:
                await failed_client.disconnect(force=True)
            except (discord.ClientException, discord.HTTPException, OSError):
                pass
        return None, "I couldn't connect to that voice channel. Please try again in a moment."


def require_ffmpeg_message() -> str:
    return "FFmpeg is unavailable. Install FFmpeg on the server or set `FFMPEG_PATH`, then restart the bot."


# ---------------------------------------------------------------------------
# Bot lifecycle and guild restriction
# ---------------------------------------------------------------------------

@bot.event
async def on_ready() -> None:
    print(f"✅ Logged in as {bot.user} (ready to work!)")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash command(s)")
    except Exception as error:
        print(f"⚠️ Could not sync commands: {error}")
    register_ticket_views()


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    if GUILD_ID and guild.id != GUILD_ID:
        print(f"🚪 Leaving unauthorized server: {guild.name} ({guild.id})")
        await guild.leave()


@bot.tree.interaction_check
async def block_other_servers(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        await interaction.response.send_message("This bot only works inside its server.", ephemeral=True)
        return False
    if GUILD_ID and interaction.guild.id != GUILD_ID:
        await interaction.response.send_message(
            "This bot is private and only works in its home server.", ephemeral=True
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Existing notification, welcome, autorole, autonickname, and automod features
# ---------------------------------------------------------------------------

@bot.tree.command(name="notify", description="DM everyone who has the special role")
@app_commands.describe(message="What do you want to tell them? (e.g. 'Come to voice chat now!')")
async def notify(interaction: discord.Interaction, message: str) -> None:
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Sorry, only a server admin can use this command.", ephemeral=True)
        return

    role = interaction.guild.get_role(ROLE_ID)
    if role is None:
        await interaction.response.send_message(
            f"I couldn't find a role with ID {ROLE_ID} in this server. Double check ROLE_ID in .env.",
            ephemeral=True,
        )
        return
    if not role.members:
        await interaction.response.send_message(
            f"Nobody currently has the '{role.name}' role, so there's nobody to message.", ephemeral=True
        )
        return

    await interaction.response.send_message("Sending DMs now... 📨", ephemeral=True)
    sent = failed = 0
    for member in role.members:
        if member.bot:
            continue
        try:
            await member.send(f"📢 **Message from {interaction.guild.name}:**\n\n{message}")
            sent += 1
        except discord.Forbidden:
            failed += 1

    await interaction.followup.send(
        f"Done! ✅ Sent to **{sent}** members.\n❌ Could not reach **{failed}** members (their DMs are probably closed).",
        ephemeral=True,
    )


class WelcomeModal(discord.ui.Modal, title="Welcome Message Setup"):
    embed_title = discord.ui.TextInput(label="Title", placeholder="WELCOME TO {server}", required=False, max_length=100)
    description = discord.ui.TextInput(label="Description (press Enter for new lines)", style=discord.TextStyle.paragraph, placeholder="〻 WELCOME {username}\n» You joined {server}\n» {membercount} members now", required=False, max_length=1000)
    ping_text = discord.ui.TextInput(label="Text shown above the box (optional)", placeholder="{user} Welcome", required=False, max_length=200)
    banner_url = discord.ui.TextInput(label="Banner image link (optional)", required=False, max_length=300)

    def __init__(self, channel: discord.TextChannel) -> None:
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction) -> None:
        set_guild_setting(interaction.guild.id, "welcome_channel", self.channel.id)
        if self.embed_title.value:
            set_guild_setting(interaction.guild.id, "welcome_title", self.embed_title.value)
        if self.description.value:
            set_guild_setting(interaction.guild.id, "welcome_message", self.description.value)
        if self.ping_text.value:
            set_guild_setting(interaction.guild.id, "welcome_ping", self.ping_text.value)
        if self.banner_url.value:
            set_guild_setting(interaction.guild.id, "welcome_banner", self.banner_url.value)
        await interaction.response.send_message(f"✅ Welcome messages are set up in {self.channel.mention}!", ephemeral=True)


class GoodbyeModal(discord.ui.Modal, title="Goodbye Message Setup"):
    description = discord.ui.TextInput(label="Message (press Enter for new lines)", style=discord.TextStyle.paragraph, placeholder="〻 GOODBYE {username}\n» You have left {server}\n» {membercount} members remain", required=False, max_length=1000)

    def __init__(self, channel: discord.TextChannel) -> None:
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction) -> None:
        set_guild_setting(interaction.guild.id, "goodbye_channel", self.channel.id)
        if self.description.value:
            set_guild_setting(interaction.guild.id, "goodbye_message", self.description.value)
        await interaction.response.send_message(f"✅ Goodbye messages are set up in {self.channel.mention}!", ephemeral=True)


@bot.tree.command(name="setwelcome", description="Set up a fancy welcome embed for new members")
@app_commands.describe(channel_id="The channel's ID number (right-click the channel → Copy Channel ID)")
async def setwelcome(interaction: discord.Interaction, channel_id: str) -> None:
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only a server admin can set this up.", ephemeral=True)
        return
    channel = resolve_channel(interaction.guild, channel_id)
    if channel is None:
        await interaction.response.send_message("I couldn't find that channel. Paste its Channel ID and enable Developer Mode if needed.", ephemeral=True)
        return
    await interaction.response.send_modal(WelcomeModal(channel))


@bot.tree.command(name="setgoodbye", description="Set the channel and message for when members leave")
@app_commands.describe(channel_id="The channel's ID number (right-click the channel → Copy Channel ID)")
async def setgoodbye(interaction: discord.Interaction, channel_id: str) -> None:
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only a server admin can set this up.", ephemeral=True)
        return
    channel = resolve_channel(interaction.guild, channel_id)
    if channel is None:
        await interaction.response.send_message("I couldn't find that channel. Paste its Channel ID and enable Developer Mode if needed.", ephemeral=True)
        return
    await interaction.response.send_modal(GoodbyeModal(channel))


@bot.tree.command(name="setautorole", description="Automatically give new members a role when they join")
@app_commands.describe(role="The role to give automatically (leave empty to turn autorole off)")
async def setautorole(interaction: discord.Interaction, role: Optional[discord.Role] = None) -> None:
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only a server admin can set this up.", ephemeral=True)
        return
    if role is None:
        set_guild_setting(interaction.guild.id, "autorole_id", None)
        await interaction.response.send_message("✅ Autorole turned off.", ephemeral=True)
        return
    if role >= interaction.guild.me.top_role:
        await interaction.response.send_message(f"⚠️ I can't assign **{role.name}** because it is higher than my role. Move my role above it.", ephemeral=True)
        return
    set_guild_setting(interaction.guild.id, "autorole_id", role.id)
    await interaction.response.send_message(f"✅ New members will automatically get the **{role.name}** role.", ephemeral=True)


@bot.tree.command(name="setautonickname", description="Automatically set a nickname format for new members")
@app_commands.describe(format="Use {username}, e.g. 'New | {username}'. Leave empty to turn off.")
async def setautonickname(interaction: discord.Interaction, format: Optional[str] = None) -> None:
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only a server admin can set this up.", ephemeral=True)
        return
    set_guild_setting(interaction.guild.id, "autonickname_format", format)
    message = f"✅ New members will be renamed using: `{format}`" if format else "✅ Autonickname turned off."
    await interaction.response.send_message(message, ephemeral=True)


@bot.tree.command(name="automod", description="Turn the bad-word and spam filter on or off")
@app_commands.describe(state="Turn automod on or off")
@app_commands.choices(state=[app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")])
async def automod(interaction: discord.Interaction, state: app_commands.Choice[str]) -> None:
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only a server admin can set this up.", ephemeral=True)
        return
    set_guild_setting(interaction.guild.id, "automod_enabled", state.value == "on")
    await interaction.response.send_message(f"✅ Automod is now **{state.value}**.", ephemeral=True)


@bot.tree.command(name="addbadword", description="Add a word for automod to delete automatically")
@app_commands.describe(word="The word to block")
async def addbadword(interaction: discord.Interaction, word: str) -> None:
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only a server admin can set this up.", ephemeral=True)
        return
    settings = get_guild_settings(interaction.guild.id)
    bad_words = settings.get("bad_words", [])
    word_lower = word.lower()
    if word_lower not in bad_words:
        bad_words.append(word_lower)
        set_guild_setting(interaction.guild.id, "bad_words", bad_words)
    await interaction.response.send_message(f"✅ Added `{word}` to the blocked word list.", ephemeral=True)


@bot.tree.command(name="removebadword", description="Remove a word from automod's blocked list")
@app_commands.describe(word="The word to unblock")
async def removebadword(interaction: discord.Interaction, word: str) -> None:
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only a server admin can set this up.", ephemeral=True)
        return
    settings = get_guild_settings(interaction.guild.id)
    bad_words = settings.get("bad_words", [])
    word_lower = word.lower()
    if word_lower in bad_words:
        bad_words.remove(word_lower)
        set_guild_setting(interaction.guild.id, "bad_words", bad_words)
        await interaction.response.send_message(f"✅ Removed `{word}` from the blocked word list.", ephemeral=True)
    else:
        await interaction.response.send_message("That word wasn't on the list.", ephemeral=True)


recent_messages: dict[tuple[int, int], list[float]] = {}


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or message.guild is None:
        return

    settings = get_guild_settings(message.guild.id)
    if settings.get("automod_enabled") and not message.author.guild_permissions.administrator:
        bad_words = settings.get("bad_words", [])
        if any(word in message.content.lower() for word in bad_words):
            try:
                await message.delete()
                await message.channel.send(f"🚫 {message.author.mention}, that word isn't allowed here.", delete_after=5)
            except discord.Forbidden:
                pass
            return

        key = (message.guild.id, message.author.id)
        now = discord.utils.utcnow().timestamp()
        timestamps = [stamp for stamp in recent_messages.get(key, []) if now - stamp < 5]
        timestamps.append(now)
        recent_messages[key] = timestamps
        if len(timestamps) > 5:
            try:
                await message.delete()
                await message.channel.send(f"🚫 {message.author.mention}, please slow down (you're sending messages too fast).", delete_after=5)
            except discord.Forbidden:
                pass
            return

    await bot.process_commands(message)


@bot.event
async def on_member_join(member: discord.Member) -> None:
    settings = get_guild_settings(member.guild.id)
    autorole_id = settings.get("autorole_id")
    if autorole_id:
        role = member.guild.get_role(autorole_id)
        if role:
            try:
                await member.add_roles(role, reason="Autorole")
            except discord.Forbidden:
                pass

    nickname_format = settings.get("autonickname_format")
    if nickname_format:
        try:
            await member.edit(nick=fill_placeholders(nickname_format, member)[:32], reason="Autonickname")
        except discord.Forbidden:
            pass

    channel_id = settings.get("welcome_channel")
    if not channel_id or (channel := member.guild.get_channel(channel_id)) is None:
        return
    title_template = settings.get("welcome_title", "WELCOME TO {server}")
    description_template = settings.get("welcome_message", "• Welcome To **{server}**\n⚠️ Enjoy Ur Stay Here\n➤ {user}\n➤ {username}\n➤ Acc Created : {joindate}")
    embed = discord.Embed(title=fill_placeholders(title_template, member), description=fill_placeholders(description_template, member), color=discord.Color.purple())
    embed.set_thumbnail(url=member.display_avatar.url)
    if banner_url := settings.get("welcome_banner"):
        embed.set_image(url=banner_url)
    embed.set_footer(text=f"{ordinal(member.guild.member_count)} member!")
    embed.timestamp = discord.utils.utcnow()
    await channel.send(content=fill_placeholders(settings.get("welcome_ping", "{user} Welcome"), member), embed=embed)


@bot.event
async def on_member_remove(member: discord.Member) -> None:
    # Departure DMs run independently so the existing goodbye system remains fast.
    asyncio.create_task(handle_member_departure(member))
    settings = get_guild_settings(member.guild.id)
    channel_id = settings.get("goodbye_channel")
    if not channel_id or (channel := member.guild.get_channel(channel_id)) is None:
        return
    template = settings.get("goodbye_message", "〻 GOODBYE {username}\n» You have left {server}\n» Thanks for being part of WXRST\n» {membercount} members remain")
    embed = discord.Embed(description=fill_placeholders(template, member), color=discord.Color.red())
    embed.set_thumbnail(url=member.display_avatar.url)
    await channel.send(embed=embed)


# ---------------------------------------------------------------------------
# Existing moderation features
# ---------------------------------------------------------------------------

@bot.tree.command(name="kick", description="Kick a member from the server")
@app_commands.describe(member="Who to kick", reason="Why are you kicking them?")
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given") -> None:
    if not interaction.user.guild_permissions.kick_members:
        await interaction.response.send_message("You don't have permission to kick members.", ephemeral=True)
        return
    if member.top_role >= interaction.user.top_role and interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("You can't kick someone with an equal or higher role than you.", ephemeral=True)
        return
    try:
        await member.kick(reason=f"{reason} (by {interaction.user})")
        await interaction.response.send_message(f"👢 Kicked **{member}**. Reason: {reason}")
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to kick that member.", ephemeral=True)


@bot.tree.command(name="ban", description="Ban a member from the server")
@app_commands.describe(member="Who to ban", reason="Why are you banning them?")
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given") -> None:
    if not interaction.user.guild_permissions.ban_members:
        await interaction.response.send_message("You don't have permission to ban members.", ephemeral=True)
        return
    if member.top_role >= interaction.user.top_role and interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("You can't ban someone with an equal or higher role than you.", ephemeral=True)
        return
    try:
        await member.ban(reason=f"{reason} (by {interaction.user})")
        await interaction.response.send_message(f"🔨 Banned **{member}**. Reason: {reason}")
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to ban that member.", ephemeral=True)


@bot.tree.command(name="timeout", description="Temporarily mute a member (they can't send messages)")
@app_commands.describe(member="Who to timeout", minutes="How many minutes", reason="Why are you timing them out?")
async def timeout(interaction: discord.Interaction, member: discord.Member, minutes: int, reason: str = "No reason given") -> None:
    if not interaction.user.guild_permissions.moderate_members:
        await interaction.response.send_message("You don't have permission to timeout members.", ephemeral=True)
        return
    duration = discord.utils.utcnow() + datetime.timedelta(minutes=minutes)
    try:
        await member.edit(timed_out_until=duration, reason=f"{reason} (by {interaction.user})")
        await interaction.response.send_message(f"🔇 Timed out **{member}** for {minutes} minute(s). Reason: {reason}")
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to timeout that member.", ephemeral=True)


@bot.tree.command(name="warn", description="Give a member a warning (saved in their record)")
@app_commands.describe(member="Who to warn", reason="Why are you warning them?")
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str) -> None:
    if not interaction.user.guild_permissions.moderate_members:
        await interaction.response.send_message("You don't have permission to warn members.", ephemeral=True)
        return
    config = load_config()
    guild_key = str(interaction.guild.id)
    config.setdefault(guild_key, {}).setdefault("warnings", {}).setdefault(str(member.id), []).append(reason)
    save_config(config)
    count = len(config[guild_key]["warnings"][str(member.id)])
    await interaction.response.send_message(f"⚠️ Warned **{member}** (warning #{count}). Reason: {reason}")


@bot.tree.command(name="warnings", description="See a member's past warnings")
@app_commands.describe(member="Whose warnings to check")
async def warnings(interaction: discord.Interaction, member: discord.Member) -> None:
    member_warnings = get_guild_settings(interaction.guild.id).get("warnings", {}).get(str(member.id), [])
    if not member_warnings:
        await interaction.response.send_message(f"**{member}** has no warnings.", ephemeral=True)
        return
    text = "\n".join(f"{index + 1}. {reason}" for index, reason in enumerate(member_warnings))
    await interaction.response.send_message(f"⚠️ Warnings for **{member}**:\n{text}", ephemeral=True)


@bot.tree.command(name="clear", description="Delete a number of recent messages in this channel")
@app_commands.describe(amount="How many messages to delete (max 100)")
async def clear(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]) -> None:
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("You don't have permission to delete messages.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"🧹 Deleted {len(deleted)} message(s).", ephemeral=True)


# ---------------------------------------------------------------------------
# Music slash commands
# ---------------------------------------------------------------------------

@bot.tree.command(name="join", description="Join your current voice channel")
async def join(interaction: discord.Interaction) -> None:
    existing = interaction.guild.voice_client
    if existing and existing.is_connected():
        await interaction.response.send_message(f"I'm already in {existing.channel.mention}.", ephemeral=True)
        return
    voice_client, error = await connect_to_user_voice(interaction)
    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return
    await interaction.response.send_message(f"✅ Joined {voice_client.channel.mention}.", ephemeral=True)


async def leave_voice_channel(interaction: discord.Interaction) -> None:
    voice_client = interaction.guild.voice_client
    if voice_client is None or not voice_client.is_connected():
        await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
        return
    player = music_players.pop(interaction.guild.id, None)
    if player:
        async with player.lock:
            player.queue.clear()
            player.current = None
            player.starting = False
            player.advance_after_stop = False
    try:
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()
        await voice_client.disconnect(force=True)
        await interaction.response.send_message("👋 Left the voice channel and cleared the music queue.", ephemeral=True)
    except (discord.ClientException, discord.HTTPException, OSError) as error:
        logger.warning("Voice disconnect failed in guild %s: %s", interaction.guild.id, error)
        await interaction.response.send_message("I couldn't disconnect cleanly, but the music queue was reset.", ephemeral=True)


@bot.tree.command(name="leave", description="Leave voice, stop music, and clear the queue")
async def leave(interaction: discord.Interaction) -> None:
    await leave_voice_channel(interaction)


# These aliases were present in the existing bot and are kept for compatibility.
@bot.tree.command(name="disconnect", description="Leave voice, stop music, and clear the queue")
async def disconnect(interaction: discord.Interaction) -> None:
    await leave_voice_channel(interaction)


@bot.tree.command(name="leavevc", description="Leave voice, stop music, and clear the queue")
async def leavevc(interaction: discord.Interaction) -> None:
    await leave_voice_channel(interaction)


@bot.tree.command(name="play", description="Play a YouTube URL or search YouTube")
@app_commands.describe(query="A YouTube URL or search text")
async def play(interaction: discord.Interaction, query: str) -> None:
    if not FFMPEG_EXECUTABLE:
        await interaction.response.send_message(require_ffmpeg_message(), ephemeral=True)
        return
    voice_client, error = await connect_to_user_voice(interaction)
    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    try:
        track = await asyncio.to_thread(extract_track, query, interaction.user)
    except Exception as extraction_error:
        logger.warning("yt-dlp extraction failed for %r: %s", query, extraction_error, exc_info=True)
        await interaction.followup.send(
            "⚠️ I couldn't get audio for that YouTube result. It may be unavailable, private, age-restricted, or YouTube may be blocking requests. Try another link or search.",
            ephemeral=True,
        )
        return

    player = get_player(interaction.guild.id)
    player.announcement_channel = interaction.channel
    async with player.lock:
        busy = player.current is not None or player.starting or voice_client.is_playing() or voice_client.is_paused()
        if busy:
            player.queue.append(track)
            position = len(player.queue)
        else:
            player.current = track
            player.starting = True
            position = 0

    if position:
        await interaction.followup.send(f"➕ Added **{track['title']}** to the queue at position **{position}** — requested by **{track['requester_name']}**.")
        return

    await interaction.followup.send(f"▶️ Preparing **{track['title']}** — requested by **{track['requester_name']}**.")
    await begin_current_track(interaction.guild.id, voice_client, track)


@bot.tree.command(name="pause", description="Pause the current music")
async def pause(interaction: discord.Interaction) -> None:
    voice_client = interaction.guild.voice_client
    if voice_client is None or not voice_client.is_playing():
        await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
        return
    voice_client.pause()
    await interaction.response.send_message("⏸️ Paused.")


@bot.tree.command(name="resume", description="Resume paused music")
async def resume(interaction: discord.Interaction) -> None:
    voice_client = interaction.guild.voice_client
    if voice_client is None or not voice_client.is_paused():
        await interaction.response.send_message("Nothing is paused right now.", ephemeral=True)
        return
    voice_client.resume()
    await interaction.response.send_message("▶️ Resumed.")


@bot.tree.command(name="skip", description="Skip the current song")
async def skip(interaction: discord.Interaction) -> None:
    voice_client = interaction.guild.voice_client
    player = music_players.get(interaction.guild.id)
    if voice_client is None or player is None or not voice_client.is_playing():
        await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
        return
    async with player.lock:
        player.current = None
        player.starting = False
        player.advance_after_stop = True
    voice_client.stop()
    await interaction.response.send_message("⏭️ Skipped.")


@bot.tree.command(name="stop", description="Stop music and clear the queue")
async def stop(interaction: discord.Interaction) -> None:
    player = music_players.get(interaction.guild.id)
    voice_client = interaction.guild.voice_client
    if player is None or (player.current is None and not player.queue):
        await interaction.response.send_message("There is no music to stop.", ephemeral=True)
        return
    async with player.lock:
        player.queue.clear()
        player.current = None
        player.starting = False
        player.advance_after_stop = False
    if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
        voice_client.stop()
    await interaction.response.send_message("⏹️ Stopped and cleared the queue.")


@bot.tree.command(name="queue", description="Show the current music queue")
async def queue(interaction: discord.Interaction) -> None:
    player = music_players.get(interaction.guild.id)
    embed = discord.Embed(title="Music Queue", color=discord.Color.blurple())
    if player is None:
        embed.description = "The queue is empty. Use `/play` to add a song."
    else:
        async with player.lock:
            current = player.current
            waiting = list(player.queue)
        if current:
            embed.add_field(name="Currently Playing", value=f"[{current['title']}]({current['webpage_url']})\nRequested by {current['requester_name']}", inline=False)
        else:
            embed.add_field(name="Currently Playing", value="Nothing", inline=False)
        if waiting:
            lines = [f"{index}. **{track['title']}** — {track['requester_name']}" for index, track in enumerate(waiting, 1)]
            embed.add_field(name="Up Next", value="\n".join(lines[:15]), inline=False)
            if len(lines) > 15:
                embed.set_footer(text=f"Showing 15 of {len(lines)} waiting songs")
        else:
            embed.add_field(name="Up Next", value="No songs waiting.", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="nowplaying", description="Show the current song")
async def nowplaying(interaction: discord.Interaction) -> None:
    player = music_players.get(interaction.guild.id)
    current = player.current if player else None
    if current is None:
        await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
        return
    embed = discord.Embed(title="Now Playing", description=f"[{current['title']}]({current['webpage_url']})", color=discord.Color.green())
    embed.add_field(name="Requested by", value=current["requester_name"])
    embed.add_field(name="Duration", value=format_duration(current["duration"]))
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="volume", description="Set music volume from 0 to 100")
@app_commands.describe(percent="Volume from 0 to 100")
async def volume(interaction: discord.Interaction, percent: int) -> None:
    if not 0 <= percent <= 100:
        await interaction.response.send_message("Volume must be between 0 and 100.", ephemeral=True)
        return
    player = get_player(interaction.guild.id)
    async with player.lock:
        player.volume = percent / 100
    voice_client = interaction.guild.voice_client
    if voice_client and isinstance(voice_client.source, discord.PCMVolumeTransformer):
        voice_client.source.volume = player.volume
    await interaction.response.send_message(f"🔊 Volume set to **{percent}%**.")


@bot.tree.command(name="shuffle", description="Shuffle the waiting music queue")
async def shuffle(interaction: discord.Interaction) -> None:
    player = music_players.get(interaction.guild.id)
    if player is None:
        await interaction.response.send_message("There are no waiting songs to shuffle.", ephemeral=True)
        return
    async with player.lock:
        if len(player.queue) < 2:
            await interaction.response.send_message("I need at least two waiting songs to shuffle.", ephemeral=True)
            return
        random.shuffle(player.queue)
    await interaction.response.send_message("🔀 Shuffled the waiting queue.")


@bot.tree.command(name="remove", description="Remove a waiting song by its queue position")
@app_commands.describe(position="The waiting-song position shown by /queue")
async def remove(interaction: discord.Interaction, position: int) -> None:
    player = music_players.get(interaction.guild.id)
    if player is None:
        await interaction.response.send_message("The queue is empty.", ephemeral=True)
        return
    async with player.lock:
        if position < 1 or position > len(player.queue):
            await interaction.response.send_message("That queue position does not exist.", ephemeral=True)
            return
        removed = player.queue.pop(position - 1)
    await interaction.response.send_message(f"🗑️ Removed **{removed['title']}** from the queue.")


@bot.tree.command(name="loop", description="Toggle looping the current song")
async def loop(interaction: discord.Interaction) -> None:
    player = music_players.get(interaction.guild.id)
    if player is None or player.current is None:
        await interaction.response.send_message("Nothing is playing to loop.", ephemeral=True)
        return
    async with player.lock:
        player.loop_current = not player.loop_current
        enabled = player.loop_current
    await interaction.response.send_message("🔁 Current-song looping is now **on**." if enabled else "➡️ Current-song looping is now **off**.")


@bot.tree.command(name="musichelp", description="Show music command help")
async def musichelp(interaction: discord.Interaction) -> None:
    embed = discord.Embed(title="Music Commands", description="Use `/play` with a YouTube URL or search text.", color=discord.Color.blurple())
    embed.add_field(name="Playback", value="`/join` `/leave` `/play` `/pause` `/resume` `/skip` `/stop`", inline=False)
    embed.add_field(name="Queue", value="`/queue` `/nowplaying` `/shuffle` `/remove` `/loop`", inline=False)
    embed.add_field(name="Sound", value="`/volume` `/musichelp`", inline=False)
    embed.set_footer(text="/remove uses the position under Up Next; the current song is never removed by it.")
    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# Configurable member departure DMs
# ---------------------------------------------------------------------------

DEPARTURE_DEFAULTS = {
    "enabled": False,
    "server_invite": "",
    "send_leave": True,
    "send_kick": True,
    "send_ban": True,
    "voluntary_leave_message": (
        "⚡︎ **WXRST**\n\n"
        "〻 We noticed that you left **{SERVER_NAME}**.\n\n"
        "[LEAVE MESSAGE WILL BE DECIDED LATER]\n\n"
        "» If you'd like to come back:\n`{SERVER_INVITE}`\n\n"
        "𑣲 **You're always welcome back.**"
    ),
    "kicked_message": (
        "⤫ **WXRST**\n\n"
        "〻 You have been **kicked** from `{SERVER_NAME}`.\n\n"
        "[KICK MESSAGE WILL BE DECIDED LATER]\n\n"
        "» Server link:\n`{SERVER_INVITE}`"
    ),
    "banned_message": (
        "⤫ **WXRST**\n\n"
        "〻 Hey **{DISPLAY_NAME}**, you have been **banned** from **{SERVER_NAME}**.\n\n"
        "» If you believe this action was made in error, you may contact the server administration.\n\n"
        "𑣲 **Server Link**\n"
        "» `{SERVER_INVITE}`\n\n"
        "~~You are currently unable to access the server.~~"
    ),
}
recent_departure_bans: dict[tuple[int, int], float] = {}
sent_departure_dms: dict[tuple[int, int], float] = {}


def departure_settings(guild_id: int) -> dict[str, Any]:
    saved = get_guild_settings(guild_id).get("departure_dm", {})
    return {**DEPARTURE_DEFAULTS, **saved}


def save_departure_settings(guild_id: int, settings: dict[str, Any]) -> None:
    set_guild_setting(guild_id, "departure_dm", settings)


def departure_template(member: discord.abc.User, guild: discord.Guild, settings: dict[str, Any], departure_type: str) -> str:
    template_key = {"leave": "voluntary_leave_message", "kick": "kicked_message", "ban": "banned_message"}[departure_type]
    template = settings.get(template_key, DEPARTURE_DEFAULTS[template_key])
    replacements = {
        "{USER}": member.mention,
        "{USERNAME}": member.name,
        "{DISPLAY_NAME}": getattr(member, "display_name", member.name),
        "{SERVER_NAME}": guild.name,
        "{SERVER_ID}": str(guild.id),
        "{SERVER_INVITE}": settings.get("server_invite", ""),
    }
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    return format_departure_message(template)


def format_departure_message(template: str) -> str:
    """Keep configured wording but add readable paragraph gaps around WXRST sections."""
    formatted = template.replace("\r\n", "\n").strip()

    # Add a blank line before each section marker
    for marker in ("〻", "»", "𑣲", "⤫", "⚡︎"):
        formatted = re.sub(rf"(?<!\n)\s*{re.escape(marker)}", f"\n\n{marker}", formatted)

    # Keep a "𑣲 Label" line glued to the "» Value" line right after it,
    # instead of letting them get pushed into separate paragraphs
    formatted = re.sub(r"(𑣲[^\n]*)\n\n(»)", r"\1\n\2", formatted)

    # Strikethrough spans (~~like this~~) should never get split in half —
    # only add a break before the whole span, never before its closing ~~
    formatted = re.sub(r"(?<!\n)\s*(~~[^~\n]+~~)", r"\n\n\1", formatted)

    # Clean up: never allow more than one blank line in a row, and never
    # start or end the message with blank lines
    formatted = re.sub(r"\n{3,}", "\n\n", formatted).strip()

    return formatted


def departure_dm_allowed(settings: dict[str, Any], departure_type: str) -> bool:
    return bool(settings.get("enabled")) and bool(settings.get(f"send_{departure_type}", True))


async def send_departure_dm(member: discord.abc.User, guild: discord.Guild, departure_type: str) -> None:
    """Send one configurable DM at most once per member departure."""
    settings = departure_settings(guild.id)
    if not departure_dm_allowed(settings, departure_type):
        return
    key = (guild.id, member.id)
    now = time.monotonic()
    previous = sent_departure_dms.get(key)
    if previous and now - previous < 120:
        return
    sent_departure_dms[key] = now
    try:
        await member.send(departure_template(member, guild, settings, departure_type))
        logger.info("Sent %s departure DM to %s in guild %s", departure_type, member.id, guild.id)
    except (discord.Forbidden, discord.HTTPException) as error:
        logger.info("Could not deliver %s departure DM to %s: %s", departure_type, member.id, error)


async def handle_member_departure(member: discord.Member) -> None:
    """Classify a removal as ban, kick, or voluntary leave without blocking events."""
    guild, key = member.guild, (member.guild.id, member.id)
    await asyncio.sleep(2)  # Audit entries can arrive after on_member_remove.
    now = time.monotonic()
    if recent_departure_bans.get(key, 0) > now - 30:
        return  # on_member_ban sends the ban template.

    departure_type = "leave"
    try:
        async for entry in guild.audit_logs(limit=6, action=discord.AuditLogAction.kick):
            target = getattr(entry, "target", None)
            if target and target.id == member.id and (discord.utils.utcnow() - entry.created_at).total_seconds() < 20:
                departure_type = "kick"
                break
    except (discord.Forbidden, discord.HTTPException) as error:
        # Without audit-log access, the safe fallback is a voluntary departure.
        logger.info("Could not inspect kick audit log in guild %s: %s", guild.id, error)
    await send_departure_dm(member, guild, departure_type)


@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User) -> None:
    recent_departure_bans[(guild.id, user.id)] = time.monotonic()
    await send_departure_dm(user, guild, "ban")


departure_group = app_commands.Group(name="departure", description="Configure member departure DMs")


def require_departure_admin(interaction: discord.Interaction) -> bool:
    return isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator


@departure_group.command(name="config", description="Show the current departure DM configuration")
async def departure_config(interaction: discord.Interaction) -> None:
    if not require_departure_admin(interaction):
        await interaction.response.send_message("Only administrators can configure departure DMs.", ephemeral=True)
        return
    settings = departure_settings(interaction.guild.id)
    embed = discord.Embed(title="Member Departure DM Configuration", color=discord.Color.blurple())
    embed.description = (
        f"**System:** {'Enabled' if settings['enabled'] else 'Disabled'}\n"
        f"**Voluntary leave DMs:** {'On' if settings['send_leave'] else 'Off'}\n"
        f"**Kick DMs:** {'On' if settings['send_kick'] else 'Off'}\n"
        f"**Ban DMs:** {'On' if settings['send_ban'] else 'Off'}\n"
        f"**Invite configured:** {'Yes' if settings['server_invite'] else 'No'}"
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@departure_group.command(name="enable", description="Enable member departure DMs")
async def departure_enable(interaction: discord.Interaction) -> None:
    if not require_departure_admin(interaction):
        await interaction.response.send_message("Only administrators can configure departure DMs.", ephemeral=True)
        return
    settings = departure_settings(interaction.guild.id)
    settings["enabled"] = True
    save_departure_settings(interaction.guild.id, settings)
    await interaction.response.send_message("✅ Member departure DMs are enabled.", ephemeral=True)


@departure_group.command(name="disable", description="Disable member departure DMs")
async def departure_disable(interaction: discord.Interaction) -> None:
    if not require_departure_admin(interaction):
        await interaction.response.send_message("Only administrators can configure departure DMs.", ephemeral=True)
        return
    settings = departure_settings(interaction.guild.id)
    settings["enabled"] = False
    save_departure_settings(interaction.guild.id, settings)
    await interaction.response.send_message("✅ Member departure DMs are disabled.", ephemeral=True)


@departure_group.command(name="setinvite", description="Set the invite included in departure DMs")
@app_commands.describe(invite="Full Discord invite URL or invite code")
async def departure_setinvite(interaction: discord.Interaction, invite: str) -> None:
    if not require_departure_admin(interaction):
        await interaction.response.send_message("Only administrators can configure departure DMs.", ephemeral=True)
        return
    cleaned = invite.strip()
    if not cleaned:
        await interaction.response.send_message("Provide a valid server invite.", ephemeral=True)
        return
    if not cleaned.startswith("http"):
        cleaned = f"https://discord.gg/{cleaned.removeprefix('discord.gg/')}"
    settings = departure_settings(interaction.guild.id)
    settings["server_invite"] = cleaned
    save_departure_settings(interaction.guild.id, settings)
    await interaction.response.send_message("✅ Departure DM invite saved.", ephemeral=True)


DEPARTURE_MESSAGE_CHOICES = [
    app_commands.Choice(name="Voluntary leave", value="leave"),
    app_commands.Choice(name="Kicked", value="kick"),
    app_commands.Choice(name="Banned", value="ban"),
]
DEPARTURE_SETTING_KEYS = {
    "leave": "voluntary_leave_message",
    "kick": "kicked_message",
    "ban": "banned_message",
}


class DepartureMessageModal(discord.ui.Modal, title="Departure Message"):
    """A popup form with a real multi-line box, so line breaks actually work."""

    message = discord.ui.TextInput(
        label="Message (press Enter for new lines)",
        style=discord.TextStyle.paragraph,
        placeholder="Supports {USER} {USERNAME} {DISPLAY_NAME} {SERVER_NAME} {SERVER_ID} {SERVER_INVITE}",
        max_length=2000,
    )

    def __init__(self, settings_key: str, label: str, current_value: str):
        super().__init__()
        self.settings_key = settings_key
        self.label = label
        self.message.default = current_value

    async def on_submit(self, interaction: discord.Interaction):
        settings = departure_settings(interaction.guild.id)
        settings[self.settings_key] = self.message.value[:2000]
        save_departure_settings(interaction.guild.id, settings)
        await interaction.response.send_message(f"✅ {self.label} departure message saved.", ephemeral=True)


@departure_group.command(name="setmessage", description="Set a departure DM message template")
@app_commands.choices(message_type=DEPARTURE_MESSAGE_CHOICES)
@app_commands.describe(message_type="Departure situation")
async def departure_setmessage(interaction: discord.Interaction, message_type: app_commands.Choice[str]) -> None:
    if not require_departure_admin(interaction):
        await interaction.response.send_message("Only administrators can configure departure DMs.", ephemeral=True)
        return
    settings_key = DEPARTURE_SETTING_KEYS[message_type.value]
    settings = departure_settings(interaction.guild.id)
    current_value = settings.get(settings_key, DEPARTURE_DEFAULTS[settings_key])
    await interaction.response.send_modal(DepartureMessageModal(settings_key, message_type.name, current_value))


@departure_group.command(name="settings", description="Choose which departure types send DMs")
async def departure_settings_command(interaction: discord.Interaction, voluntary_leave: bool = True, kicked: bool = True, banned: bool = True) -> None:
    if not require_departure_admin(interaction):
        await interaction.response.send_message("Only administrators can configure departure DMs.", ephemeral=True)
        return
    settings = departure_settings(interaction.guild.id)
    settings.update({"send_leave": voluntary_leave, "send_kick": kicked, "send_ban": banned})
    save_departure_settings(interaction.guild.id, settings)
    await interaction.response.send_message("✅ Departure-type settings saved.", ephemeral=True)


@departure_group.command(name="test", description="DM yourself a safe preview of a departure template")
@app_commands.choices(message_type=DEPARTURE_MESSAGE_CHOICES)
async def departure_test(interaction: discord.Interaction, message_type: app_commands.Choice[str]) -> None:
    if not require_departure_admin(interaction):
        await interaction.response.send_message("Only administrators can test departure DMs.", ephemeral=True)
        return
    settings = departure_settings(interaction.guild.id)
    try:
        await interaction.user.send(departure_template(interaction.user, interaction.guild, settings, message_type.value))
        await interaction.response.send_message("✅ Test DM sent to you.", ephemeral=True)
    except (discord.Forbidden, discord.HTTPException):
        preview = departure_template(interaction.user, interaction.guild, settings, message_type.value)
        await interaction.response.send_message(f"I couldn't DM you. Preview:\n\n{preview}", ephemeral=True)


bot.tree.add_command(departure_group)


# ---------------------------------------------------------------------------
# Persistent WXRST ticket system
# ---------------------------------------------------------------------------

DEFAULT_TICKET_CATEGORIES = {
    "general": {"label": "General", "emoji": "💬"},
    "team-vs-team": {"label": "Team Vs Team", "emoji": "⚔️"},
    "giveaway-ping": {"label": "Giveaway Ping", "emoji": "🎉"},
    "staff-apply": {"label": "Staff Apply", "emoji": "🛡️"},
    "ally": {"label": "Ally", "emoji": "🤝"},
    "team-apply": {"label": "Team Apply", "emoji": "👥"},
}
REMOVED_TICKET_CATEGORY_KEYS = {
    "support",
    "application",
    "purchase",
    "report",
    "partnership",
    "other",
}
DEFAULT_TICKET_PANEL_DESCRIPTION = (
    "〻 **Select the ticket type you want to open:**\n\n"
    "» 💬 **General** — General questions, inquiries, or assistance.\n\n"
    "» ⚔️ **Team Vs Team** — Organize or discuss `Team Vs Team` matches.\n\n"
    "» 🎉 **Giveaway Ping** — Claim your giveaway prize or contact staff about a giveaway reward.\n\n"
    "» 🛡️ **Staff Apply** — Apply to become a part of the `WXRST` staff team.\n\n"
    "» 🤝 **Ally** — Alliance requests and partnership inquiries.\n\n"
    "» 👥 **Team Apply** — Apply to join the `WXRST` team and represent the name.\n\n"
    "⤫ **Please select the appropriate option below.**\n\n"
    "~~Do not open unnecessary tickets.~~\n\n"
    "𑣲 **One Team. One Family. One WXRST.** ⚡︎"
)
TICKET_TRANSCRIPT_DIR = Path("transcripts")
ticket_creation_locks: dict[int, asyncio.Lock] = {}
registered_ticket_views: set[int] = set()


def default_ticket_settings() -> dict[str, Any]:
    return {
        "panel_title": "⚡︎ **WXRST TICKET SERVICE** 🎟️",
        "panel_description": DEFAULT_TICKET_PANEL_DESCRIPTION,
        "panel_banner": None,
        "ticket_category_id": None,
        "transcript_channel_id": None,
        "log_channel_id": None,
        "support_role_ids": [],
        "max_open_per_user": 1,
        "creation_cooldown_seconds": 30,
        "auto_delete_minutes": 0,
        "naming_format": "{category}-{username}",
        "claiming_enabled": True,
        "reopening_enabled": True,
        "user_management_enabled": True,
        "default_ticket_message": "Please explain your issue clearly and provide screenshots or evidence if necessary. A WXRST staff member will assist you shortly.",
        "categories": DEFAULT_TICKET_CATEGORIES.copy(),
        "next_ticket_number": 1,
        "tickets": {},
        "ticket_stats": {"staff": {}},
    }


def ticket_settings(guild_id: int) -> dict[str, Any]:
    settings = get_guild_settings(guild_id).get("ticket_system", {})
    merged = default_ticket_settings()
    merged.update(settings)
    if str(settings.get("panel_description", "")).startswith("〻 **Select the ticket type you want to open:**"):
        merged["panel_description"] = DEFAULT_TICKET_PANEL_DESCRIPTION
    configured_categories = {
        key: value
        for key, value in settings.get("categories", {}).items()
        if key not in REMOVED_TICKET_CATEGORY_KEYS
    }
    merged["categories"] = {**DEFAULT_TICKET_CATEGORIES, **configured_categories}
    merged.setdefault("tickets", {})
    merged.setdefault("ticket_stats", {"staff": {}})
    merged["ticket_stats"].setdefault("staff", {})
    return merged


def save_ticket_settings(guild_id: int, settings: dict[str, Any]) -> None:
    categories = settings.get("categories", {})
    settings["categories"] = {
        key: value for key, value in categories.items() if key not in REMOVED_TICKET_CATEGORY_KEYS
    }
    set_guild_setting(guild_id, "ticket_system", settings)


def get_ticket(guild_id: int, ticket_id: str) -> Optional[dict[str, Any]]:
    return ticket_settings(guild_id).get("tickets", {}).get(ticket_id)


def is_ticket_staff(member: discord.Member, settings: dict[str, Any]) -> bool:
    if member.guild_permissions.administrator:
        return True
    support_roles = {int(role_id) for role_id in settings.get("support_role_ids", [])}
    return any(role.id in support_roles for role in member.roles)


def is_ticket_creator(member: discord.Member, ticket: dict[str, Any]) -> bool:
    return member.id == ticket.get("creator_id")


def sanitize_ticket_name(value: str) -> str:
    clean = "".join(char.lower() if char.isalnum() else "-" for char in value)
    clean = "-".join(part for part in clean.split("-") if part)
    return clean[:70] or "member"


def ticket_channel_name(ticket: dict[str, Any], settings: dict[str, Any], closed: bool = False) -> str:
    category = sanitize_ticket_name(ticket["category_key"])
    username = sanitize_ticket_name(ticket.get("creator_name", "member"))
    template = settings.get("naming_format") or "{category}-{username}"
    name = template.replace("{category}", category).replace("{username}", username).replace("{id}", ticket["id"].lower())
    name = sanitize_ticket_name(name)
    return ("closed-" if closed else "") + name[:90]


def ticket_lock(guild_id: int) -> asyncio.Lock:
    return ticket_creation_locks.setdefault(guild_id, asyncio.Lock())


def ticket_time() -> str:
    return discord.utils.utcnow().isoformat()


def display_ticket_time(value: Optional[str]) -> str:
    if not value:
        return "—"
    try:
        return discord.utils.format_dt(datetime.datetime.fromisoformat(value), style="F")
    except ValueError:
        return value


async def ticket_log(guild: discord.Guild, settings: dict[str, Any], action: str, ticket: dict[str, Any], actor: Optional[discord.abc.User] = None, detail: Optional[str] = None) -> None:
    channel_id = settings.get("log_channel_id")
    channel = guild.get_channel(channel_id) if channel_id else None
    if not isinstance(channel, discord.TextChannel):
        return
    embed = discord.Embed(title=f"🎫 Ticket {action}", color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
    embed.add_field(name="Ticket", value=ticket["id"], inline=True)
    embed.add_field(name="Category", value=ticket["category_label"], inline=True)
    embed.add_field(name="Creator", value=f"<@{ticket['creator_id']}>", inline=True)
    if actor:
        embed.add_field(name="Staff/User", value=actor.mention, inline=True)
    if detail:
        embed.add_field(name="Details", value=detail[:1024], inline=False)
    try:
        await channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException) as error:
        logger.warning("Could not send ticket log in guild %s: %s", guild.id, error)


def update_staff_stat(settings: dict[str, Any], member_id: int, field: str) -> None:
    staff = settings.setdefault("ticket_stats", {}).setdefault("staff", {}).setdefault(str(member_id), {"claimed": 0, "closed": 0, "reopened": 0})
    staff[field] = staff.get(field, 0) + 1


def transcript_html(guild: discord.Guild, channel: discord.TextChannel, ticket: dict[str, Any], messages: list[discord.Message]) -> bytes:
    metadata = [
        ("Ticket ID", ticket["id"]), ("Channel", f"#{channel.name}"), ("Category", ticket["category_label"]),
        ("Creator", f"{ticket.get('creator_name', 'Unknown')} ({ticket['creator_id']})"),
        ("Claimed by", ticket.get("claimer_name") or "Unclaimed"), ("Opened", ticket.get("created_at", "—")),
        ("Closed", ticket.get("closed_at", "—")), ("Closed by", ticket.get("closed_by_name") or "—"),
        ("Reason", ticket.get("close_reason") or "No reason provided"),
    ]
    cards = []
    for message in messages:
        content = html.escape(message.content or "")
        attachments = "".join(f'<li><a href="{html.escape(item.url, quote=True)}">{html.escape(item.filename)}</a></li>' for item in message.attachments)
        embeds = " ".join(html.escape((embed.title or "") + " " + (embed.description or "")) for embed in message.embeds)
        extra = f"<ul>{attachments}</ul>" if attachments else ""
        cards.append(f"<article><b>{html.escape(str(message.author))}</b> <small>({message.author.id}) · {message.created_at.isoformat()}</small><p>{content.replace(chr(10), '<br>')}</p><p>{embeds}</p>{extra}</article>")
    meta_html = "".join(f"<dt>{html.escape(key)}</dt><dd>{html.escape(str(value))}</dd>" for key, value in metadata)
    page = f"""<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(ticket['id'])} transcript</title><style>body{{font:15px Arial;background:#111827;color:#e5e7eb;margin:0;padding:32px}}main{{max-width:950px;margin:auto}}header,article{{background:#1f2937;border-radius:10px;padding:18px;margin:12px 0}}h1{{color:#a78bfa}}dl{{display:grid;grid-template-columns:150px 1fr;gap:8px}}dt{{font-weight:bold;color:#c4b5fd}}dd{{margin:0}}article{{border-left:4px solid #8b5cf6}}a{{color:#93c5fd}}small{{color:#9ca3af}}</style></head><body><main><header><h1>WXRST SUPPORT — Transcript</h1><p>{html.escape(guild.name)}</p><dl>{meta_html}</dl></header>{''.join(cards)}</main></body></html>"""
    return page.encode("utf-8")


async def create_transcript(guild: discord.Guild, channel: discord.TextChannel, ticket: dict[str, Any]) -> tuple[bytes, str]:
    messages = [message async for message in channel.history(limit=None, oldest_first=True)]
    data = transcript_html(guild, channel, ticket, messages)
    filename = f"{ticket['id'].lower()}-transcript.html"
    TICKET_TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TICKET_TRANSCRIPT_DIR / filename
    await asyncio.to_thread(path.write_bytes, data)
    ticket["transcript_path"] = str(path)
    return data, filename


def transcript_file(data: bytes, filename: str) -> discord.File:
    return discord.File(io.BytesIO(data), filename=filename)


async def send_ticket_transcript(guild: discord.Guild, settings: dict[str, Any], channel: discord.TextChannel, ticket: dict[str, Any], data: bytes, filename: str) -> None:
    creator = guild.get_member(ticket["creator_id"])
    if creator:
        dm = discord.Embed(title="WXRST Support — Ticket Closed", color=discord.Color.red())
        dm.description = f"Your ticket has been closed.\n\n**Ticket ID:** {ticket['id']}\n**Category:** {ticket['category_label']}\n**Closed by:** {ticket.get('closed_by_name') or 'Unknown'}\n**Reason:** {ticket.get('close_reason') or 'No reason provided'}\n\nThank you for contacting WXRST Support."
        try:
            await creator.send(embed=dm, file=transcript_file(data, filename))
            ticket["creator_dm_sent"] = True
        except (discord.Forbidden, discord.HTTPException) as error:
            logger.info("Ticket transcript DM could not be delivered for %s: %s", ticket["id"], error)
            ticket["creator_dm_sent"] = False

    log_channel_id = settings.get("transcript_channel_id")
    log_channel = guild.get_channel(log_channel_id) if log_channel_id else None
    if isinstance(log_channel, discord.TextChannel):
        embed = discord.Embed(title="🎫 Ticket Closed", color=discord.Color.red(), timestamp=discord.utils.utcnow())
        embed.description = f"**ID:** {ticket['id']}\n**Creator:** <@{ticket['creator_id']}>\n**Category:** {ticket['category_label']}\n**Claimer:** {ticket.get('claimer_name') or 'Unclaimed'}\n**Closed by:** {ticket.get('closed_by_name') or 'Unknown'}\n**Reason:** {ticket.get('close_reason') or 'No reason provided'}\n**Created:** {ticket.get('created_at')}\n**Closed:** {ticket.get('closed_at')}"
        try:
            await log_channel.send(embed=embed, file=transcript_file(data, filename))
            ticket["transcript_sent"] = True
        except (discord.Forbidden, discord.HTTPException) as error:
            logger.warning("Could not deliver transcript for %s: %s", ticket["id"], error)
    else:
        ticket["transcript_sent"] = False


class TicketPanelView(discord.ui.View):
    def __init__(self, guild_id: int) -> None:
        super().__init__(timeout=None)
        settings = ticket_settings(guild_id)
        for key, category in list(settings["categories"].items())[:20]:
            self.add_item(TicketCreateButton(key, category.get("label", key.title()), category.get("emoji", "🎫")))


class TicketCreateButton(discord.ui.Button):
    def __init__(self, category_key: str, label: str, emoji: str) -> None:
        super().__init__(label=label[:80], emoji=emoji, style=discord.ButtonStyle.primary, custom_id=f"ticket:create:{category_key}")
        self.category_key = category_key

    async def callback(self, interaction: discord.Interaction) -> None:
        await open_ticket(interaction, self.category_key)


class TicketControlsView(discord.ui.View):
    def __init__(self, ticket_id: str, claimed: bool = False) -> None:
        super().__init__(timeout=None)
        self.add_item(TicketActionButton("close", ticket_id, "Close Ticket", "🔒", discord.ButtonStyle.danger, row=0))
        self.add_item(TicketActionButton("claim", ticket_id, "Unclaim" if claimed else "Claim Ticket", "🔓" if claimed else "🔔", discord.ButtonStyle.secondary, row=0))
        self.add_item(TicketActionButton("add", ticket_id, "Add User", "👤", discord.ButtonStyle.secondary, row=0))
        self.add_item(TicketActionButton("remove", ticket_id, "Remove User", "➖", discord.ButtonStyle.secondary, row=0))
        self.add_item(TicketActionButton("reopen", ticket_id, "Reopen", "🔓", discord.ButtonStyle.success, row=1))
        self.add_item(TicketActionButton("delete", ticket_id, "Delete", "🗑️", discord.ButtonStyle.danger, row=1))


class TicketActionButton(discord.ui.Button):
    def __init__(self, action: str, ticket_id: str, label: str, emoji: str, style: discord.ButtonStyle, row: int) -> None:
        super().__init__(label=label, emoji=emoji, style=style, custom_id=f"ticket:{action}:{ticket_id}", row=row)
        self.action, self.ticket_id = action, ticket_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await ticket_action(interaction, self.action, self.ticket_id)


class CloseReasonModal(discord.ui.Modal, title="Close Ticket"):
    reason = discord.ui.TextInput(label="Closing Reason (optional)", style=discord.TextStyle.paragraph, required=False, max_length=800)

    def __init__(self, ticket_id: str) -> None:
        super().__init__()
        self.ticket_id = ticket_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("Close this ticket?", ephemeral=True, view=ConfirmTicketView("close", self.ticket_id, self.reason.value))


class UserIdModal(discord.ui.Modal):
    user_id = discord.ui.TextInput(label="User ID", placeholder="Right-click user → Copy User ID", max_length=30)

    def __init__(self, action: str, ticket_id: str) -> None:
        super().__init__(title="Add Ticket User" if action == "add" else "Remove Ticket User")
        self.action, self.ticket_id = action, ticket_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.user_id.value.isdigit():
            await interaction.response.send_message("Enter a valid numeric User ID.", ephemeral=True)
            return
        await alter_ticket_user(interaction, self.action, self.ticket_id, int(self.user_id.value))


class ConfirmTicketView(discord.ui.View):
    def __init__(self, action: str, ticket_id: str, reason: str = "") -> None:
        super().__init__(timeout=120)
        self.action, self.ticket_id, self.reason = action, ticket_id, reason

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self.action == "close":
            await close_ticket(interaction, self.ticket_id, self.reason)
        else:
            await delete_ticket(interaction, self.ticket_id)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)


async def open_ticket(interaction: discord.Interaction, category_key: str) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Tickets can only be opened in a server.", ephemeral=True)
        return
    guild, creator = interaction.guild, interaction.user
    async with ticket_lock(guild.id):
        settings = ticket_settings(guild.id)
        category = settings["categories"].get(category_key)
        if not category:
            await interaction.response.send_message("This ticket category is no longer configured.", ephemeral=True)
            return
        now = discord.utils.utcnow()
        tickets = settings["tickets"]
        open_tickets = [ticket for ticket in tickets.values() if ticket.get("creator_id") == creator.id and ticket.get("status") == "open"]
        duplicate = next((ticket for ticket in open_tickets if ticket.get("category_key") == category_key), None)
        if duplicate:
            await interaction.response.send_message(f"You already have an open {category.get('label', category_key)} ticket: <#{duplicate['channel_id']}>.", ephemeral=True)
            return
        if len(open_tickets) >= max(1, int(settings.get("max_open_per_user", 1))):
            await interaction.response.send_message("You have reached the maximum number of open tickets.", ephemeral=True)
            return
        last_created = max((ticket.get("created_at", "") for ticket in tickets.values() if ticket.get("creator_id") == creator.id), default="")
        if last_created:
            try:
                if (now - datetime.datetime.fromisoformat(last_created)).total_seconds() < int(settings.get("creation_cooldown_seconds", 30)):
                    await interaction.response.send_message("Please wait a moment before opening another ticket.", ephemeral=True)
                    return
            except ValueError:
                pass

        number = int(settings.get("next_ticket_number", 1))
        ticket_id = f"WXRST-{number:04d}"
        ticket = {"id": ticket_id, "creator_id": creator.id, "creator_name": creator.name, "category_key": category_key, "category_label": category.get("label", category_key.title()), "status": "open", "created_at": now.isoformat(), "claimer_id": None, "claimer_name": None, "added_user_ids": []}
        bot_member = guild.me or guild.get_member(bot.user.id)
        overwrites: dict[Any, discord.PermissionOverwrite] = {guild.default_role: discord.PermissionOverwrite(view_channel=False), creator: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)}
        if bot_member:
            overwrites[bot_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, manage_messages=True, read_message_history=True)
        for role_id in settings.get("support_role_ids", []):
            if role := guild.get_role(int(role_id)):
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)
        parent = guild.get_channel(settings.get("ticket_category_id")) if settings.get("ticket_category_id") else None
        if parent is not None and not isinstance(parent, discord.CategoryChannel):
            parent = None
        try:
            channel = await guild.create_text_channel(ticket_channel_name(ticket, settings), category=parent, overwrites=overwrites, topic=f"{ticket_id} | creator={creator.id} | category={category_key}", reason=f"Ticket opened by {creator}")
        except (discord.Forbidden, discord.HTTPException) as error:
            logger.warning("Could not create ticket in %s: %s", guild.id, error)
            await interaction.response.send_message("I couldn't create a ticket channel. Check my Manage Channels permission and the configured category.", ephemeral=True)
            return
        ticket["channel_id"] = channel.id
        tickets[ticket_id] = ticket
        settings["next_ticket_number"] = number + 1
        save_ticket_settings(guild.id, settings)

    embed = discord.Embed(title="🎫 WXRST SUPPORT", description=f"Welcome {creator.mention}!\n\n{settings['default_ticket_message']}", color=discord.Color.blurple(), timestamp=now)
    embed.add_field(name="Ticket", value=ticket_id, inline=True)
    embed.add_field(name="Type", value=ticket["category_label"], inline=True)
    embed.add_field(name="Status", value="OPEN", inline=True)
    await channel.send(content=creator.mention, embed=embed, view=TicketControlsView(ticket_id))
    await interaction.response.send_message(f"🎫 Your ticket has been created: {channel.mention}", ephemeral=True)
    await ticket_log(guild, settings, "Created", ticket, creator)


async def ticket_action(interaction: discord.Interaction, action: str, ticket_id: str) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return
    ticket, settings = get_ticket(interaction.guild.id, ticket_id), ticket_settings(interaction.guild.id)
    if not ticket or interaction.channel_id != ticket.get("channel_id"):
        await interaction.response.send_message("This ticket is no longer available.", ephemeral=True)
        return
    staff, creator = is_ticket_staff(interaction.user, settings), is_ticket_creator(interaction.user, ticket)
    if action == "close":
        if not (staff or creator) or ticket["status"] != "open":
            await interaction.response.send_message("You cannot close this ticket.", ephemeral=True)
            return
        await interaction.response.send_modal(CloseReasonModal(ticket_id))
    elif action == "claim":
        if ticket.get("claimer_id"):
            await unclaim_ticket(interaction, ticket_id)
        else:
            await claim_ticket(interaction, ticket_id)
    elif action in {"add", "remove"}:
        if not staff or not settings.get("user_management_enabled", True):
            await interaction.response.send_message("Only support staff can manage ticket users.", ephemeral=True)
            return
        await interaction.response.send_modal(UserIdModal(action, ticket_id))
    elif action == "reopen":
        await reopen_ticket(interaction, ticket_id)
    elif action == "delete":
        if not staff or ticket["status"] != "closed":
            await interaction.response.send_message("Only staff can delete a closed ticket.", ephemeral=True)
            return
        await interaction.response.send_message("Delete this closed ticket permanently?", ephemeral=True, view=ConfirmTicketView("delete", ticket_id))


async def claim_ticket(interaction: discord.Interaction, ticket_id: str) -> None:
    settings, ticket = ticket_settings(interaction.guild.id), get_ticket(interaction.guild.id, ticket_id)
    if not is_ticket_staff(interaction.user, settings) or not settings.get("claiming_enabled", True):
        await interaction.response.send_message("Only support staff can claim tickets.", ephemeral=True)
        return
    if ticket["status"] != "open":
        await interaction.response.send_message("Only open tickets can be claimed.", ephemeral=True)
        return
    claimer_id = ticket.get("claimer_id")
    if claimer_id and claimer_id != interaction.user.id and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(f"This ticket is already claimed by <@{claimer_id}>.", ephemeral=True)
        return
    if claimer_id == interaction.user.id:
        ticket["claimer_id"], ticket["claimer_name"] = None, None
        action, stat = "Unclaimed", None
    else:
        ticket["claimer_id"], ticket["claimer_name"] = interaction.user.id, interaction.user.display_name
        action, stat = "Claimed", "claimed"
    if stat:
        update_staff_stat(settings, interaction.user.id, stat)
    settings["tickets"][ticket_id] = ticket
    save_ticket_settings(interaction.guild.id, settings)
    await interaction.response.send_message(f"🔔 Ticket {action.lower()} by {interaction.user.mention}.")
    if interaction.message:
        try:
            await interaction.message.edit(view=TicketControlsView(ticket_id, claimed=bool(ticket.get("claimer_id"))))
        except discord.HTTPException:
            pass
    await ticket_log(interaction.guild, settings, action, ticket, interaction.user)


async def unclaim_ticket(interaction: discord.Interaction, ticket_id: str) -> None:
    settings, ticket = ticket_settings(interaction.guild.id), get_ticket(interaction.guild.id, ticket_id)
    if not ticket or not is_ticket_staff(interaction.user, settings):
        await interaction.response.send_message("Only support staff can unclaim tickets.", ephemeral=True)
        return
    if not ticket.get("claimer_id"):
        await interaction.response.send_message("This ticket is not claimed.", ephemeral=True)
        return
    if ticket["claimer_id"] != interaction.user.id and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only the claimer or an administrator can unclaim this ticket.", ephemeral=True)
        return
    ticket["claimer_id"], ticket["claimer_name"] = None, None
    settings["tickets"][ticket_id] = ticket
    save_ticket_settings(interaction.guild.id, settings)
    await interaction.response.send_message(f"🔓 Ticket unclaimed by {interaction.user.mention}.")
    if interaction.message:
        try:
            await interaction.message.edit(view=TicketControlsView(ticket_id, claimed=False))
        except discord.HTTPException:
            pass
    await ticket_log(interaction.guild, settings, "Unclaimed", ticket, interaction.user)


async def alter_ticket_user(interaction: discord.Interaction, action: str, ticket_id: str, user_id: int) -> None:
    settings, ticket = ticket_settings(interaction.guild.id), get_ticket(interaction.guild.id, ticket_id)
    if not ticket or not is_ticket_staff(interaction.user, settings):
        await interaction.response.send_message("You cannot manage this ticket.", ephemeral=True)
        return
    member = interaction.guild.get_member(user_id)
    if member is None:
        await interaction.response.send_message("That user is not in this server.", ephemeral=True)
        return
    if user_id == ticket["creator_id"]:
        await interaction.response.send_message("The ticket creator cannot be removed.", ephemeral=True)
        return
    added = set(ticket.get("added_user_ids", []))
    try:
        if action == "add":
            added.add(user_id)
            await interaction.channel.set_permissions(member, view_channel=True, send_messages=ticket["status"] == "open", read_message_history=True, attach_files=True)
            wording = "added"
        else:
            if user_id not in added:
                await interaction.response.send_message("That user was not added to this ticket.", ephemeral=True)
                return
            added.remove(user_id)
            await interaction.channel.set_permissions(member, overwrite=None)
            wording = "removed"
    except (discord.Forbidden, discord.HTTPException) as error:
        logger.warning("Ticket permission update failed: %s", error)
        await interaction.response.send_message("I couldn't update that user's ticket access.", ephemeral=True)
        return
    ticket["added_user_ids"] = list(added)
    settings["tickets"][ticket_id] = ticket
    save_ticket_settings(interaction.guild.id, settings)
    await interaction.response.send_message(f"👤 {member.mention} was {wording} {'to' if action == 'add' else 'from'} this ticket.")
    await ticket_log(interaction.guild, settings, f"User {wording.title()}", ticket, interaction.user, str(member))


async def close_ticket(interaction: discord.Interaction, ticket_id: str, reason: str) -> None:
    settings, ticket = ticket_settings(interaction.guild.id), get_ticket(interaction.guild.id, ticket_id)
    if not ticket or ticket["status"] != "open":
        await interaction.response.send_message("This ticket is already closed or unavailable.", ephemeral=True)
        return
    staff, creator = is_ticket_staff(interaction.user, settings), is_ticket_creator(interaction.user, ticket)
    if not (staff or creator):
        await interaction.response.send_message("You cannot close this ticket.", ephemeral=True)
        return
    channel = interaction.guild.get_channel(ticket["channel_id"])
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("The ticket channel no longer exists.", ephemeral=True)
        return
    ticket.update({"status": "closed", "closed_at": ticket_time(), "closed_by_id": interaction.user.id, "closed_by_name": interaction.user.display_name, "close_reason": reason or "No reason provided"})
    creator_member = interaction.guild.get_member(ticket["creator_id"])
    try:
        if creator_member:
            await channel.set_permissions(creator_member, view_channel=True, send_messages=False, read_message_history=True)
        await channel.edit(name=ticket_channel_name(ticket, settings, closed=True), reason=f"Ticket closed by {interaction.user}")
    except (discord.Forbidden, discord.HTTPException) as error:
        logger.warning("Ticket close permission/rename failed: %s", error)
    if staff:
        update_staff_stat(settings, interaction.user.id, "closed")
    try:
        data, filename = await create_transcript(interaction.guild, channel, ticket)
        await send_ticket_transcript(interaction.guild, settings, channel, ticket, data, filename)
    except Exception as error:
        logger.exception("Transcript generation failed for %s: %s", ticket_id, error)
        ticket["transcript_error"] = str(error)
    settings["tickets"][ticket_id] = ticket
    save_ticket_settings(interaction.guild.id, settings)
    await interaction.response.edit_message(content=f"🔒 Ticket closed by {interaction.user.mention}. Reason: {ticket['close_reason']}", view=TicketControlsView(ticket_id, claimed=bool(ticket.get("claimer_id"))))
    await ticket_log(interaction.guild, settings, "Closed", ticket, interaction.user, ticket["close_reason"])
    delay = int(settings.get("auto_delete_minutes", 0))
    if delay > 0 and ticket.get("transcript_sent"):
        asyncio.create_task(auto_delete_ticket(interaction.guild.id, ticket_id, delay * 60))


async def auto_delete_ticket(guild_id: int, ticket_id: str, seconds: int) -> None:
    await asyncio.sleep(seconds)
    guild = bot.get_guild(guild_id)
    ticket = get_ticket(guild_id, ticket_id)
    if guild and ticket and ticket.get("status") == "closed":
        channel = guild.get_channel(ticket.get("channel_id"))
        if isinstance(channel, discord.TextChannel) and ticket.get("transcript_sent"):
            try:
                await channel.delete(reason="Configured ticket auto-delete")
            except (discord.Forbidden, discord.HTTPException):
                pass


async def reopen_ticket(interaction: discord.Interaction, ticket_id: str) -> None:
    settings, ticket = ticket_settings(interaction.guild.id), get_ticket(interaction.guild.id, ticket_id)
    if not ticket or not is_ticket_staff(interaction.user, settings) or not settings.get("reopening_enabled", True):
        await interaction.response.send_message("Only support staff can reopen tickets.", ephemeral=True)
        return
    if ticket["status"] != "closed":
        await interaction.response.send_message("This ticket is already open.", ephemeral=True)
        return
    channel = interaction.guild.get_channel(ticket["channel_id"])
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("The ticket channel no longer exists.", ephemeral=True)
        return
    creator = interaction.guild.get_member(ticket["creator_id"])
    try:
        if creator:
            await channel.set_permissions(creator, view_channel=True, send_messages=True, read_message_history=True, attach_files=True)
        await channel.edit(name=ticket_channel_name(ticket, settings), reason=f"Ticket reopened by {interaction.user}")
    except (discord.Forbidden, discord.HTTPException) as error:
        await interaction.response.send_message("I couldn't restore ticket permissions.", ephemeral=True)
        logger.warning("Ticket reopen failed: %s", error)
        return
    ticket.update({"status": "open", "reopened_at": ticket_time(), "reopened_by_id": interaction.user.id, "reopened_by_name": interaction.user.display_name})
    update_staff_stat(settings, interaction.user.id, "reopened")
    settings["tickets"][ticket_id] = ticket
    save_ticket_settings(interaction.guild.id, settings)
    await interaction.response.send_message(f"🔓 Ticket reopened by {interaction.user.mention}.")
    await ticket_log(interaction.guild, settings, "Reopened", ticket, interaction.user)


async def delete_ticket(interaction: discord.Interaction, ticket_id: str) -> None:
    settings, ticket = ticket_settings(interaction.guild.id), get_ticket(interaction.guild.id, ticket_id)
    if not ticket or not is_ticket_staff(interaction.user, settings) or ticket["status"] != "closed":
        await interaction.response.send_message("Only staff can delete closed tickets.", ephemeral=True)
        return
    channel = interaction.guild.get_channel(ticket["channel_id"])
    if not ticket.get("transcript_path") or not ticket.get("transcript_sent"):
        await interaction.response.send_message("The transcript must be generated and delivered to the configured transcript channel before deletion.", ephemeral=True)
        return
    ticket["deleted_at"], ticket["deleted_by_id"] = ticket_time(), interaction.user.id
    settings["tickets"][ticket_id] = ticket
    save_ticket_settings(interaction.guild.id, settings)
    await ticket_log(interaction.guild, settings, "Deleted", ticket, interaction.user)
    await interaction.response.send_message("🗑️ Deleting ticket channel…", ephemeral=True)
    if isinstance(channel, discord.TextChannel):
        try:
            await channel.delete(reason=f"Ticket deleted by {interaction.user}")
        except (discord.Forbidden, discord.HTTPException) as error:
            logger.warning("Ticket deletion failed: %s", error)


def register_ticket_views() -> None:
    for guild in bot.guilds:
        if guild.id in registered_ticket_views:
            continue
        settings = ticket_settings(guild.id)
        panel_message_id = settings.get("panel_message_id")
        if panel_message_id:
            bot.add_view(TicketPanelView(guild.id), message_id=int(panel_message_id))
        for ticket_id, ticket in settings.get("tickets", {}).items():
            if ticket.get("status") in {"open", "closed"}:
                bot.add_view(TicketControlsView(ticket_id, claimed=bool(ticket.get("claimer_id"))))
            if ticket.get("status") == "closed" and ticket.get("transcript_sent") and int(settings.get("auto_delete_minutes", 0)) > 0:
                try:
                    close_time = datetime.datetime.fromisoformat(ticket["closed_at"])
                    remaining = max(0, int(settings["auto_delete_minutes"]) * 60 - int((discord.utils.utcnow() - close_time).total_seconds()))
                    asyncio.create_task(auto_delete_ticket(guild.id, ticket_id, remaining))
                except (KeyError, TypeError, ValueError):
                    pass
        registered_ticket_views.add(guild.id)


def require_ticket_admin(interaction: discord.Interaction) -> bool:
    return isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator


@bot.tree.command(name="ticketsetup", description="Send the WXRST support ticket panel")
@app_commands.describe(channel="Channel where the ticket panel should be sent")
async def ticketsetup(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    if not require_ticket_admin(interaction):
        await interaction.response.send_message("Only administrators can set up ticket panels.", ephemeral=True)
        return
    settings = ticket_settings(interaction.guild.id)
    embed = discord.Embed(title=settings["panel_title"], description=settings["panel_description"], color=discord.Color.blurple())
    if settings.get("panel_banner"):
        embed.set_image(url=settings["panel_banner"])
    embed.set_footer(text="WXRST SUPPORT • Your ticket is private")
    try:
        message = None
        if settings.get("panel_channel_id") == channel.id and settings.get("panel_message_id"):
            try:
                previous = await channel.fetch_message(int(settings["panel_message_id"]))
                await previous.edit(embed=embed, view=TicketPanelView(interaction.guild.id))
                message = previous
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        if message is None:
            message = await channel.send(embed=embed, view=TicketPanelView(interaction.guild.id))
        settings["panel_channel_id"] = channel.id
        settings["panel_message_id"] = message.id
        save_ticket_settings(interaction.guild.id, settings)
        bot.add_view(TicketPanelView(interaction.guild.id), message_id=message.id)
        await interaction.response.send_message(f"✅ Ticket panel updated in {channel.mention}.", ephemeral=True)
    except (discord.Forbidden, discord.HTTPException) as error:
        await interaction.response.send_message("I couldn't send the ticket panel there. Check my channel permissions.", ephemeral=True)
        logger.warning("Ticket panel send failed: %s", error)


ticket_config = app_commands.Group(name="ticketconfig", description="Configure the ticket system")


@ticket_config.command(name="channels", description="Configure ticket, transcript, and log channels")
async def ticketconfig_channels(interaction: discord.Interaction, ticket_category: Optional[discord.CategoryChannel] = None, transcript_channel: Optional[discord.TextChannel] = None, log_channel: Optional[discord.TextChannel] = None) -> None:
    if not require_ticket_admin(interaction):
        await interaction.response.send_message("Only administrators can configure tickets.", ephemeral=True); return
    settings = ticket_settings(interaction.guild.id)
    if ticket_category: settings["ticket_category_id"] = ticket_category.id
    if transcript_channel: settings["transcript_channel_id"] = transcript_channel.id
    if log_channel: settings["log_channel_id"] = log_channel.id
    save_ticket_settings(interaction.guild.id, settings)
    await interaction.response.send_message("✅ Ticket channel configuration saved.", ephemeral=True)


@ticket_config.command(name="supportrole", description="Add or remove a ticket support role")
async def ticketconfig_supportrole(interaction: discord.Interaction, role: discord.Role, remove: bool = False) -> None:
    if not require_ticket_admin(interaction):
        await interaction.response.send_message("Only administrators can configure tickets.", ephemeral=True); return
    settings = ticket_settings(interaction.guild.id)
    roles = set(int(role_id) for role_id in settings.get("support_role_ids", []))
    if remove: roles.discard(role.id)
    else: roles.add(role.id)
    settings["support_role_ids"] = list(roles)
    save_ticket_settings(interaction.guild.id, settings)
    await interaction.response.send_message(f"✅ {role.mention} {'removed from' if remove else 'added to'} ticket support roles.", ephemeral=True)


@ticket_config.command(name="limits", description="Configure ticket limits and automatic deletion")
async def ticketconfig_limits(interaction: discord.Interaction, max_open_per_user: app_commands.Range[int, 1, 10] = 1, cooldown_seconds: app_commands.Range[int, 0, 3600] = 30, auto_delete_minutes: app_commands.Range[int, 0, 1440] = 0) -> None:
    if not require_ticket_admin(interaction):
        await interaction.response.send_message("Only administrators can configure tickets.", ephemeral=True); return
    settings = ticket_settings(interaction.guild.id)
    settings.update({"max_open_per_user": max_open_per_user, "creation_cooldown_seconds": cooldown_seconds, "auto_delete_minutes": auto_delete_minutes})
    save_ticket_settings(interaction.guild.id, settings)
    await interaction.response.send_message("✅ Ticket limits saved. Set auto-delete to 0 for Never.", ephemeral=True)


@ticket_config.command(name="panel", description="Configure ticket panel branding and message")
async def ticketconfig_panel(interaction: discord.Interaction, title: str, description: str, banner_url: Optional[str] = None) -> None:
    if not require_ticket_admin(interaction):
        await interaction.response.send_message("Only administrators can configure tickets.", ephemeral=True); return
    settings = ticket_settings(interaction.guild.id)
    settings.update({"panel_title": title[:256], "panel_description": description[:4000], "panel_banner": banner_url or None})
    save_ticket_settings(interaction.guild.id, settings)
    await interaction.response.send_message("✅ Ticket panel branding saved. Run `/ticketsetup` to post an updated panel.", ephemeral=True)


@ticket_config.command(name="category", description="Configure a ticket button/category")
async def ticketconfig_category(interaction: discord.Interaction, key: str, label: str, emoji: str = "🎫") -> None:
    if not require_ticket_admin(interaction):
        await interaction.response.send_message("Only administrators can configure tickets.", ephemeral=True); return
    key = sanitize_ticket_name(key)
    if not key:
        await interaction.response.send_message("Use a valid category key.", ephemeral=True); return
    settings = ticket_settings(interaction.guild.id)
    settings["categories"][key] = {"label": label[:80], "emoji": emoji[:16]}
    save_ticket_settings(interaction.guild.id, settings)
    await interaction.response.send_message("✅ Ticket category saved. Run `/ticketsetup` to post a panel using it.", ephemeral=True)


@ticket_config.command(name="features", description="Enable or disable ticket features")
async def ticketconfig_features(interaction: discord.Interaction, claiming: bool = True, reopening: bool = True, user_management: bool = True) -> None:
    if not require_ticket_admin(interaction):
        await interaction.response.send_message("Only administrators can configure tickets.", ephemeral=True); return
    settings = ticket_settings(interaction.guild.id)
    settings.update({"claiming_enabled": claiming, "reopening_enabled": reopening, "user_management_enabled": user_management})
    save_ticket_settings(interaction.guild.id, settings)
    await interaction.response.send_message("✅ Ticket feature settings saved.", ephemeral=True)


@bot.tree.command(name="ticketstats", description="Show ticket statistics")
async def ticketstats(interaction: discord.Interaction) -> None:
    settings = ticket_settings(interaction.guild.id); tickets = list(settings["tickets"].values()); now = discord.utils.utcnow()
    opened_today = sum(ticket.get("created_at", "").startswith(now.date().isoformat()) for ticket in tickets)
    closed_today = sum(ticket.get("closed_at", "").startswith(now.date().isoformat()) for ticket in tickets)
    week_ago = now - datetime.timedelta(days=7)
    opened_week = sum(datetime.datetime.fromisoformat(ticket["created_at"]) >= week_ago for ticket in tickets if ticket.get("created_at"))
    closed_week = sum(datetime.datetime.fromisoformat(ticket["closed_at"]) >= week_ago for ticket in tickets if ticket.get("closed_at"))
    categories = [ticket.get("category_label", "Other") for ticket in tickets]
    popular = max(set(categories), key=categories.count) if categories else "—"
    embed = discord.Embed(title="🎫 WXRST Ticket Statistics", color=discord.Color.blurple())
    embed.description = f"**Total:** {len(tickets)}\n**Open:** {sum(ticket.get('status') == 'open' for ticket in tickets)}\n**Closed:** {sum(ticket.get('status') == 'closed' for ticket in tickets)}\n\n**Opened today / week:** {opened_today} / {opened_week}\n**Closed today / week:** {closed_today} / {closed_week}\n**Most used category:** {popular}"
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="ticketstaff", description="Show ticket support staff leaderboard")
async def ticketstaff(interaction: discord.Interaction) -> None:
    staff = ticket_settings(interaction.guild.id)["ticket_stats"].get("staff", {})
    ranking = sorted(staff.items(), key=lambda item: (item[1].get("closed", 0), item[1].get("claimed", 0)), reverse=True)
    lines = [f"{index}. <@{member_id}> — {values.get('closed', 0)} resolved | {values.get('claimed', 0)} claimed | {values.get('reopened', 0)} reopened" for index, (member_id, values) in enumerate(ranking[:10], 1)]
    embed = discord.Embed(title="🏆 WXRST SUPPORT STAFF", description="\n".join(lines) or "No ticket staff activity yet.", color=discord.Color.gold())
    await interaction.response.send_message(embed=embed, ephemeral=True)


ticket_group = app_commands.Group(name="ticket", description="Manage the current ticket")


@ticket_group.command(name="add", description="Add a user to this ticket")
async def ticket_add(interaction: discord.Interaction, user: discord.Member) -> None:
    ticket = next((ticket for ticket in ticket_settings(interaction.guild.id)["tickets"].values() if ticket.get("channel_id") == interaction.channel_id), None)
    if not ticket:
        await interaction.response.send_message("Use this command inside a ticket channel.", ephemeral=True); return
    await alter_ticket_user(interaction, "add", ticket["id"], user.id)


@ticket_group.command(name="remove", description="Remove a user from this ticket")
async def ticket_remove(interaction: discord.Interaction, user: discord.Member) -> None:
    ticket = next((ticket for ticket in ticket_settings(interaction.guild.id)["tickets"].values() if ticket.get("channel_id") == interaction.channel_id), None)
    if not ticket:
        await interaction.response.send_message("Use this command inside a ticket channel.", ephemeral=True); return
    await alter_ticket_user(interaction, "remove", ticket["id"], user.id)


async def ticket_command_action(interaction: discord.Interaction, action: str) -> None:
    ticket = next((ticket for ticket in ticket_settings(interaction.guild.id)["tickets"].values() if ticket.get("channel_id") == interaction.channel_id), None)
    if not ticket:
        await interaction.response.send_message("Use this command inside a ticket channel.", ephemeral=True); return
    if action == "close": await ticket_action(interaction, "close", ticket["id"])
    elif action == "reopen": await reopen_ticket(interaction, ticket["id"])
    elif action == "delete": await ticket_action(interaction, "delete", ticket["id"])
    elif action == "claim": await claim_ticket(interaction, ticket["id"])
    else: await unclaim_ticket(interaction, ticket["id"])


@ticket_group.command(name="close", description="Close this ticket")
async def ticket_close(interaction: discord.Interaction) -> None: await ticket_command_action(interaction, "close")
@ticket_group.command(name="reopen", description="Reopen this ticket")
async def ticket_reopen(interaction: discord.Interaction) -> None: await ticket_command_action(interaction, "reopen")
@ticket_group.command(name="delete", description="Delete this closed ticket")
async def ticket_delete(interaction: discord.Interaction) -> None: await ticket_command_action(interaction, "delete")
@ticket_group.command(name="claim", description="Claim this ticket")
async def ticket_claim(interaction: discord.Interaction) -> None: await ticket_command_action(interaction, "claim")
@ticket_group.command(name="unclaim", description="Unclaim this ticket")
async def ticket_unclaim(interaction: discord.Interaction) -> None: await ticket_command_action(interaction, "unclaim")


bot.tree.add_command(ticket_config)
bot.tree.add_command(ticket_group)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("No token found! Open .env and set DISCORD_TOKEN.")
    bot.run(TOKEN)
