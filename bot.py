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
import json
import random

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
from gtts import gTTS
import yt_dlp


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
ROLE_ID = int(os.getenv("ROLE_ID", "0"))
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

CONFIG_FILE = "config.json"


# =========================
# CONFIG
# =========================

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_config(data):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
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


def ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {
            1: "st",
            2: "nd",
            3: "rd"
        }.get(n % 10, "th")

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


# =========================
# READY
# =========================

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
            "This bot is private and only works in its home server.",
            ephemeral=True
        )
        return False

    return True


# =========================
# NOTIFY
# =========================

@bot.tree.command(
    name="notify",
    description="DM everyone who has the special role"
)
@app_commands.describe(
    message="What do you want to tell them?"
)
async def notify(
    interaction: discord.Interaction,
    message: str
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "Sorry, only a server admin can use this command.",
            ephemeral=True
        )
        return

    role = interaction.guild.get_role(ROLE_ID)

    if role is None:
        await interaction.response.send_message(
            f"I couldn't find a role with ID {ROLE_ID} in this server. "
            f"Double check the ROLE_ID value in your .env file.",
            ephemeral=True
        )
        return

    members = [
        member
        for member in role.members
        if not member.bot
    ]

    if not members:
        await interaction.response.send_message(
            f"Nobody currently has the '{role.name}' role, so there's nobody to message.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        "Sending DMs now... 📨",
        ephemeral=True
    )

    sent = 0
    failed = 0

    for member in members:
        try:
            await member.send(
                f"📢 **Message from {interaction.guild.name}:**\n\n{message}"
            )
            sent += 1
        except (discord.Forbidden, discord.HTTPException):
            failed += 1

    await interaction.followup.send(
        f"Done! ✅ Sent to **{sent}** members.\n"
        f"❌ Could not reach **{failed}** members.",
        ephemeral=True
    )


# =========================
# WELCOME
# =========================

class WelcomeModal(
    discord.ui.Modal,
    title="Welcome Message Setup"
):
    embed_title = discord.ui.TextInput(
        label="Title",
        placeholder="WELCOME TO {server}",
        required=False,
        max_length=100
    )

    description = discord.ui.TextInput(
        label="Description (press Enter for new lines)",
        style=discord.TextStyle.paragraph,
        placeholder=(
            "〻 WELCOME {username}\n"
            "» You joined {server}\n"
            "» {membercount} members now"
        ),
        required=False,
        max_length=1000
    )

    ping_text = discord.ui.TextInput(
        label="Text shown above the box (optional)",
        placeholder="{user} Welcome",
        required=False,
        max_length=200
    )

    banner_url = discord.ui.TextInput(
        label="Banner image link (optional)",
        required=False,
        max_length=300
    )

    def __init__(self, channel: discord.TextChannel):
        super().__init__()
        self.channel = channel

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):
        set_guild_setting(
            interaction.guild.id,
            "welcome_channel",
            self.channel.id
        )

        if self.embed_title.value:
            set_guild_setting(
                interaction.guild.id,
                "welcome_title",
                self.embed_title.value
            )

        if self.description.value:
            set_guild_setting(
                interaction.guild.id,
                "welcome_message",
                self.description.value
            )

        if self.ping_text.value:
            set_guild_setting(
                interaction.guild.id,
                "welcome_ping",
                self.ping_text.value
            )

        if self.banner_url.value:
            set_guild_setting(
                interaction.guild.id,
                "welcome_banner",
                self.banner_url.value
            )

        await interaction.response.send_message(
            f"✅ Welcome messages are set up in {self.channel.mention}!",
            ephemeral=True
        )


# =========================
# GOODBYE
# =========================

class GoodbyeModal(
    discord.ui.Modal,
    title="Goodbye Message Setup"
):
    description = discord.ui.TextInput(
        label="Message (press Enter for new lines)",
        style=discord.TextStyle.paragraph,
        placeholder=(
            "〻 GOODBYE {username}\n"
            "» You have left {server}\n"
            "» {membercount} members remain"
        ),
        required=False,
        max_length=1000
    )

    def __init__(self, channel: discord.TextChannel):
        super().__init__()
        self.channel = channel

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):
        set_guild_setting(
            interaction.guild.id,
            "goodbye_channel",
            self.channel.id
        )

        if self.description.value:
            set_guild_setting(
                interaction.guild.id,
                "goodbye_message",
                self.description.value
            )

        await interaction.response.send_message(
            f"✅ Goodbye messages are set up in {self.channel.mention}!",
            ephemeral=True
        )


