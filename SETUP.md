Discord bot deployment and testing

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

4. Start or fully restart the server and check the console for `Logged in as` and `Synced ... slash command(s)`. Commands, including `/vcnotify`, are synced directly to `GUILD_ID` and should appear immediately after the restart. If `GUILD_ID` is empty, the bot syncs directly to every guild it is currently in.
5. The bot first looks for system `ffmpeg`, then the `imageio-ffmpeg` binary installed from requirements. If your host has FFmpeg at another path, add `FFMPEG_PATH=/path/to/ffmpeg` to `.env` and restart.

## Discord Developer Portal

1. In **Bot**, enable **Server Members Intent** and **Message Content Intent**. Message Content Intent is also required for `.[text]` TTS; without it Discord delivers no readable message text to the bot. Keep **Voice States** enabled under Gateway Intents so target voice-channel notifications can receive join/leave events.
2. Invite the bot with the `bot` and `applications.commands` scopes.
3. Grant at least: View Channels, Send Messages, Embed Links, Attach Files, Read Message History, Manage Channels, Manage Roles, **View Audit Log**, Connect, Speak, and Use Application Commands. View Audit Log lets the departure-DM system reliably distinguish kicks from voluntary leaves. The ticket system needs Manage Channels to create/rename/delete private ticket channels and Manage Roles to set member overwrites. Keep the existing moderation permissions only if you use their matching commands: Manage Messages, Kick Members, Ban Members, and Moderate Members.
4. Ensure the bot's server role is above every role it should assign and has access to the target welcome/goodbye and voice channels.

## Music commands

`/join`, `/leave`, `/play`, `/pause`, `/resume`, `/skip`, `/stop`, `/queue`, `/nowplaying`, `/volume`, `/shuffle`, `/remove`, `/loop`, `/musichelp`

The existing `/disconnect` and `/leavevc` aliases are retained; they behave like `/leave`.

## Text-to-speech

While you are in the same voice channel as the bot, send a normal text message in the form:

```text
.[hello everyone]
```

The bot speaks the text inside the brackets. Empty messages and messages longer than 300 characters are rejected safely. TTS automatically joins your voice channel if the bot is not already connected; it needs **Connect** and **Speak** permissions. It uses the same voice connection as music, never creates a second one, and waits until the current music track finishes before speaking queued TTS messages. `gTTS` needs outbound internet access from the hosting server to generate speech.

Optional per-guild `config.json` settings are stored under `tts`: `enabled`, `prefix` (default `.[`), `max_length` (default `300`), `language` (default `en`), and `auto_join` (default `true`).

## Welcome, moderation, and voice notifications

These features are disabled or configurable per server, so deploying the new file does not unexpectedly DM members or post voice alerts.

- Welcome DM: run `/welcomedm enable`, then optionally `/welcomedm setmessage`. Use `/welcomedm test` to DM a preview to yourself. Available placeholders: `{USER}`, `{USERNAME}`, `{DISPLAY_NAME}`, `{SERVER_NAME}`, and `{SERVER_ID}`.
- Timeout/ban DMs: moderation DMs are enabled by default. Use `/moderationdm settings` to enable or disable them and `/moderationdm setmessage` to customise a timeout or ban message. Templates also support `{REASON}`, `{DURATION}`, and `{MODERATOR}`. A failed DM is logged only; the timeout or ban still happens.
- Voice join/leave pings: run `/vcnotify set` and choose the **one** voice channel to watch and the text channel that receives alerts. This enables it. Use `/vcnotify messages`, `/vcnotify disable`, `/vcnotify enable`, and `/vcnotify status` as needed. Members are pinged only when they enter or leave that selected channel, including moves in or out; bots, mute/deafen updates, and movement between other channels are ignored.

The bot needs **View Channel** and **Send Messages** permission in the configured alert channel. It does not join or need to be in the watched voice channel.

### Voice notification test

1. Restart the bot and confirm its console says it synced slash commands.
2. Run `/vcnotify set`, select the watched voice channel, then select the text channel where alerts should be sent. This command enables notifications.
3. Join the watched channel, leave it, then move into and out of it. Each action should produce exactly one alert. Use `/vcnotify status` to verify the saved channels if no alert arrives.

### TTS test

