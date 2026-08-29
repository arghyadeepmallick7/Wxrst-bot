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
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

# Load secrets from the .env file (never put your token directly in this file)
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
ROLE_ID = int(os.getenv("ROLE_ID", "0"))  # set ROLE_ID in .env to your role's ID number

# "Intents" are permissions the bot needs from Discord.
# members = the bot is allowed to see the server's member list and their roles.
intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------------------------------------------------------------------
# SIMPLE SETTINGS STORAGE
# ---------------------------------------------------------------------------
# We save each server's welcome/goodbye settings into a small file called
# config.json, so they don't get erased every time the bot restarts.
import json

CONFIG_FILE = "config.json"


def load_config():
    """Read config.json from disk. If it doesn't exist yet, start empty."""
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def save_config(data):
    """Write the settings back to config.json."""
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_guild_settings(guild_id: int) -> dict:
    """Get the settings dict for one server (creates an empty one if needed)."""
    config = load_config()
    return config.get(str(guild_id), {})


def set_guild_setting(guild_id: int, key: str, value):
    """Save one setting (like 'welcome_channel') for one server."""
    config = load_config()
    guild_key = str(guild_id)
    if guild_key not in config:
        config[guild_key] = {}
    config[guild_key][key] = value
    save_config(config)


def fill_placeholders(text: str, member: discord.Member) -> str:
    """Replace {user}, {username}, {server}, {membercount}, {joindate} with real values."""
    return (
        text.replace("{user}", member.mention)
        .replace("{username}", member.name)
        .replace("{server}", member.guild.name)
        .replace("{membercount}", str(member.guild.member_count))
        .replace("{ordinal}", ordinal(member.guild.member_count))
        .replace("{joindate}", member.created_at.strftime("%d/%b/%Y"))
    )


def ordinal(n: int) -> str:
    """Turn 222 into '222th', 1 into '1st', 2 into '2nd', 3 into '3rd', etc."""
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


@bot.tree.command(name="notify", description="DM everyone who has the special role")
@app_commands.describe(message="What do you want to tell them? (e.g. 'Come to voice chat now!')")
async def notify(interaction: discord.Interaction, message: str):
    # Only let server admins use this command, so random members can't spam DMs
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

    # Let the admin know we're working on it (DMs can take a few seconds for big servers)
    await interaction.response.send_message("Sending DMs now... 📨", ephemeral=True)

    sent = 0
    failed = 0
    for member in role.members:
        if member.bot:
            continue  # never DM other bots
        try:
            await member.send(
                f"📢 **Message from {interaction.guild.name}:**\n\n{message}"
            )
            sent += 1
        except discord.Forbidden:
            # This happens when someone has DMs turned off, or blocked the bot
            failed += 1

    await interaction.followup.send(
        f"Done! ✅ Sent to **{sent}** members.\n"
        f"❌ Could not reach **{failed}** members (their DMs are probably closed).",
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# WELCOMER
# ---------------------------------------------------------------------------

@bot.tree.command(name="setwelcome", description="Set up a fancy welcome embed for new members")
@app_commands.describe(
    channel="Which channel should welcome messages go in?",
    title="Big bold title at the top of the embed (optional)",
    description="Main text. Use {user} {username} {server} {ordinal} {membercount} {joindate} (optional)",
    banner_url="Link to an image to show big at the bottom of the embed (optional)",
    ping_text="Plain text shown above the embed, e.g. 'Hey {username} Welcome' (optional)",
)
async def setwelcome(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    title: str = None,
    description: str = None,
    banner_url: str = None,
    ping_text: str = None,
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only a server admin can set this up.", ephemeral=True)
        return

    default_title = "WELCOME TO {server}"
    default_description = (
        "• Welcome To **{server}**\n"
        "⚠️ Enjoy Ur Stay Here\n"
        "➤ {user}\n"
        "➤ {username}\n"
        "➤ Acc Created : {joindate}"
    )
    default_ping = "{user} Welcome"

    set_guild_setting(interaction.guild.id, "welcome_channel", channel.id)
    set_guild_setting(interaction.guild.id, "welcome_title", title or default_title)
    set_guild_setting(interaction.guild.id, "welcome_message", description or default_description)
    set_guild_setting(interaction.guild.id, "welcome_ping", ping_text or default_ping)
    if banner_url:
        set_guild_setting(interaction.guild.id, "welcome_banner", banner_url)

    await interaction.response.send_message(
        f"✅ Fancy welcome messages will now be sent in {channel.mention}. "
        f"Try having someone join (or use a test account) to see it!",
        ephemeral=True,
    )


@bot.tree.command(name="setgoodbye", description="Set the channel and message for when members leave")
@app_commands.describe(
    channel="Which channel should goodbye messages go in?",
    message="Use {user}, {username}, {server}, {membercount} as placeholders (optional)",
)
async def setgoodbye(interaction: discord.Interaction, channel: discord.TextChannel, message: str = None):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only a server admin can set this up.", ephemeral=True)
        return

    default_message = "👋 **{username}** has left **{server}**. We're now {membercount} members."
    set_guild_setting(interaction.guild.id, "goodbye_channel", channel.id)
    set_guild_setting(interaction.guild.id, "goodbye_message", message or default_message)

    await interaction.response.send_message(
        f"✅ Goodbye messages will now be sent in {channel.mention}.", ephemeral=True
    )


@bot.event
async def on_member_join(member: discord.Member):
    settings = get_guild_settings(member.guild.id)
    channel_id = settings.get("welcome_channel")
    if not channel_id:
        return  # nothing set up for this server yet

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
        return  # nothing set up for this server yet

    channel = member.guild.get_channel(channel_id)
    if channel is None:
        return

    message_template = settings.get("goodbye_message", "👋 **{username}** has left **{server}**.")
    text = fill_placeholders(message_template, member)

    embed = discord.Embed(description=text, color=discord.Color.red())
    embed.set_thumbnail(url=member.display_avatar.url)
    await channel.send(embed=embed)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(
            "No token found! Open the .env file and paste your bot token into DISCORD_TOKEN."
        )
    bot.run(TOKEN)