def resolve_channel(
    guild: discord.Guild,
    channel_input: str
):
    cleaned = channel_input.strip().strip("<#>")

    if cleaned.isdigit():
        return guild.get_channel(int(cleaned))

    return None


@bot.tree.command(
    name="setwelcome",
    description="Set up a fancy welcome embed for new members"
)
@app_commands.describe(
    channel_id="The channel's ID number"
)
async def setwelcome(
    interaction: discord.Interaction,
    channel_id: str
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "Only a server admin can set this up.",
            ephemeral=True
        )
        return

    channel = resolve_channel(
        interaction.guild,
        channel_id
    )

    if channel is None:
        await interaction.response.send_message(
            "I couldn't find that channel. Make sure you pasted the Channel ID number.",
            ephemeral=True
        )
        return

    await interaction.response.send_modal(
        WelcomeModal(channel)
    )


@bot.tree.command(
    name="setgoodbye",
    description="Set the channel and message for when members leave"
)
@app_commands.describe(
    channel_id="The channel's ID number"
)
async def setgoodbye(
    interaction: discord.Interaction,
    channel_id: str
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "Only a server admin can set this up.",
            ephemeral=True
        )
        return

    channel = resolve_channel(
        interaction.guild,
        channel_id
    )

    if channel is None:
        await interaction.response.send_message(
            "I couldn't find that channel. Make sure you pasted the Channel ID number.",
            ephemeral=True
        )
        return

    await interaction.response.send_modal(
        GoodbyeModal(channel)
    )


# =========================
# AUTOROLE
# =========================

@bot.tree.command(
    name="setautorole",
    description="Automatically give new members a role when they join"
)
@app_commands.describe(
    role="The role to give automatically"
)
async def setautorole(
    interaction: discord.Interaction,
    role: discord.Role = None
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "Only a server admin can set this up.",
            ephemeral=True
        )
        return

    if role is None:
        set_guild_setting(
            interaction.guild.id,
            "autorole_id",
            None
        )

        await interaction.response.send_message(
            "✅ Autorole turned off.",
            ephemeral=True
        )
        return

    if role >= interaction.guild.me.top_role:
        await interaction.response.send_message(
            f"⚠️ I can't assign **{role.name}** because it's higher than my own role.",
            ephemeral=True
        )
        return

    set_guild_setting(
        interaction.guild.id,
        "autorole_id",
        role.id
    )

    await interaction.response.send_message(
        f"✅ New members will automatically get the **{role.name}** role.",
        ephemeral=True
    )


# =========================
# AUTONICKNAME
# =========================

@bot.tree.command(
    name="setautonickname",
    description="Automatically set a nickname format for new members"
)
@app_commands.describe(
    format="Use {username} as a placeholder"
)
async def setautonickname(
    interaction: discord.Interaction,
    format: str = None
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "Only a server admin can set this up.",
            ephemeral=True
        )
        return

    set_guild_setting(
        interaction.guild.id,
        "autonickname_format",
        format
    )

    if format:
        await interaction.response.send_message(
            f"✅ New members will be renamed using the format: `{format}`",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "✅ Autonickname turned off.",
            ephemeral=True
        )


# =========================
# AUTOMOD
# =========================

@bot.tree.command(
    name="automod",
    description="Turn the bad-word and spam filter on or off"
)
@app_commands.describe(
    state="Turn automod on or off"
)
@app_commands.choices(
    state=[
        app_commands.Choice(
            name="on",
            value="on"
        ),
        app_commands.Choice(
            name="off",
            value="off"
        )
    ]
)
async def automod(
    interaction: discord.Interaction,
    state: app_commands.Choice[str]
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "Only a server admin can set this up.",
            ephemeral=True
        )
        return

    set_guild_setting(
        interaction.guild.id,
        "automod_enabled",
        state.value == "on"
    )

    await interaction.response.send_message(
        f"✅ Automod is now **{state.value}**.",
        ephemeral=True
    )


@bot.tree.command(
    name="addbadword",
    description="Add a word for automod to delete automatically"
)
@app_commands.describe(
    word="The word to block"
)
async def addbadword(
    interaction: discord.Interaction,
    word: str
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "Only a server admin can set this up.",
            ephemeral=True
        )
        return

    settings = get_guild_settings(
        interaction.guild.id
    )

    bad_words = settings.get(
        "bad_words",
        []
    )

    word_lower = word.lower()

    if word_lower not in bad_words:
        bad_words.append(word_lower)

        set_guild_setting(
            interaction.guild.id,
            "bad_words",
            bad_words
        )

    await interaction.response.send_message(
        f"✅ Added `{word}` to the blocked word list.",
        ephemeral=True
    )


