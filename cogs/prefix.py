import logging

import discord
from discord import app_commands
from discord.ext import commands

import embeds
from prefixes import (
    DEFAULT_PREFIX,
    MAX_LENGTH,
    clear_prefix,
    is_custom,
    prefix_for,
    set_prefix,
    validate,
)

log = logging.getLogger(__name__)
ACCENT = embeds.ACCENT.value


def can_manage(member: discord.Member) -> bool:
    return (
        member.id == member.guild.owner_id
        or member.guild_permissions.administrator
    )


class PrefixModal(discord.ui.Modal, title="Change prefix"):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.field = discord.ui.TextInput(
            label="New prefix",
            default=prefix_for(panel.guild.id),
            placeholder="! or ? or tef",
            max_length=MAX_LENGTH,
            required=True,
        )
        self.add_item(self.field)

    async def on_submit(self, interaction):
        value, problem = validate(self.field.value)
        if problem:
            await interaction.response.send_message(
                embed=embeds.error(problem), ephemeral=True
            )
            return
        set_prefix(self.panel.guild.id, value)
        await interaction.response.defer()
        await self.panel.refresh()


class PrefixButtons(discord.ui.ActionRow):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel

    @discord.ui.button(label="Change prefix", style=discord.ButtonStyle.secondary)
    async def change(self, interaction, button):
        if not can_manage(interaction.user):
            await interaction.response.send_message(
                embed=embeds.error("only the owner or an admin can change the prefix."),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(PrefixModal(self.panel))

    @discord.ui.button(label="Reset to default", style=discord.ButtonStyle.secondary)
    async def reset(self, interaction, button):
        if not can_manage(interaction.user):
            await interaction.response.send_message(
                embed=embeds.error("only the owner or an admin can change the prefix."),
                ephemeral=True,
            )
            return
        clear_prefix(self.panel.guild.id)
        await self.panel.refresh(interaction)


class PrefixPanel(discord.ui.LayoutView):
    def __init__(self, guild, author_id, message=None):
        super().__init__(timeout=300)
        self.guild = guild
        self.author_id = author_id
        self.message = message
        self.build()

    def build(self, interactive=True):
        self.clear_items()
        current = prefix_for(self.guild.id)
        source = "custom" if is_custom(self.guild.id) else f"default (`{DEFAULT_PREFIX}`)"

        container = discord.ui.Container(accent_colour=ACCENT)
        container.add_item(discord.ui.TextDisplay("## Prefix setup"))
        container.add_item(discord.ui.TextDisplay(
            f"**Prefix** — `{current}`\n**Source** — {source}"
        ))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(
            f"-# mentioning me always works · `{current}help` lists commands · "
            "only the owner or an admin can change it"
        ))
        self.add_item(container)

        if interactive:
            self.add_item(PrefixButtons(self))

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                embed=embeds.error("this panel isn't yours - mention me to get your own."),
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self):
        self.build(interactive=False)
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def refresh(self, interaction=None):
        self.build()
        if interaction is not None and not interaction.response.is_done():
            await interaction.response.edit_message(view=self)
        elif self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class Prefix(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def open(self, sendable, guild, author_id):
        panel = PrefixPanel(guild, author_id)
        panel.message = await sendable.send(view=panel)
        return panel

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None or self.bot.user is None:
            return
        mentions = {f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>"}
        if message.content.strip() in mentions:
            await self.open(message.channel, message.guild, message.author.id)

    @commands.hybrid_command(
        name="prefix",
        description="Show or change the command prefix.",
    )
    @app_commands.describe(new="Optional - set a new prefix right away (admins only).")
    @commands.guild_only()
    async def prefix(self, ctx, *, new: str = None):
        if new is None:
            await self.open(ctx, ctx.guild, ctx.author.id)
            return

        if not can_manage(ctx.author):
            await embeds.send(
                ctx, embeds.error("only the owner or an admin can change the prefix.")
            )
            return

        value, problem = validate(new)
        if problem:
            await embeds.send(ctx, embeds.error(problem))
            return

        set_prefix(ctx.guild.id, value)
        await embeds.send(ctx, embeds.ok(f"prefix set to **{value}** — try `{value}help`."))


async def setup(bot):
    await bot.add_cog(Prefix(bot))