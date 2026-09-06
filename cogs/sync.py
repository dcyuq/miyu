import logging

import discord
from discord.ext import commands

import embeds
from prefixes import display_prefix

log = logging.getLogger(__name__)

DESCRIPTION_LIMIT = 4000


def is_moderator(member):
    if not isinstance(member, discord.Member):
        return False
    if member.id == member.guild.owner_id:
        return True
    perms = member.guild_permissions
    return perms.administrator or perms.manage_guild


class Sync(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_check(self, ctx):
        if await self.bot.is_owner(ctx.author):
            return True
        if is_moderator(ctx.author):
            return True
        raise commands.CheckFailure()

    async def cog_command_error(self, ctx, error):
        if isinstance(error, (commands.NotOwner, commands.CheckFailure)):
            return

        log.exception("Sync failed in %s", ctx.command, exc_info=error)
        await embeds.send(
            ctx,
            embeds.error(
                "the sync did not go through. check the log for details.",
                title="Sync failed",
            ),
        )

    @commands.group(name="sync", invoke_without_command=True, hidden=True)
    async def sync(self, ctx):
        async with ctx.typing():
            synced = await self.bot.tree.sync()

        await embeds.send(
            ctx,
            embeds.notice(
                f"synced {len(synced)} command(s) globally. global commands "
                "can take up to an hour to appear everywhere. Use "
                f"`{display_prefix(ctx)}sync here` for an instant test in this server.",
                title="Synced",
            ),
        )

    @sync.command(name="here", aliases=["guild"])
    @commands.guild_only()
    async def sync_here(self, ctx):
        self.bot.tree.copy_global_to(guild=ctx.guild)
        async with ctx.typing():
            synced = await self.bot.tree.sync(guild=ctx.guild)

        await embeds.send(
            ctx,
            embeds.notice(
                f"synced {len(synced)} command(s) to this server.",
                title="Synced",
            ),
        )

    @sync.command(name="clear")
    async def sync_clear(self, ctx):
        self.bot.tree.clear_commands(guild=None)
        async with ctx.typing():
            await self.bot.tree.sync()

        await embeds.send(
            ctx,
            embeds.notice(
                "cleared every global command. restart the bot and run "
                f"`{display_prefix(ctx)}sync` to put them back.",
                title="Cleared",
            ),
        )

    @sync.command(name="clearhere")
    @commands.guild_only()
    async def sync_clear_here(self, ctx):
        self.bot.tree.clear_commands(guild=ctx.guild)
        async with ctx.typing():
            await self.bot.tree.sync(guild=ctx.guild)

        await embeds.send(
            ctx,
            embeds.notice(
                "cleared the copies registered to this server. global "
                "commands are untouched.",
                title="Cleared",
            ),
        )

    @sync.command(name="list")
    async def sync_list(self, ctx):
        names = sorted(
            command.qualified_name for command in self.bot.tree.walk_commands()
        )

        if not names:
            await embeds.send(
                ctx, embeds.notice("nothing in the tree.", title="Slash tree")
            )
            return

        listed = ", ".join(f"`/{name}`" for name in names)

        await embeds.send(
            ctx,
            embeds.notice(
                listed[:DESCRIPTION_LIMIT],
                title=f"Slash tree ({len(names)})",
            ),
        )


async def setup(bot):
    await bot.add_cog(Sync(bot))