@bot.tree.command(
    name="removebadword",
    description="Remove a word from automod's blocked list"
)
@app_commands.describe(
    word="The word to unblock"
)
async def removebadword(
    interaction: discord.Interaction,
    word: str
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "Only a server admin can set this up.",
            ephemeral=True
        )
        return

    settings = get_guild_settings(
        interaction.guild.id
    )

    bad_words = settings.get(
        "bad_words",
        []
    )

    word_lower = word.lower()

    if word_lower in bad_words:
        bad_words.remove(word_lower)

        set_guild_setting(
            interaction.guild.id,
            "bad_words",
            bad_words
        )

        await interaction.response.send_message(
            f"✅ Removed `{word}` from the blocked word list.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "That word wasn't on the list.",
            ephemeral=True
        )


# =========================
# TTS + MUSIC STORAGE
# =========================

recent_messages = {}
tts_queues = {}
music_queues = {}
now_playing = {}


YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch1",
    "nocheckcertificate": True
}


FFMPEG_OPTIONS = {
    "before_options": (
        "-reconnect 1 "
        "-reconnect_streamed 1 "
        "-reconnect_delay_max 5"
    ),
    "options": "-vn"
}


# =========================
# MUSIC SEARCH
# =========================

def search_song(query: str):
    with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
        info = ydl.extract_info(
            query,
            download=False
        )

        if not info:
            raise RuntimeError("No result found.")

        if "entries" in info:
            entries = info["entries"]

            if not entries:
                raise RuntimeError("No result found.")

            info = entries[0]

        return {
            "title": info.get(
                "title",
                "Unknown title"
            ),
            "url": info["url"]
        }


# =========================
# MUSIC PLAYBACK
# =========================

async def play_next_song(
    guild_id: int,
    vc: discord.VoiceClient
):
    queue = music_queues.get(
        guild_id,
        []
    )

    if not queue:
        now_playing[guild_id] = None
        return

    if not vc.is_connected():
        now_playing[guild_id] = None
        return

    song = queue.pop(0)

    now_playing[guild_id] = song

    def after_playing(error):
        if error:
            print(
                f"⚠️ Music playback error: {error}"
            )

        future = asyncio.run_coroutine_threadsafe(
            play_next_song(
                guild_id,
                vc
            ),
            bot.loop
        )

        try:
            future.result()
        except Exception as e:
            print(
                f"⚠️ Music follow-up error: {e}"
            )

    try:
        source = discord.FFmpegPCMAudio(
            song["url"],
            **FFMPEG_OPTIONS
        )

        vc.play(
            discord.PCMVolumeTransformer(
                source,
                volume=0.5
            ),
            after=after_playing
        )

    except Exception as e:
        print(
            f"⚠️ Could not start music: {e}"
        )

        now_playing[guild_id] = None

        await play_next_song(
            guild_id,
            vc
        )


# =========================
# TTS PLAYBACK
# =========================

async def play_next_tts(
    guild_id: int,
    vc: discord.VoiceClient
):
    queue = tts_queues.get(
        guild_id,
        []
    )

    if not queue:
        return

    if not vc.is_connected():
        return

    text = queue.pop(0)

    filename = f"tts_{guild_id}.mp3"

    def generate_audio():
        gTTS(
            text=text,
            lang="en"
        ).save(filename)

    try:
        loop = asyncio.get_running_loop()

        await loop.run_in_executor(
            None,
            generate_audio
        )

    except Exception as e:
        print(
            f"⚠️ TTS generation error: {e}"
        )

        await play_next_tts(
            guild_id,
            vc
        )

        return

    def after_playing(error):
        if error:
            print(
                f"⚠️ TTS playback error: {error}"
            )

        future = asyncio.run_coroutine_threadsafe(
            play_next_tts(
                guild_id,
                vc
            ),
            bot.loop
        )

        try:
            future.result()
        except Exception as e:
            print(
                f"⚠️ TTS follow-up error: {e}"
            )

    try:
        vc.play(
            discord.FFmpegPCMAudio(
                filename
            ),
            after=after_playing
        )

    except Exception as e:
        print(
            f"⚠️ Could not start TTS: {e}"
        )


async def handle_tts_message(
    message: discord.Message,
    spoken_text: str
):
    voice_state = message.author.voice

    if voice_state is None or voice_state.channel is None:
        return

    voice_channel = voice_state.channel

    vc = discord.utils.get(
        bot.voice_clients,
        guild=message.guild
    )

    try:
        if vc is None:
            vc = await voice_channel.connect()

        elif vc.channel != voice_channel:
            await vc.move_to(
                voice_channel
            )

    except (
        discord.ClientException,
        discord.Forbidden
    ):
        return

    full_text = (
        f"{message.author.display_name} said "
        f"{spoken_text}"
    )

    tts_queues.setdefault(
        message.guild.id,
        []
    ).append(full_text)

    if not vc.is_playing():
        await play_next_tts(
            message.guild.id,
            vc
        )


