import logging
import re

import discord
from discord import app_commands
from discord.ext import commands, tasks

import embeds
import templating
from scheduling import now_in, clock_parts
from storage import Store

log = logging.getLogger(__name__)

FORMAT_LIMIT = 3000

DEFAULT_OPEN_FORMAT = (
    "**we're open**\n"
    "\n"
    "orders are open until {close}. come on in!\n"
    "{date}"
)

DEFAULT_CLOSED_FORMAT = (
    "**we're closed**\n"
    "\n"
    "we open again at {open}. see you then.\n"
    "{date}"
)

DEFAULT_HIATUS_FORMAT = (
    "**on hiatus**\n"
    "\n"
    "the shop is taking a short break, back soon. thank you for your patience.\n"
    "{date}"
)

STATE_LABELS = {"open": "open", "closed": "closed", "hiatus": "on hiatus"}
FORMAT_KEYS = {"open": "open_format", "closed": "closed_format", "hiatus": "hiatus_format"}

FIELDS = ("open", "close", "date", "time", "server")

ALIASES = {
    "open": "open", "opens": "open", "open time": "open", "opening": "open",
    "close": "close", "closes": "close", "close time": "close", "closing": "close",
    "date": "date", "day": "date",
    "time": "time", "now": "time",
    "server": "server", "shop": "server", "name": "server",
}

IMAGE_URL = re.compile(
    r"(?<![(\[<])\bhttps?://[^\s<>()\[\]]+?"
    r"\.(?:png|jpe?g|gif|webp|avif)"
    r"(?:\?[^\s<>()\[\]]*)?",
    re.IGNORECASE,
)

_store = Store("shopstatus_config.json")
config = _store.load()


def save_config():
    _store.save(config)


def defaults():
    return {
        "channel_id": None,
        "enabled": False,
        "hiatus": False,
        "open_time": "09:00",
        "close_time": "21:00",
        "open_format": DEFAULT_OPEN_FORMAT,
        "closed_format": DEFAULT_CLOSED_FORMAT,
        "hiatus_format": DEFAULT_HIATUS_FORMAT,
        "use_embed": True,
        "last_message_id": None,
        "state": None,
    }


def ensure_config(guild_id):
    key = str(guild_id)
    if key not in config:
        config[key] = defaults()
    settings = config[key]
    for field, value in defaults().items():
        settings.setdefault(field, value)
    return settings


def settings_for(guild_id):
    return config.get(str(guild_id)) or defaults()


def to_minutes(hhmm):
    try:
        hour, minute = str(hhmm).split(":")
        return int(hour) * 60 + int(minute)
    except (ValueError, AttributeError):
        return 0


def is_open_now(settings, guild_id):
    opens = to_minutes(settings["open_time"])
    closes = to_minutes(settings["close_time"])
    if opens == closes:
        return False
    now = now_in(guild_id)
    current = now.hour * 60 + now.minute
    if opens < closes:
        return opens <= current < closes
    return current >= opens or current < closes


def desired_state(settings, guild_id):
    if settings.get("hiatus"):
        return "hiatus"
    return "open" if is_open_now(settings, guild_id) else "closed"


def format_values(guild, settings):
    now = now_in(guild.id)
    return {
        "open": settings["open_time"],
        "close": settings["close_time"],
        "date": now.strftime("%B %d, %Y").lower(),
        "time": now.strftime("%I:%M %p").lstrip("0").lower(),
        "server": guild.name,
    }


def render(template, guild, settings):
    return templating.render(template, format_values(guild, settings), ALIASES, guild)


def split_image(body):
    matches = list(IMAGE_URL.finditer(body))
    if not matches:
        return body, None
    last = matches[-1]
    trimmed = body[: last.start()] + body[last.end():]
    return trimmed.strip(), last.group(0)


def state_embed(guild, settings, state):
    body = render(settings[FORMAT_KEYS[state]], guild, settings)
    body, image_url = split_image(body)
    embed = embeds.build(body[:4096] or None)
    if image_url:
        embed.set_image(url=image_url)
    return embed