1. Confirm **Message Content Intent** is enabled in the Discord Developer Portal and restart the bot after enabling it.
2. Join a voice channel in which the bot has **Connect** and **Speak** permissions.
3. Send exactly `.[hello]` in a normal server text channel. The bot joins (if needed), generates speech, and plays it through the same connection used for music.
4. If the bot reports a speech-generation failure, permit outbound HTTPS access from the host for gTTS. If it reports FFmpeg unavailable, keep `imageio-ffmpeg` in `requirements.txt` or set `FFMPEG_PATH`.

## Ticket system setup

1. Create a private **ticket category**, a private **transcript channel**, and a private **ticket-log channel**. Give staff access; do not give regular members access to the transcript/log channels.
2. Run `/ticketconfig channels` and select those three channels.
3. Run `/ticketconfig supportrole` once for every support role. Administrators always have ticket access.
4. Optionally set limits with `/ticketconfig limits`, branding with `/ticketconfig panel`, features with `/ticketconfig features`, and custom buttons with `/ticketconfig category`.
5. Run `/ticketsetup` in the channel that should contain the public support panel.

Ticket records, IDs, staff statistics, and configuration are stored under `ticket_system` in your existing `config.json`. HTML transcripts are also saved in the server's `transcripts/` folder and attached to the configured transcript channel when a ticket closes. Keep that folder if you want local transcript copies to survive host restarts.

Ticket commands: `/ticketsetup`, `/ticketconfig ...`, `/ticketstats`, `/ticketstaff`, and `/ticket add|remove|close|reopen|delete|claim|unclaim`.

### Transcript and auto-delete checklist

Tickets are intentionally closed before they are deleted. Auto-delete defaults to **Never** (`0`) so that transcripts are not lost.

1. Run `/ticketconfig channels` and set a private **transcript channel**.
2. In that channel, grant the bot **View Channel**, **Send Messages**, **Attach Files**, and **Read Message History**.
3. Run `/ticketconfig limits` and set `auto_delete_minutes` to a value above `0` (for example, `5`).
4. Run `/ticketconfig status` to confirm both settings.
5. For an already closed ticket, run `/ticket retrytranscript` inside that ticket. Once delivery succeeds, auto-delete is scheduled using the configured delay.

The bot now acknowledges the close confirmation immediately, generates the transcript before it locks/deletes a channel, and keeps the ticket open when transcript generation itself fails. Pending delete deadlines are stored in `config.json` and recovered after a restart.

## Departure DM setup

1. Run `/departure setinvite` with your server invite, then customize each template with `/departure setmessage`.
2. Use `/departure settings` to choose which of leave, kick, and ban send DMs.
3. Run `/departure test` for each type; it safely DMs only the administrator who runs the command.
4. Run `/departure enable` when you are ready. Use `/departure disable` to pause all departure DMs.

The bot waits briefly before checking kick audit logs. If it cannot confirm a kick, it safely treats the departure as a voluntary leave. Bans use Discord's ban event, so they do not receive the voluntary-leave template.

## Tests

1. Join a voice channel and run `/join`. The bot should join that channel. Run `/leave` afterward; it should leave and reset the queue.
2. Join voice and run `/play never gonna give you up` or `/play <YouTube URL>`. The bot should join automatically (if needed), extract audio without downloading a video file, and announce the title/requester.
3. Run `/play` with two more searches, then `/queue`. The first title is **Currently Playing** and the rest appear under **Up Next**. Test `/shuffle` and `/remove 2`.
4. While playing, run `/pause`, `/resume`, `/skip`, and `/stop`. Skip advances to the next waiting track; stop clears everything. Test `/loop` to repeat only the current track and `/volume 25` to set 25% volume.

## FFmpeg troubleshooting

Run `which ffmpeg` in the Pterodactyl console if shell access is available. If it prints a path, put that path in `FFMPEG_PATH` in `.env`. If it does not, keep `imageio-ffmpeg` in requirements or ask the host to provide FFmpeg. The bot responds with a clear error instead of crashing when no binary is available.

## YouTube / yt-dlp troubleshooting

If a video is private, unavailable, age-restricted, or YouTube returns **Sign in to confirm you're not a bot**, the bot reports a friendly failure and logs the technical error to the server console. It will not crash and does not try to bypass YouTube authentication or CAPTCHA. Update the dependency with `python3 -m pip install -U yt-dlp`, restart the bot, then try another public URL or a search query.
