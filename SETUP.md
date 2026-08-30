# Discord bot deployment and testing

## Files to upload

Upload `bot.py` and `requirements.txt` to the root of your existing bot server. Keep your existing `config.json`; this version uses the same configuration helpers and does not delete its settings.

Create or keep a `.env` file in the same folder:

```env
DISCORD_TOKEN=your_bot_token_here
GUILD_ID=your_home_server_id
ROLE_ID=your_notify_role_id
# Optional only when FFmpeg is in a non-standard location:
# FFMPEG_PATH=/usr/bin/ffmpeg
```

Never share or commit `.env`.

## Pterodactyl setup

1. Select a Python egg/image with Python 3.11+ (Python 3.14.7 is fine when supplied by the host).
2. Upload the files above and your existing `config.json` and `.env`.
3. Set the startup command to:

```bash
python3 -m pip install -r requirements.txt && python3 bot.py
```

4. Start the server and check the console for `Logged in as` and `Synced ... slash command(s)`.
5. The bot first looks for system `ffmpeg`, then the `imageio-ffmpeg` binary installed from requirements. If your host has FFmpeg at another path, add `FFMPEG_PATH=/path/to/ffmpeg` to `.env` and restart.

## Discord Developer Portal

1. In **Bot**, enable **Server Members Intent** and **Message Content Intent**. These are required by the existing welcome/autorole and automod features.
2. Invite the bot with the `bot` and `applications.commands` scopes.
3. Grant at least: View Channels, Send Messages, Embed Links, Attach Files, Read Message History, Manage Channels, Manage Roles, Connect, Speak, and Use Application Commands. The ticket system needs Manage Channels to create/rename/delete private ticket channels and Manage Roles to set member overwrites. Keep the existing moderation permissions only if you use their matching commands: Manage Messages, Kick Members, Ban Members, and Moderate Members.
4. Ensure the bot's server role is above every role it should assign and has access to the target welcome/goodbye and voice channels.

## Music commands

`/join`, `/leave`, `/play`, `/pause`, `/resume`, `/skip`, `/stop`, `/queue`, `/nowplaying`, `/volume`, `/shuffle`, `/remove`, `/loop`, `/musichelp`

The existing `/disconnect` and `/leavevc` aliases are retained; they behave like `/leave`.

## Ticket system setup

1. Create a private **ticket category**, a private **transcript channel**, and a private **ticket-log channel**. Give staff access; do not give regular members access to the transcript/log channels.
2. Run `/ticketconfig channels` and select those three channels.
3. Run `/ticketconfig supportrole` once for every support role. Administrators always have ticket access.
4. Optionally set limits with `/ticketconfig limits`, branding with `/ticketconfig panel`, features with `/ticketconfig features`, and custom buttons with `/ticketconfig category`.
5. Run `/ticketsetup` in the channel that should contain the public support panel.

Ticket records, IDs, staff statistics, and configuration are stored under `ticket_system` in your existing `config.json`. HTML transcripts are also saved in the server's `transcripts/` folder and attached to the configured transcript channel when a ticket closes. Keep that folder if you want local transcript copies to survive host restarts.

Ticket commands: `/ticketsetup`, `/ticketconfig ...`, `/ticketstats`, `/ticketstaff`, and `/ticket add|remove|close|reopen|delete|claim|unclaim`.

## Tests

1. Join a voice channel and run `/join`. The bot should join that channel. Run `/leave` afterward; it should leave and reset the queue.
2. Join voice and run `/play never gonna give you up` or `/play <YouTube URL>`. The bot should join automatically (if needed), extract audio without downloading a video file, and announce the title/requester.
3. Run `/play` with two more searches, then `/queue`. The first title is **Currently Playing** and the rest appear under **Up Next**. Test `/shuffle` and `/remove 2`.
4. While playing, run `/pause`, `/resume`, `/skip`, and `/stop`. Skip advances to the next waiting track; stop clears everything. Test `/loop` to repeat only the current track and `/volume 25` to set 25% volume.

## FFmpeg troubleshooting

Run `which ffmpeg` in the Pterodactyl console if shell access is available. If it prints a path, put that path in `FFMPEG_PATH` in `.env`. If it does not, keep `imageio-ffmpeg` in requirements or ask the host to provide FFmpeg. The bot responds with a clear error instead of crashing when no binary is available.

## YouTube / yt-dlp troubleshooting

If a video is private, unavailable, age-restricted, or YouTube returns **Sign in to confirm you're not a bot**, the bot reports a friendly failure and logs the technical error to the server console. It will not crash and does not try to bypass YouTube authentication or CAPTCHA. Update the dependency with `python3 -m pip install -U yt-dlp`, restart the bot, then try another public URL or a search query.