# =========================
# MESSAGE EVENT
# =========================

@bot.event
async def on_message(
    message: discord.Message
):
    if (
        message.author.bot
        or message.guild is None
    ):
        return

    # TTS:
    # .Hello everyone
    if message.content.startswith("."):
        spoken_text = (
            message.content[1:].strip()
        )

        if spoken_text:
            await handle_tts_message(
                message,
                spoken_text
            )

        return

    settings = get_guild_settings(
        message.guild.id
    )

    # =========================
    # AUTOMOD
    # =========================

    if settings.get("automod_enabled"):

        if not message.author.guild_permissions.administrator:

            bad_words = settings.get(
                "bad_words",
                []
            )

            content_lower = (
                message.content.lower()
            )

            if any(
                word in content_lower
                for word in bad_words
            ):
                try:
                    await message.delete()

                    await message.channel.send(
                        f"🚫 {message.author.mention}, "
                        f"that word isn't allowed here.",
                        delete_after=5
                    )

                except discord.Forbidden:
                    pass

                return

            key = (
                message.guild.id,
                message.author.id
            )

            now = (
                discord.utils.utcnow()
                .timestamp()
            )

            timestamps = recent_messages.get(
                key,
                []
            )

            timestamps = [
                timestamp
                for timestamp in timestamps
                if now - timestamp < 5
            ]

            timestamps.append(now)

            recent_messages[key] = timestamps

            if len(timestamps) > 5:
                try:
                    await message.delete()

                    await message.channel.send(
                        f"🚫 {message.author.mention}, "
                        f"please slow down "
                        f"(you're sending messages too fast).",
                        delete_after=5
                    )

                except discord.Forbidden:
                    pass

                return

    await bot.process_commands(
        message
    )


# =========================
# MEMBER JOIN
# =========================

@bot.event
async def on_member_join(
    member: discord.Member
):
    settings = get_guild_settings(
        member.guild.id
    )

    # AUTOROLE
    autorole_id = settings.get(
        "autorole_id"
    )

    if autorole_id:
        role = member.guild.get_role(
            autorole_id
        )

        if role:
            try:
                await member.add_roles(
                    role,
                    reason="Autorole"
                )
            except discord.Forbidden:
                pass

    # AUTONICKNAME
    nickname_format = settings.get(
        "autonickname_format"
    )

    if nickname_format:
        new_nick = fill_placeholders(
            nickname_format,
            member
        )[:32]

        try:
            await member.edit(
                nick=new_nick,
                reason="Autonickname"
            )
        except discord.Forbidden:
            pass

    # WELCOME
    channel_id = settings.get(
        "welcome_channel"
    )

    if not channel_id:
        return

    channel = member.guild.get_channel(
        channel_id
    )

    if channel is None:
        return

    title_template = settings.get(
        "welcome_title",
        "WELCOME TO {server}"
    )

    desc_template = settings.get(
        "welcome_message",
        (
            "〻 **WELCOME {user}**\n"
            "\n"
            "» You've officially joined **{server}**\n"
            "» `Member #{ordinal}` 〻 **{membercount} members**\n"
            "» Joined __{joindate}__\n"
            "\n"
            "〻 **WXRST COMMUNITY**\n"
            "\n"
            "» PvP 〻 Grinding 〻 Competition\n"
            "» `Meet the team` 〻 Build your legacy\n"
            "» Stay active 〻 **Represent WXRST**\n"
            "\n"
            "> *Welcome to the family.*\n"
            "\n"
            "〻 **One Team** » `One Goal` » **WXRST**"
        )
    )

    ping_template = settings.get(
        "welcome_ping",
        "{user} Welcome"
    )

    banner_url = settings.get(
        "welcome_banner"
    )

    embed = discord.Embed(
        title=fill_placeholders(
            title_template,
            member
        ),
        description=fill_placeholders(
            desc_template,
            member
        ),
        color=discord.Color.purple()
    )

    embed.set_thumbnail(
        url=member.display_avatar.url
    )

    if banner_url:
        embed.set_image(
            url=banner_url
        )

    embed.set_footer(
        text=f"{ordinal(member.guild.member_count)} member!"
    )

    embed.timestamp = (
        discord.utils.utcnow()
    )

    content = fill_placeholders(
        ping_template,
        member
    )

    try:
        await channel.send(
            content=content,
            embed=embed
        )
    except discord.Forbidden:
        pass


