# Discord Bot Setup Guide

## 1. What this bot needs

This bot is a Python Discord bot with:
- Moderation commands
- Welcome/goodbye
- Autorole
- Automod
- Notifications
- Autonickname
- Configuration commands
- Music commands
- Per-server music queues
- YouTube audio extraction using yt-dlp
- FFmpeg audio playback
- Slash commands

TTS/gTTS is not required.

---

## 2. GitHub setup

Put these files in the root of your GitHub repository:

```text
bot.py
requirements.txt
SETUP.md
```

If you are updating an existing bot, replace the old `bot.py` and `requirements.txt` with the new versions.

Do not upload your Discord bot token to GitHub.

---

## 3. Bot Hosting Net setup

Use your existing bot/server on Bot Hosting Net.

### Python version

Use a supported Python 3 version, preferably Python 3.11+ if available.

### Install command

If the host asks for an install command:

```bash
pip install -r requirements.txt
```

### Startup command

Use:

```bash
python bot.py
```

Then restart/redeploy the bot after updating the GitHub files.

---

## 4. Environment variables

The bot token must be stored as an environment variable/secret in your hosting panel, NOT inside `bot.py`.

Use the variable names required by the code.

If the code expects:

```text
TOKEN
```

set:

```text
TOKEN=YOUR_DISCORD_BOT_TOKEN
```

If it expects:

```text
GUILD_ID
```

set:

```text
GUILD_ID=YOUR_DISCORD_SERVER_ID
```

Do not add or rename variables unless they are required by `bot.py`.

---

## 5. Discord Developer Portal

Open your existing Discord application.

Go to:

**Developer Portal → Your Application → Bot**

Make sure the required Gateway Intents used by the bot are enabled.

For features such as member-based welcome/autorole/moderation functionality, the Server Members Intent may be required.

Only enable Message Content Intent if the bot code requires it.

Save the changes.

---

## 6. Bot permissions

Keep using your existing bot.

Make sure the bot has the permissions required for the features you use, such as:

- View Channels
- Send Messages
- Embed Links
- Read Message History
- Connect
- Speak
- Manage Messages
- Manage Roles
- Kick Members
- Ban Members

Do not give Administrator unless you intentionally want to.

---

## 7. Music setup

The music system uses `yt-dlp` for media extraction and FFmpeg for audio playback.

The Python packages should be installed from:

```bash
pip install -r requirements.txt
```

FFmpeg must also be available to the hosting environment.

If the bot logs say:

```text
FFmpeg not found
```

then install/enable FFmpeg using the hosting provider's available package/system option.

Do not put an FFmpeg executable inside the Discord bot token/config settings.

---

## 8. Updating the bot later

Whenever you receive a new version of the bot:

1. Open GitHub.
2. Replace `bot.py`.
3. Replace `requirements.txt` if it was updated.
4. Commit the changes.
5. Open Bot Hosting Net.
6. Pull/sync the latest GitHub files.
7. Reinstall requirements if `requirements.txt` changed.
8. Restart the bot.
9. Check the console for errors.
10. Test the slash commands in Discord.

---

## 9. Testing

After the bot starts, type:

```text
/
```

in your Discord server and check that the bot's slash commands appear.

The updated version is expected to contain approximately 30 unique slash commands.

Test:
- Moderation
- Welcome/goodbye
- Autorole
- Automod
- Configuration
- Notifications
- Music/playback
- Queue controls

---

## 10. Common problems

### Bot does not start

Check the hosting console.

Common causes:
- Wrong/missing token environment variable
- Missing Python package
- Incorrect startup command
- FFmpeg unavailable
- Python version incompatibility

### Slash commands do not appear

Check:
- The bot is online.
- The bot was invited with the `applications.commands` scope.
- The bot has access to the server.
- `GUILD_ID` is correct if the code uses guild restrictions.
- Restart the bot and wait for command synchronization.

### Music does not play

Check:
- The bot can connect to the voice channel.
- The bot has Connect and Speak permissions.
- FFmpeg is available.
- `yt-dlp` installed correctly.
- The console for the exact extraction/voice error.

### Commands work in one server but not another

The bot may intentionally restrict commands using `GUILD_ID`.

Check the environment variables and the configuration in `bot.py`.

---

## 11. Security

NEVER share:
- Discord bot token
- Hosting panel password
- GitHub access token
- API keys

If the Discord bot token is exposed, regenerate/reset the token in the Discord Developer Portal immediately.

Keep secrets in Bot Hosting Net environment variables/secrets.

---

## 12. Quick setup summary

```text
GitHub
  ↓
Replace bot.py + requirements.txt
  ↓
Bot Hosting Net
  ↓
Install:
pip install -r requirements.txt
  ↓
Start:
python bot.py
  ↓
Discord Developer Portal
  ↓
Check required intents
  ↓
Discord server
  ↓
Test /
```

Your existing Discord bot can be updated; you do NOT need to create a new Discord application just to add these features.
