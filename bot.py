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
    if channel is None:
        await interaction.response.send_message(
            "I couldn't find that channel. Make sure you pasted the Channel ID number "
            "(right-click the channel → Copy Channel ID). You may need to turn on Developer Mode first "
            "in Discord Settings → Advanced.",
            ephemeral=True,
        )
        return

    await interaction.response.send_modal(WelcomeModal(channel))


@bot.tree.command(name="setgoodbye", description="Set the channel and message for when members leave")
@app_commands.describe(channel_id="The channel's ID number (right-click the channel → Copy Channel ID)")
async def setgoodbye(interaction: discord.Interaction, channel_id: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only a server admin can set this up.", ephemeral=True)
        return

    channel = resolve_channel(interaction.guild, channel_id)
    if channel is None:
        await interaction.response.send_message(
            "I couldn't find that channel. Make sure you pasted the Channel ID number "
            "(right-click the channel → Copy Channel ID). You may need to turn on Developer Mode first "
            "in Discord Settings → Advanced.",
            ephemeral=True,
        )
        return

    await interaction.response.send_modal(GoodbyeModal(channel))


@bot.tree.command(name="setautorole", description="Automatically give new members a role when they join")
@app_commands.describe(role="The role to give automatically (leave empty to turn autorole off)")
async def setautorole(interaction: discord.Interaction, role: discord.Role = None):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only a server admin can set this up.", ephemeral=True)
        return

    if role is None:
        set_guild_setting(interaction.guild.id, "autorole_id", None)
        await interaction.response.send_message("✅ Autorole turned off.", ephemeral=True)
        return

    if role >= interaction.guild.me.top_role:
        await interaction.response.send_message(
            f"⚠️ I can't assign **{role.name}** because it's higher than my own role in the server settings. "
            f"Move my bot's role above it in Server Settings → Roles.",
            ephemeral=True,
        )
        return

    set_guild_setting(interaction.guild.id, "autorole_id", role.id)
    await interaction.response.send_message(
        f"✅ New members will automatically get the **{role.name}** role.", ephemeral=True
    )


@bot.tree.command(name="setautonickname", description="Automatically set a nickname format for new members")
@app_commands.describe(
    format="Use {username} as a placeholder, e.g. 'New | {username}'. Leave empty to turn off."
)
async def setautonickname(interaction: discord.Interaction, format: str = None):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only a server admin can set this up.", ephemeral=True)
        return

    set_guild_setting(interaction.guild.id, "autonickname_format", format)
    if format:
        await interaction.response.send_message(
            f"✅ New members will be renamed using the format: `{format}`", ephemeral=True
        )
    else:
        await interaction.response.send_message("✅ Autonickname turned off.", ephemeral=True)


@bot.tree.command(name="automod", description="Turn the bad-word and spam filter on or off")
@app_commands.describe(state="Turn automod on or off")
@app_commands.choices(state=[
    app_commands.Choice(name="on", value="on"),
    app_commands.Choice(name="off", value="off"),
])
async def automod(interaction: discord.Interaction, state: app_commands.Choice[str]):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only a server admin can set this up.", ephemeral=True)
        return

    set_guild_setting(interaction.guild.id, "automod_enabled", state.value == "on")
    await interaction.response.send_message(f"✅ Automod is now **{state.value}**.", ephemeral=True)


@bot.tree.command(name="addbadword", description="Add a word for automod to delete automatically")
@app_commands.describe(word="The word to block")
async def addbadword(interaction: discord.Interaction, word: str):
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
async def removebadword(interaction: discord.Interaction, word: str):
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
        await interaction.response.send_message(f"That word wasn't on the list.", ephemeral=True)


recent_messages = {}
tts_queues = {}


async def play_next_tts(guild_id: int, vc: discord.VoiceClient):
    """Play the next queued TTS message. Stays connected when the queue empties —
    only /leave, /disconnect, or /leavevc actually disconnects the bot."""
    queue = tts_queues.get(guild_id, [])
    if not queue:
        return

    text = queue.pop(0)
    filename = f"tts_{guild_id}.mp3"

    def generate_audio():
        gTTS(text=text, lang="en").save(filename)

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, generate_audio)

    def after_playing(error):
        if error:
            print(f"⚠️ TTS playback error: {error}")
        fut = asyncio.run_coroutine_threadsafe(play_next_tts(guild_id, vc), bot.loop)
        try:
            fut.result()
        except Exception as e:
            print(f"⚠️ TTS follow-up error: {e}")

    vc.play(discord.FFmpegPCMAudio(filename), after=after_playing)


