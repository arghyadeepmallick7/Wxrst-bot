"""
Wxrst DM and community bot with a per-server music player.

Secrets belong in .env; this file never stores a Discord token, cookies, or API keys.
"""

import asyncio
import datetime
import json
import logging
import os
import random
import shutil
import time
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


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("No token found! Open .env and set DISCORD_TOKEN.")
    bot.run(TOKEN)
