        or interaction.user.guild_permissions.administrator
    )


@bot.tree.command(name="join", description="Make the bot join your voice channel")
async def join_voice(interaction: discord.Interaction) -> None:
    if not is_voice_admin(interaction):
        await interaction.response.send_message(
            "You need the Manage Server permission to use this command.", ephemeral=True
        )
        return

    member = interaction.user
    voice_state = member.voice
    if voice_state is None or not isinstance(voice_state.channel, discord.VoiceChannel):
        await interaction.response.send_message(
            "Join a normal voice channel first, then run `/join`.", ephemeral=True
        )
        return

    channel = voice_state.channel
    permissions = channel.permissions_for(interaction.guild.me)
    if not permissions.connect or not permissions.speak:
        await interaction.response.send_message(
            "I need **Connect** and **Speak** permissions in that voice channel.",
            ephemeral=True,
        )
        return

    voice_client = interaction.guild.voice_client
    try:
        if voice_client is None or not voice_client.is_connected():
            await channel.connect(self_deaf=True)
            await interaction.response.send_message(
                f"Joined {channel.mention}. I will stay here until `/leave` is used."
            )
        elif voice_client.channel.id == channel.id:
            await interaction.response.send_message(
                f"I am already in {channel.mention} and will stay until `/leave`. ",
                ephemeral=True,
            )
        else:
            await voice_client.move_to(channel)
            await interaction.response.send_message(
                f"Moved to {channel.mention}. I will stay until `/leave` is used."
            )
    except discord.ClientException as error:
        await interaction.response.send_message(
            f"I could not join that voice channel: {error}", ephemeral=True
        )


@bot.tree.command(name="leave", description="Make the bot leave its current voice channel")
async def leave_voice(interaction: discord.Interaction) -> None:
    if not is_voice_admin(interaction):
        await interaction.response.send_message(
            "You need the Manage Server permission to use this command.", ephemeral=True
        )
        return

    voice_client = interaction.guild.voice_client
    if voice_client is None or not voice_client.is_connected():
        await interaction.response.send_message("I am not in a voice channel.", ephemeral=True)
        return

    worker = voice_announcement_workers.pop(interaction.guild.id, None)
    if worker is not None:
        worker.cancel()
    if voice_client.is_playing() or voice_client.is_paused():
        voice_client.stop()
    await voice_client.disconnect(force=True)
    await interaction.response.send_message("Left the voice channel.")


@bot.event
async def on_voice_state_update(
    member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
) -> None:
    """Speak only for people entering or leaving the channel where the bot is."""
    if member.bot or before.channel == after.channel:
        return

    voice_client = member.guild.voice_client
    if voice_client is None or not voice_client.is_connected() or voice_client.channel is None:
        return
    if not isinstance(voice_client.channel, discord.VoiceChannel):
        return

    bot_channel = voice_client.channel
    if after.channel is bot_channel:
        queue_voice_announcement(
            member.guild, bot_channel, f"{member.display_name} joined the voice channel"
        )
    elif before.channel is bot_channel:
        queue_voice_announcement(
            member.guild, bot_channel, f"{member.display_name} left the voice channel"
        )


# Required one-time install:
#   pip install -U "discord.py[voice]" gTTS python-dotenv
# FFmpeg must also be installed and available on the machine PATH.
