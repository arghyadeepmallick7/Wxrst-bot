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
@ticket_group.command(name="retrytranscript", description="Generate and resend this closed ticket's transcript")
async def ticket_retrytranscript(interaction: discord.Interaction) -> None:
    ticket = next((item for item in ticket_settings(interaction.guild.id)["tickets"].values() if item.get("channel_id") == interaction.channel_id), None)
    settings = ticket_settings(interaction.guild.id)
    if not ticket:
        await interaction.response.send_message("Use this command inside a ticket channel.", ephemeral=True); return
    if not is_ticket_staff(interaction.user, settings) or ticket.get("status") != "closed":
        await interaction.response.send_message("Only support staff can retry a transcript for a closed ticket.", ephemeral=True); return
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("This ticket channel is unavailable.", ephemeral=True); return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        data, filename = await create_transcript(interaction.guild, interaction.channel, ticket)
        delivered = await send_ticket_transcript(interaction.guild, settings, interaction.channel, ticket, data, filename)
        settings["tickets"][ticket["id"]] = ticket
        save_ticket_settings(interaction.guild.id, settings)
        if delivered:
            await interaction.followup.send("✅ Transcript generated and sent to the configured transcript channel.", ephemeral=True)
            delay = int(settings.get("auto_delete_minutes", 0))
            if delay > 0:
                schedule_ticket_auto_delete(interaction.guild.id, ticket["id"], delay * 60)
        else:
            await interaction.followup.send(f"⚠️ Transcript saved locally but not delivered. {ticket.get('transcript_delivery_error', 'Check settings and permissions.')}", ephemeral=True)
    except Exception as error:
        logger.exception("Transcript retry failed for %s: %s", ticket["id"], error)
        await interaction.followup.send("⚠️ Transcript generation failed. Check the bot console for details.", ephemeral=True)
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