async def post_state(guild, settings, state):
    channel_id = settings.get("channel_id")
    channel = guild.get_channel(channel_id) if channel_id else None
    if channel is None:
        return False
    try:
        if settings.get("use_embed", True):
            sent = await channel.send(
                embed=state_embed(guild, settings, state),
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=True, users=True),
            )
        else:
            body = render(settings[FORMAT_KEYS[state]], guild, settings)[:2000]
            if not body:
                return False
            sent = await channel.send(
                content=body,
                allowed_mentions=discord.AllowedMentions.all(),
            )
    except (discord.Forbidden, discord.HTTPException):
        return False
    old_id = settings.get("last_message_id")
    if old_id and old_id != sent.id:
        try:
            await channel.get_partial_message(old_id).delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    settings["last_message_id"] = sent.id
    return True


async def sync_state(guild, force=False):
    settings = config.get(str(guild.id))
    if not settings or not settings.get("enabled"):
        return
    state = desired_state(settings, guild.id)
    if force or state != settings.get("state"):
        if await post_state(guild, settings, state):
            settings["state"] = state
            save_config()


class FormatModal(discord.ui.Modal):
    def __init__(self, panel, kind):
        super().__init__(title=f"{kind} format")
        self.panel = panel
        self.kind = kind
        self.field = discord.ui.TextInput(
            label="text shown when sent",
            style=discord.TextStyle.paragraph,
            default=panel.settings[FORMAT_KEYS[kind]],
            max_length=FORMAT_LIMIT,
            required=True,
        )
        self.add_item(self.field)

    async def on_submit(self, interaction):
        text = self.field.value.strip()
        if not text:
            await interaction.response.send_message(
                embed=embeds.error("the format cannot be empty."), ephemeral=True
            )
            return
        self.panel.settings[FORMAT_KEYS[self.kind]] = text
        save_config()
        await interaction.response.defer()
        await self.panel.refresh()


class TimeModal(discord.ui.Modal):
    def __init__(self, panel, which):
        super().__init__(title="open time" if which == "open_time" else "close time")
        self.panel = panel
        self.which = which
        self.field = discord.ui.TextInput(
            label="time, like 9am, 9:30pm or 21:00",
            default=panel.settings[which],
            max_length=10,
            required=True,
        )
        self.add_item(self.field)

    async def on_submit(self, interaction):
        parts = clock_parts(self.field.value)
        if parts is None:
            await interaction.response.send_message(
                embed=embeds.error("i couldn't read that time. try `9am`, `9:30pm` or `21:00`."),
                ephemeral=True,
            )
            return
        self.panel.settings[self.which] = f"{parts[0]:02d}:{parts[1]:02d}"
        save_config()
        await interaction.response.defer()
        await self.panel.refresh()
        await sync_state(interaction.guild)