# =========================
# MEMBER LEAVE
# =========================

@bot.event
async def on_member_remove(
    member: discord.Member
):
    settings = get_guild_settings(
        member.guild.id
    )

    channel_id = settings.get(
        "goodbye_channel"
    )

    if not channel_id:
        return

    channel = member.guild.get_channel(
        channel_id
    )

    if channel is None:
        return

    message_template = settings.get(
        "goodbye_message",
        (
            "〻 **GOODBYE {username}**\n"
            "\n"
            "» You have left **{server}**\n"
            "\n"
            "» Thanks for being part of WXRST\n"
            "\n"
            "» `{membercount}` members remain"
        )
    )

    text = fill_placeholders(
        message_template,
        member
    )

    embed = discord.Embed(
        description=text,
        color=discord.Color.red()
    )

    embed.set_thumbnail(
        url=member.display_avatar.url
    )

    try:
        await channel.send(
            embed=embed
        )
    except discord.Forbidden:
        pass


# =========================
# MODERATION
# =========================

@bot.tree.command(
    name="kick",
    description="Kick a member from the server"
)
@app_commands.describe(
    member="Who to kick",
    reason="Why are you kicking them?"
)
async def kick(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason given"
):
    if not interaction.user.guild_permissions.kick_members:
        await interaction.response.send_message(
            "You don't have permission to kick members.",
            ephemeral=True
        )
        return

    if (
        member.top_role >= interaction.user.top_role
        and interaction.user.id != interaction.guild.owner_id
    ):
        await interaction.response.send_message(
            "You can't kick someone with an equal or higher role than you.",
            ephemeral=True
        )
        return

    try:
        await member.kick(
            reason=f"{reason} (by {interaction.user})"
        )

        await interaction.response.send_message(
            f"👢 Kicked **{member}**. Reason: {reason}"
        )

    except discord.Forbidden:
        await interaction.response.send_message(
            "I don't have permission to kick that member.",
            ephemeral=True
        )


@bot.tree.command(
    name="ban",
    description="Ban a member from the server"
)
@app_commands.describe(
    member="Who to ban",
    reason="Why are you banning them?"
)
async def ban(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason given"
):
    if not interaction.user.guild_permissions.ban_members:
        await interaction.response.send_message(
            "You don't have permission to ban members.",
            ephemeral=True
        )
        return

    if (
        member.top_role >= interaction.user.top_role
        and interaction.user.id != interaction.guild.owner_id
    ):
        await interaction.response.send_message(
            "You can't ban someone with an equal or higher role than you.",
            ephemeral=True
        )
        return

    try:
        await member.ban(
            reason=f"{reason} (by {interaction.user})"
        )

        await interaction.response.send_message(
            f"🔨 Banned **{member}**. Reason: {reason}"
        )

    except discord.Forbidden:
        await interaction.response.send_message(
            "I don't have permission to ban that member.",
            ephemeral=True
        )


@bot.tree.command(
    name="timeout",
    description="Temporarily mute a member"
)
@app_commands.describe(
    member="Who to timeout",
    minutes="How many minutes",
    reason="Why are you timing them out?"
)
async def timeout(
    interaction: discord.Interaction,
    member: discord.Member,
    minutes: int,
    reason: str = "No reason given"
):
    if not interaction.user.guild_permissions.moderate_members:
        await interaction.response.send_message(
            "You don't have permission to timeout members.",
            ephemeral=True
        )
        return

    if minutes <= 0:
        await interaction.response.send_message(
            "Minutes must be greater than 0.",
            ephemeral=True
        )
        return

    duration = (
        discord.utils.utcnow()
        + datetime.timedelta(minutes=minutes)
    )

    try:
        await member.edit(
            timed_out_until=duration,
            reason=f"{reason} (by {interaction.user})"
        )

        await interaction.response.send_message(
            f"🔇 Timed out **{member}** for "
            f"{minutes} minute(s). Reason: {reason}"
        )

    except discord.Forbidden:
        await interaction.response.send_message(
            "I don't have permission to timeout that member.",
            ephemeral=True
        )


@bot.tree.command(
    name="warn",
    description="Give a member a warning"
)
@app_commands.describe(
    member="Who to warn",
    reason="Why are you warning them?"
)
async def warn(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str
):
    if not interaction.user.guild_permissions.moderate_members:
        await interaction.response.send_message(
            "You don't have permission to warn members.",
            ephemeral=True
        )
        return

    config = load_config()

    guild_key = str(
        interaction.guild.id
    )

    config.setdefault(
        guild_key,
        {}
    )

    config[guild_key].setdefault(
        "warnings",
        {}
    )

    config[guild_key]["warnings"].setdefault(
        str(member.id),
        []
    )

    config[guild_key]["warnings"][
        str(member.id)
    ].append(reason)

    save_config(config)

    count = len(
        config[guild_key]["warnings"][
            str(member.id)
        ]
    )

    await interaction.response.send_message(
        f"⚠️ Warned **{member}** "
        f"(warning #{count}). Reason: {reason}"
    )


