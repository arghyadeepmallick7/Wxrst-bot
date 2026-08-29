# Wxrst DM Bot — Setup Guide (for total beginners)

This bot lives inside your existing bot app **Freakky Labs**. It adds one
command, `/notify`, that DMs everyone in **Team Wxrst** who has your chosen
role (default: `wxrst`).

## What you need to do, in order

1. **Turn on the "Members" permission for your bot**
   - Go to https://discord.com/developers/applications
   - Click **Freakky Labs**
   - Click **Bot** on the left
   - Scroll to **Privileged Gateway Intents**
   - Turn ON **Server Members Intent**
   - Click **Save Changes**

2. **Get your bot's token**
   - Still on the **Bot** page, click **Reset Token** (or **Copy** if you already have one showing)
   - Copy the long code that appears — this is secret, like a password. Never share it or post it anywhere.

3. **Fill in the `.env` file**
   - In this folder, make a copy of `.env.example` and rename the copy to `.env`
   - Paste your token after `DISCORD_TOKEN=`
   - Make sure `ROLE_NAME=` matches your role's exact name (default is `wxrst`)

4. **Install the required packages** (run this in a terminal, inside this folder)
   ```
   pip install -r requirements.txt
   ```

5. **Run the bot**
   ```
   python bot.py
   ```
   You should see `✅ Logged in as Freakky Labs`.

6. **Use it in Discord**
   - In any channel in Team Wxrst, type `/notify`
   - Discord will ask for a `message` — type what you want to say, e.g. "Come to voice chat now!"
   - Press Enter — the bot DMs everyone with the `wxrst` role.

## Notes
- Only server **admins** can use `/notify` — this stops random members from spamming DMs.
- If someone has their DMs closed, the bot can't reach them — it'll tell you how many it missed.
- Keep your `.env` file private. Never upload it to GitHub or share it with anyone.