class SetupView(discord.ui.View):
    def __init__(self, ctx, settings):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.settings = settings
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            embed=embeds.error("this panel isn't yours."), ephemeral=True
        )
        return False

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    def status_embed(self):
        settings = self.settings
        guild = self.ctx.guild
        channel = guild.get_channel(settings.get("channel_id")) if settings.get("channel_id") else None
        power = "on" if settings.get("enabled") else "off"

        if not settings.get("enabled"):
            now_label = "off"
        else:
            now_label = STATE_LABELS.get(desired_state(settings, guild.id), "closed")

        preview_state = desired_state(settings, guild.id)
        preview = render(settings[FORMAT_KEYS[preview_state]], guild, settings)

        lines = [
            "**shop status**",
            "",
            f"**power** : {power}",
            f"**now** : {now_label}",
            f"**channel** : {channel.mention if channel else 'not set'}",
            f"**opens** : {settings['open_time']}    **closes** : {settings['close_time']}",
            f"**output** : {'embed' if settings.get('use_embed', True) else 'raw text'}",
            "",
            f"**preview ({STATE_LABELS[preview_state]})**",
            preview,
        ]
        return embeds.build("\n".join(lines)[:4096])

    async def refresh(self, interaction=None):
        embed = self.status_embed()
        if interaction is not None and not interaction.response.is_done():
            await interaction.response.edit_message(embed=embed, view=self)
        elif self.message is not None:
            try:
                await self.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        placeholder="channel to post the status in",
        row=0,
    )
    async def pick_channel(self, interaction, select):
        self.settings["channel_id"] = select.values[0].id
        save_config()
        await self.refresh(interaction)

    @discord.ui.button(label="open format", style=discord.ButtonStyle.secondary, row=1)
    async def open_format(self, interaction, button):
        await interaction.response.send_modal(FormatModal(self, "open"))

    @discord.ui.button(label="closed format", style=discord.ButtonStyle.secondary, row=1)
    async def closed_format(self, interaction, button):
        await interaction.response.send_modal(FormatModal(self, "closed"))

    @discord.ui.button(label="hiatus format", style=discord.ButtonStyle.secondary, row=1)
    async def hiatus_format(self, interaction, button):
        await interaction.response.send_modal(FormatModal(self, "hiatus"))

    @discord.ui.button(label="embed / text", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_output(self, interaction, button):
        self.settings["use_embed"] = not self.settings.get("use_embed", True)
        save_config()
        await self.refresh(interaction)

    @discord.ui.button(label="open time", style=discord.ButtonStyle.secondary, row=2)
    async def open_time(self, interaction, button):
        await interaction.response.send_modal(TimeModal(self, "open_time"))

    @discord.ui.button(label="close time", style=discord.ButtonStyle.secondary, row=2)
    async def close_time(self, interaction, button):
        await interaction.response.send_modal(TimeModal(self, "close_time"))

    @discord.ui.button(label="turn on/off", style=discord.ButtonStyle.secondary, row=3)
    async def toggle_power(self, interaction, button):
        self.settings["enabled"] = not self.settings.get("enabled", False)
        save_config()
        await self.refresh(interaction)
        await sync_state(interaction.guild)

    @discord.ui.button(label="hiatus", style=discord.ButtonStyle.secondary, row=3)
    async def toggle_hiatus(self, interaction, button):
        self.settings["hiatus"] = not self.settings.get("hiatus", False)
        save_config()
        await self.refresh(interaction)
        await sync_state(interaction.guild)

    @discord.ui.button(label="post now", style=discord.ButtonStyle.secondary, row=3)
    async def post_now(self, interaction, button):
        if not self.settings.get("enabled"):
            await interaction.response.send_message(
                embed=embeds.error("turn the shop status on first."), ephemeral=True
            )
            return
        await interaction.response.defer()
        await sync_state(interaction.guild, force=True)
        await self.refresh()


class ShopStatus(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.ticker.start()

    async def cog_unload(self):
        self.ticker.cancel()

    @tasks.loop(seconds=60)
    async def ticker(self):
        for key in list(config.keys()):
            settings = config.get(key)
            if not settings or not settings.get("enabled"):
                continue
            guild = self.bot.get_guild(int(key))
            if guild is None:
                continue
            try:
                await sync_state(guild)
            except Exception:
                log.exception("shopstatus tick failed for %s", key)

    @ticker.before_loop
    async def before_ticker(self):
        await self.bot.wait_until_ready()

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.NoPrivateMessage):
            await embeds.send(ctx, embeds.error("this command only works in a server."))
            return
        if isinstance(error, commands.MissingPermissions):
            await embeds.send(
                ctx,
                embeds.error("you need manage messages permission for that.", title="Not allowed"),
            )
            return
        log.exception("Unhandled error in %s", ctx.command, exc_info=error)
        await embeds.send(ctx, embeds.error("something broke on my end. it has been logged."))

    @commands.hybrid_command(
        name="shopstatus",
        aliases=["shop"],
        description="Set up automatic open, closed and hiatus posts.",
    )
    @app_commands.default_permissions(manage_messages=True)
    @commands.has_permissions(manage_messages=True)
    @commands.guild_only()
    async def shopstatus(self, ctx):
        settings = ensure_config(ctx.guild.id)
        save_config()
        view = SetupView(ctx, settings)
        view.message = await ctx.send(
            embed=view.status_embed(),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot):
    await bot.add_cog(ShopStatus(bot))