@bot.tree.command(
    name="warnings",
    description="See a member's past warnings"
)
@app_commands.describe(
    member="Whose warnings to check"
)
async def warnings(
    interaction: discord.Interaction,
    member: discord.Member
):
    settings = get_guild_settings(
        interaction.guild.id
    )

    member_warnings = settings.get(
        "warnings",
        {}
    ).get(
        str(member.id),
        []
    )

    if not member_warnings:
        await interaction.response.send_message(
            f"**{member}** has no warnings.",
            ephemeral=True
        )
        return

    text = "\n".join(
        f"{i + 1}. {reason}"
        for i, reason in enumerate(member_warnings)
    )

    await interaction.response.send_message(
        f"⚠️ Warnings for **{member}**:\n{text}",
        ephemeral=True
    )


@bot.tree.command(
    name="clear",
    description="Delete recent messages"
)
@app_commands.describe(
    amount="How many messages to delete (max 100)"
)
async def clear(
    interaction: discord.Interaction,
    amount: app_commands.Range[int, 1, 100]
):
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message(
            "You don't have permission to delete messages.",
            ephemeral=True
        )
        return

    await interaction.response.defer(
        ephemeral=True
    )

    try:
        deleted = await interaction.channel.purge(
            limit=amount
        )

        await interaction.followup.send(
            f"🧹 Deleted {len(deleted)} message(s).",
            ephemeral=True
        )

    except discord.Forbidden:
        await interaction.followup.send(
            "I don't have permission to delete messages here.",
            ephemeral=True
        )


# =========================
# SLASH JOIN
# =========================

@bot.tree.command(
    name="join",
    description="Make the bot join your current voice channel"
)
async def join(
    interaction: discord.Interaction
):
    voice_state = interaction.user.voice

    if (
        voice_state is None
        or voice_state.channel is None
    ):
        await interaction.response.send_message(
            "You need to be in a voice channel first.",
            ephemeral=True
        )
        return

    voice_channel = voice_state.channel

    vc = discord.utils.get(
        bot.voice_clients,
        guild=interaction.guild
    )

    try:
        if vc is None:
            await voice_channel.connect()

        elif vc.channel != voice_channel:
            await vc.move_to(
                voice_channel
            )

        else:
            await interaction.response.send_message(
                f"I'm already in {voice_channel.mention}.",
                ephemeral=True
            )
            return

    except (
        discord.ClientException,
        discord.Forbidden
    ):
        await interaction.response.send_message(
            "Something went wrong trying to join.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"✅ Joined {voice_channel.mention}.",
        ephemeral=True
    )


# =========================
# MUSIC HELPERS
# =========================

def get_voice_client(
    guild: discord.Guild
):
    return discord.utils.get(
        bot.voice_clients,
        guild=guild
    )


async def ensure_music_voice(
    ctx: commands.Context
):
    if (
        ctx.author.voice is None
        or ctx.author.voice.channel is None
    ):
        return None

    voice_channel = (
        ctx.author.voice.channel
    )

    vc = get_voice_client(
        ctx.guild
    )

    if vc is None:
        vc = await voice_channel.connect()

    elif vc.channel != voice_channel:
        await vc.move_to(
            voice_channel
        )

    return vc


# =========================
# MUSIC: PLAY
# =========================

@bot.command(
    name="play",
    aliases=["p"]
)
async def play(
    ctx: commands.Context,
    *,
    query: str = None
):
    if ctx.guild is None:
        return

    if not query:
        await ctx.send(
            "Usage: `!play <song name or YouTube URL>`"
        )
        return

    if (
        ctx.author.voice is None
        or ctx.author.voice.channel is None
    ):
        await ctx.send(
            "You need to be in a voice channel first."
        )
        return

    try:
        vc = await ensure_music_voice(
            ctx
        )

        if vc is None:
            await ctx.send(
                "I couldn't join your voice channel."
            )
            return

        loop = asyncio.get_running_loop()

        song = await loop.run_in_executor(
            None,
            search_song,
            query
        )

        music_queues.setdefault(
            ctx.guild.id,
            []
        ).append(song)

        if vc.is_playing() or vc.is_paused():
            await ctx.send(
                f"Queued: **{song['title']}**"
            )
        else:
            await play_next_song(
                ctx.guild.id,
                vc
            )

            await ctx.send(
                f"Now playing: **{song['title']}**"
            )

    except Exception as e:
        print(
            f"⚠️ Music play error: {e}"
        )

        await ctx.send(
            "I couldn't play that song. "
            "Check FFmpeg and the music source."
        )


