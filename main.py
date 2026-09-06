import discord
from discord import app_commands
from discord.ext import commands
import logging
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
from pathlib import Path
from prefixes import resolve_prefix
import embeds
import os
import asyncio

BOT_NAME = "miyu"

ROOT = Path(__file__).resolve().parent
COGS_DIR = ROOT / "cogs"

load_dotenv(ROOT / ".env")
token = os.getenv("DISCORD_TOKEN")

log = logging.getLogger(BOT_NAME)

handler = RotatingFileHandler(
    filename=ROOT / "discord.log",
    encoding="utf-8",
    maxBytes=2 * 1024 * 1024,
    backupCount=2,
)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True

bot = commands.Bot(
    command_prefix=resolve_prefix,
    intents=intents,
    allowed_mentions=discord.AllowedMentions(
        everyone=False, roles=False, users=True
    ),
)


def unwrap(error):
    while True:
        if isinstance(error, commands.HybridCommandError):
            error = error.original
        elif isinstance(error, app_commands.CommandInvokeError):
            error = error.original
        else:
            return error


@bot.event
async def on_ready():
    tree_size = len(bot.tree.get_commands())
    print(f"{BOT_NAME} online as {bot.user} in {len(bot.guilds)} guild(s)")
    print(f"Slash tree holds {tree_size} top level command(s), not yet synced")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return

    if ctx.command is not None and ctx.command.has_error_handler():
        return
    if ctx.cog is not None and ctx.cog.has_error_handler():
        return

    error = unwrap(error)

    if isinstance(error, commands.MissingRequiredArgument):
        await embeds.send(
            ctx, embeds.error(f"Missing argument: **{error.param.name}**.")
        )
        return

    if isinstance(error, commands.NoPrivateMessage):
        await embeds.send(
            ctx, embeds.error("This command only works in a **server**.")
        )
        return

    if isinstance(error, commands.CommandOnCooldown):
        await embeds.send(
            ctx,
            embeds.error(f"Slow down - try again in **{error.retry_after:.0f}s**."),
        )
        return

    if isinstance(error, (commands.MissingPermissions, commands.CheckFailure)):
        await embeds.send(
            ctx, embeds.error("You don't have **permission** to use that.")
        )
        return

    log.exception("Unhandled error in %s", ctx.command, exc_info=error)
    await embeds.send(
        ctx, embeds.error("Something broke on my end. It's been **logged**.")
    )


async def load_cogs():
    loaded, skipped, failed = 0, 0, 0

    for path in sorted(COGS_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            await bot.load_extension(f"cogs.{path.stem}")
            print(f"  loaded   {path.name}")
            loaded += 1
        except commands.NoEntryPointError:
            print(f"  skipped  {path.name}: no setup function")
            skipped += 1
        except Exception as exc:
            print(f"  FAILED   {path.name}: {type(exc).__name__}: {exc}")
            failed += 1

    print(f"Cogs: {loaded} loaded, {skipped} skipped, {failed} failed")


async def main():
    if not token:
        print(f"DISCORD_TOKEN is missing. Check {ROOT / '.env'}")
        return

    async with bot:
        discord.utils.setup_logging(handler=handler, level=logging.INFO)
        embeds.install()
        await load_cogs()
        await bot.start(token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"{BOT_NAME} shutting down.")