async def handle_tts_message(message: discord.Message, spoken_text: str):
    voice_state = message.author.voice
    if voice_state is None or voice_state.channel is None:
        return

    voice_channel = voice_state.channel
    vc = discord.utils.get(bot.voice_clients, guild=message.guild)

    try:
        if vc is None:
            vc = await voice_channel.connect()
        elif vc.channel != voice_channel:
            await vc.move_to(voice_channel)
    except discord.ClientException:
        return

    full_text = f"{message.author.display_name} said {spoken_text}"
    tts_queues.setdefault(message.guild.id, []).append(full_text)

    if not vc.is_playing():
        await play_next_tts(message.guild.id, vc)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return

    if message.content.startswith("."):
        spoken_text = message.content[1:].strip()
        if spoken_text:
            await handle_tts_message(message, spoken_text)
        return

    settings = get_guild_settings(message.guild.id)

    if settings.get("automod_enabled"):
        if not message.author.guild_permissions.administrator:
            bad_words = settings.get("bad_words", [])
            content_lower = message.content.lower()
            if any(word in content_lower for word in bad_words):
                try:
                    await message.delete()
                    await message.channel.send(
                        f"🚫 {message.author.mention}, that word isn't allowed here.", delete_after=5
                    )
                except discord.Forbidden:
                    pass
                return

            key = (message.guild.id, message.author.id)
            now = discord.utils.utcnow().timestamp()
            timestamps = recent_messages.get(key, [])
            timestamps = [t for t in timestamps if now - t < 5]
            timestamps.append(now)
            recent_messages[key] = timestamps

            if len(timestamps) > 5:
                try:
                    await message.delete()
                    await message.channel.send(
                        f"🚫 {message.author.mention}, please slow down (you're sending messages too fast).",
                        delete_after=5,
                    )
                except discord.Forbidden:
                    pass
                return

    await bot.process_commands(message)