# =========================
# MUSIC: SEARCH
# =========================

@bot.command(
    name="search"
)
async def search(
    ctx: commands.Context,
    *,
    query: str = None
):
    if ctx.guild is None:
        return

    if not query:
        await ctx.send(
            "Usage: `!search <song name>`"
        )
        return

    try:
        loop = asyncio.get_running_loop()

        song = await loop.run_in_executor(
            None,
            search_song,
            query
        )

        await ctx.send(
            f"Found: **{song['title']}**"
        )

    except Exception as e:
        print(
            f"⚠️ Music search error: {e}"
        )

        await ctx.send(
            "I couldn't find that song."
        )


# =========================
# MUSIC: QUEUE
# =========================

@bot.command(
    name="queue",
    aliases=["q"]
)
async def queue(
    ctx: commands.Context
):
    if ctx.guild is None:
        return

    current = now_playing.get(
        ctx.guild.id
    )

    items = music_queues.get(
        ctx.guild.id,
        []
    )

    lines = []

    if current:
        lines.append(
            f"**Now playing:** {current['title']}"
        )

    if items:
        lines.append(
            "**Up next:**"
        )

        for i, song in enumerate(
            items[:10],
            start=1
        ):
            lines.append(
                f"`{i}.` {song['title']}"
            )

    if not lines:
        await ctx.send(
            "The music queue is empty."
        )
        return

    await ctx.send(
        "\n".join(lines)
    )


# =========================
# MUSIC: NOW PLAYING
# =========================

@bot.command(
    name="nowplaying",
    aliases=["np"]
)
async def nowplaying_command(
    ctx: commands.Context
):
    if ctx.guild is None:
        return

    song = now_playing.get(
        ctx.guild.id
    )

    if song is None:
        await ctx.send(
            "Nothing is currently playing."
        )
        return

    await ctx.send(
        f"**Now playing:** {song['title']}"
    )


# =========================
# MUSIC: PAUSE
# =========================

@bot.command(
    name="pause"
)
async def pause(
    ctx: commands.Context
):
    if ctx.guild is None:
        return

    vc = get_voice_client(
        ctx.guild
    )

    if vc is None or not vc.is_playing():
        await ctx.send(
            "Nothing is currently playing."
        )
        return

    vc.pause()

    await ctx.send(
        "Paused."
    )


# =========================
# MUSIC: RESUME
# =========================

@bot.command(
    name="resume"
)
async def resume(
    ctx: commands.Context
):
    if ctx.guild is None:
        return

    vc = get_voice_client(
        ctx.guild
    )

    if vc is None or not vc.is_paused():
        await ctx.send(
            "Nothing is paused."
        )
        return

    vc.resume()

    await ctx.send(
        "Resumed."
    )


# =========================
# MUSIC: SKIP
# =========================

@bot.command(
    name="skip",
    aliases=["s"]
)
async def skip(
    ctx: commands.Context
):
    if ctx.guild is None:
        return

    vc = get_voice_client(
        ctx.guild
    )

    if vc is None or not vc.is_playing():
        await ctx.send(
            "Nothing is currently playing."
        )
        return

    vc.stop()

    await ctx.send(
        "Skipped the current song."
    )


# =========================
# MUSIC: STOP
# =========================

@bot.command(
    name="stop"
)
async def stop(
    ctx: commands.Context
):
    if ctx.guild is None:
        return

    vc = get_voice_client(
        ctx.guild
    )

    music_queues[
        ctx.guild.id
    ] = []

    now_playing[
        ctx.guild.id
    ] = None

    if vc is None:
        await ctx.send(
            "I'm not in a voice channel."
        )
        return

    vc.stop()

    try:
        await vc.disconnect()
    except discord.HTTPException:
        pass

    await ctx.send(
        "Stopped the music and left the voice channel."
    )


# =========================
# MUSIC: SHUFFLE
# =========================

@bot.command(
    name="shuffle"
)
async def shuffle(
    ctx: commands.Context
):
    if ctx.guild is None:
        return

    items = music_queues.get(
        ctx.guild.id,
        []
    )

    if len(items) < 2:
        await ctx.send(
            "There aren't enough songs in the queue to shuffle."
        )
        return

    random.shuffle(
        items
    )

    await ctx.send(
        "Queue shuffled."
    )


# =========================
# MUSIC: CLEAR QUEUE
# =========================

@bot.command(
    name="clearqueue",
    aliases=["clearq"]
)
async def clearqueue(
    ctx: commands.Context
):
    if ctx.guild is None:
        return

    music_queues[
        ctx.guild.id
    ] = []

    await ctx.send(
        "Music queue cleared."
    )


# =========================
# MUSIC: VOLUME
# =========================

@bot.command(
    name="volume",
    aliases=["vol"]
)
async def volume(
    ctx: commands.Context,
    level: int = None
):
    if ctx.guild is None:
        return

    if (
        level is None
        or not 0 <= level <= 150
    ):
        await ctx.send(
            "Usage: `!volume <0-150>`"
        )
        return

    vc = get_voice_client(
        ctx.guild
    )

    if vc is None or vc.source is None:
        await ctx.send(
            "Nothing is currently playing."
        )
        return

    if isinstance(
        vc.source,
        discord.PCMVolumeTransformer
    ):
        vc.source.volume = (
            level / 100
        )

        await ctx.send(
            f"Volume set to **{level}%**."
        )
    else:
        await ctx.send(
            "Nothing is currently playing."
        )


# =========================
# MUSIC: REPLAY
# =========================

@bot.command(
    name="replay"
)
async def replay(
    ctx: commands.Context
):
    if ctx.guild is None:
        return

    song = now_playing.get(
        ctx.guild.id
    )

    vc = get_voice_client(
        ctx.guild
    )

    if song is None or vc is None:
        await ctx.send(
            "Nothing is currently playing."
        )
        return

    music_queues.setdefault(
        ctx.guild.id,
        []
    ).insert(
        0,
        song
    )

    vc.stop()

    await ctx.send(
        "Replaying the current song."
    )


# =========================
# MUSIC: JOIN
# =========================

@bot.command(
    name="connect",
    aliases=["musicjoin"]
)
async def music_join(
    ctx: commands.Context
):
    if ctx.guild is None:
        return

    if (
        ctx.author.voice is None
        or ctx.author.voice.channel is None
    ):
        await ctx.send(
            "You need to be in a voice channel first."
        )
        return

    try:
        vc = await ensure_music_voice(
            ctx
        )

        if vc is not None:
            await ctx.send(
                f"Joined **{ctx.author.voice.channel.name}**."
            )

    except (
        discord.ClientException,
        discord.Forbidden
    ):
        await ctx.send(
            "I couldn't join that voice channel. "
            "Check my Connect and Speak permissions."
        )


# =========================
# MUSIC: LEAVE
# =========================

@bot.command(
    name="leave",
    aliases=["disconnect", "dc"]
)
async def music_leave(
    ctx: commands.Context
):
    if ctx.guild is None:
        return

    vc = get_voice_client(
        ctx.guild
    )

    music_queues[
        ctx.guild.id
    ] = []

    now_playing[
        ctx.guild.id
    ] = None

    if vc is None:
        await ctx.send(
            "I'm not in a voice channel."
        )
        return

    try:
        await vc.disconnect()
    except discord.HTTPException:
        pass

    await ctx.send(
        "Left the voice channel."
    )


# =========================
# MUSIC: AUTOPLAY
# =========================

@bot.command(
    name="autoplay"
)
async def autoplay(
    ctx: commands.Context
):
    await ctx.send(
        "Autoplay is not implemented in the current player."
    )


# =========================
# MUSIC: LOOP
# =========================

@bot.command(
    name="loop"
)
async def loop_command(
    ctx: commands.Context
):
    await ctx.send(
        "Loop is not implemented in the current player."
    )


# =========================
# MUSIC: SEEK
# =========================

@bot.command(
    name="seek"
)
async def seek(
    ctx: commands.Context,
    percentage: int = None
):
    await ctx.send(
        "Seek is not available with the current player."
    )


# =========================
# ERROR HANDLER
# =========================

@bot.event
async def on_command_error(
    ctx: commands.Context,
    error
):
    if isinstance(
        error,
        commands.CommandNotFound
    ):
        return

    if isinstance(
        error,
        commands.MissingRequiredArgument
    ):
        await ctx.send(
            "You're missing a required argument."
        )
        return

    if isinstance(
        error,
        commands.BadArgument
    ):
        await ctx.send(
            "One of the arguments you entered is invalid."
        )
        return

    print(
        f"⚠️ Command error in {ctx.command}: {error}"
    )


# =========================
# START BOT
# =========================

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(
            "No token found! Open the .env file and put your bot token in DISCORD_TOKEN."
        )

    bot.run(TOKEN)