@bot.event
async def on_member_join(member: discord.Member):
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
        new_nick = fill_placeholders(nickname_format, member)[:32]
        try:
            await member.edit(nick=new_nick, reason="Autonickname")
        except discord.Forbidden:
            pass

    channel_id = settings.get("welcome_channel")
    if not channel_id:
        return

    channel = member.guild.get_channel(channel_id)
    if channel is None:
        return

    title_template = settings.get("welcome_title", "WELCOME TO {server}")
    desc_template = settings.get(
        "welcome_message",
        "• Welcome To **{server}**\n⚠️ Enjoy Ur Stay Here\n➤ {user}\n➤ {username}\n➤ Acc Created : {joindate}",
    )
    ping_template = settings.get("welcome_ping", "{user} Welcome")
    banner_url = settings.get("welcome_banner")

    embed = discord.Embed(
        title=fill_placeholders(title_template, member),
        description=fill_placeholders(desc_template, member),
        color=discord.Color.purple(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    if banner_url:
        embed.set_image(url=banner_url)
    embed.set_footer(text=f"{ordinal(member.guild.member_count)} member!")
    embed.timestamp = discord.utils.utcnow()

    content = fill_placeholders(ping_template, member)
    await channel.send(content=content, embed=embed)


@bot.event
async def on_member_remove(member: discord.Member):
    settings = get_guild_settings(member.guild.id)
    channel_id = settings.get("goodbye_channel")
    if not channel_id:
        return

    channel = member.guild.get_channel(channel_id)
    if channel is None:
        return

    message_template = settings.get(
        "goodbye_message",
        "〻 GOODBYE {username}\n» You have left {server}\n» Thanks for being part of WXRST\n» {membercount} members remain",
    )
    text = fill_placeholders(message_template, member)

    embed = discord.Embed(description=text, color=discord.Color.red())
    embed.set_thumbnail(url=member.display_avatar.url)
    await channel.send(embed=embed)


@bot.tree.command(name="kick", description="Kick a member from the server")
@app_commands.describe(member="Who to kick", reason="Why are you kicking them?")
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given"):
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
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given"):
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
async def timeout(interaction: discord.Interaction, member: discord.Member, minutes: int, reason: str = "No reason given"):
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
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    if not interaction.user.guild_permissions.moderate_members:
        await interaction.response.send_message("You don't have permission to warn members.", ephemeral=True)
        return

    config = load_config()
    guild_key = str(interaction.guild.id)
    config.setdefault(guild_key, {})
    config[guild_key].setdefault("warnings", {})
    config[guild_key]["warnings"].setdefault(str(member.id), [])
    config[guild_key]["warnings"][str(member.id)].append(reason)
    save_config(config)

    count = len(config[guild_key]["warnings"][str(member.id)])
    await interaction.response.send_message(f"⚠️ Warned **{member}** (warning #{count}). Reason: {reason}")


@bot.tree.command(name="warnings", description="See a member's past warnings")
@app_commands.describe(member="Whose warnings to check")
async def warnings(interaction: discord.Interaction, member: discord.Member):
    settings = get_guild_settings(interaction.guild.id)
    member_warnings = settings.get("warnings", {}).get(str(member.id), [])

    if not member_warnings:
        await interaction.response.send_message(f"**{member}** has no warnings.", ephemeral=True)
        return

    text = "\n".join(f"{i+1}. {reason}" for i, reason in enumerate(member_warnings))
    await interaction.response.send_message(f"⚠️ Warnings for **{member}**:\n{text}", ephemeral=True)


@bot.tree.command(name="clear", description="Delete a number of recent messages in this channel")
@app_commands.describe(amount="How many messages to delete (max 100)")
async def clear(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("You don't have permission to delete messages.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"🧹 Deleted {len(deleted)} message(s).", ephemeral=True)


@bot.tree.command(name="join", description="Make the bot join your current voice channel")
async def join(interaction: discord.Interaction):
    voice_state = interaction.user.voice
    if voice_state is None or voice_state.channel is None:
        await interaction.response.send_message("You need to be in a voice channel first.", ephemeral=True)
        return

    voice_channel = voice_state.channel
    vc = discord.utils.get(bot.voice_clients, guild=interaction.guild)

    try:
        if vc is None:
            await voice_channel.connect()
        elif vc.channel != voice_channel:
            await vc.move_to(voice_channel)
        else:
            await interaction.response.send_message(f"I'm already in {voice_channel.mention}.", ephemeral=True)
            return
    except discord.ClientException:
        await interaction.response.send_message("Something went wrong trying to join.", ephemeral=True)
        return

    await interaction.response.send_message(f"✅ Joined {voice_channel.mention}.", ephemeral=True)


async def leave_voice_channel(interaction: discord.Interaction):
    vc = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    if vc is None:
        await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
        return

    tts_queues[interaction.guild.id] = []
    await vc.disconnect()
    await interaction.response.send_message("👋 Left the voice channel.", ephemeral=True)


@bot.tree.command(name="leave", description="Make the bot leave the voice channel (stops TTS)")
async def leave(interaction: discord.Interaction):
    await leave_voice_channel(interaction)


@bot.tree.command(name="disconnect", description="Make the bot leave the voice channel (stops TTS)")
async def disconnect(interaction: discord.Interaction):
    await leave_voice_channel(interaction)


@bot.tree.command(name="leavevc", description="Make the bot leave the voice channel (stops TTS)")
async def leavevc(interaction: discord.Interaction):
    await leave_voice_channel(interaction)


@bot.tree.command(name="skip", description="Skip the message currently being spoken")
async def skip(interaction: discord.Interaction):
    vc = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    if vc is None or not vc.is_playing():
        await interaction.response.send_message("Nothing is being spoken right now.", ephemeral=True)
        return

    vc.stop()
    await interaction.response.send_message("⏭️ Skipped.", ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(
            "No token found! Open the .env file and paste your bot token into DISCORD_TOKEN."
        )
    bot.run(TOKEN)
