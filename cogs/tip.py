import datetime
import logging
import time
import uuid

import discord
from discord import app_commands
from discord.ext import commands

import attach
import embeds
from prefixes import display_prefix
import emojiutils
import templating
from scheduling import tz_for
from storage import Store

log = logging.getLogger(__name__)

_config_store = Store("tip_config.json")
_tip_store = Store("tips.json", default=list)

config = _config_store.load()
tips = _tip_store.load()

TEMPLATE_LIMIT = 3800
AMOUNT_LIMIT = 300
COOLDOWN_SECONDS = 30

PAD = "ㅤ"

DEFAULT_TEMPLATE = (
    f":03dc_cake:{PAD}a sweet little tip for {{user}}\n"
    f"{PAD}\n"
    f":shortcake1:{PAD}{{amount}}\n"
    f"{PAD}\n"
    f":strawberri:{PAD}thank you for keeping the treats coming!\n"
    f":IceCreamSundae:{PAD}tipped by: {{tipper}}\n"
    f":dndexl:{PAD}{{date}}"
)

FIELDS = ("user", "amount", "tipper", "date", "time", "when", "count")

ALIASES = {
    "user": "user", "recipient": "user", "worker": "user", "artist": "user",
    "staff": "user", "tipped": "user", "for": "user", "them": "user",
    "amount": "amount", "tip": "amount", "value": "amount", "gratuity": "amount",
    "how much": "amount", "sum": "amount",
    "tipper": "tipper", "tipped by": "tipper", "author": "tipper",
    "customer": "tipper", "buyer": "tipper", "from": "tipper", "by": "tipper",
    "date": "date", "day": "date",
    "time": "time",
    "when": "when", "posted": "when", "tipped at": "when",
    "count": "count", "number": "count", "tip number": "count", "times": "count",
}

SAMPLE = {
    "user": "@barista",
    "amount": "$5.00",
    "tipper": "@customer",
    "date": "august 30, 2026",
    "time": "10:22 pm",
    "when": "just now",
    "count": "7",
}


def save_config():
    _config_store.save(config)


def save_tips():
    _tip_store.save(tips)


def get_config(guild_id):
    return config.get(str(guild_id))


def defaults():
    return {
        "channel_id": None,
        "template": DEFAULT_TEMPLATE,
        "ping": False,
        "staff_only": False,
        "allow_self": False,
        "raw": False,
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
    return get_config(guild_id) or defaults()


def guild_tips(guild_id):
    return [t for t in tips if t["guild_id"] == guild_id]


def count_for(guild_id, user_id):
    return sum(
        1 for t in tips
        if t["guild_id"] == guild_id and t["user_id"] == user_id
    )


def stamp_values(guild, record):
    stamp = int(record.get("created_at") or 0)
    if not stamp:
        return {"date": "", "time": "", "when": ""}

    moment = datetime.datetime.fromtimestamp(stamp, tz_for(guild.id))
    return {
        "date": moment.strftime("%B %d, %Y").lower(),
        "time": moment.strftime("%I:%M %p").lstrip("0").lower(),
        "when": f"<t:{stamp}:R>",
    }


def tip_values(guild, record):
    values = {
        "user": f"<@{record['user_id']}>",
        "amount": record["amount"],
        "tipper": f"<@{record['tipper_id']}>",
        "count": str(record.get("count", 1)),
    }
    values.update(stamp_values(guild, record))
    return values


def render(template, values, guild):
    return templating.render(template, values, ALIASES, guild)


def tip_embed(guild, settings, record):
    body = render(settings["template"], tip_values(guild, record), guild)
    return embeds.build(body[:4096])


def tip_text(guild, settings, record):
    body = render(settings["template"], tip_values(guild, record), guild)
    return body[:2000]


class TemplateModal(discord.ui.Modal, title="Tip Format"):
    def __init__(self, builder):
        super().__init__()
        self.builder = builder
        self.f_template = discord.ui.TextInput(
            label="Format",
            default=builder.settings["template"][:4000],
            style=discord.TextStyle.paragraph,
            max_length=4000,
            required=True,
        )
        self.add_item(self.f_template)

    async def on_submit(self, interaction):
        text = self.f_template.value.strip()

        if not text:
            await interaction.response.send_message(
                embed=embeds.error("the format cannot be empty."), ephemeral=True
            )
            return

        if len(text) > TEMPLATE_LIMIT:
            await interaction.response.send_message(
                embed=embeds.error(
                    f"keep the format under {TEMPLATE_LIMIT} characters."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        self.builder.settings["template"] = text
        save_config()
        await self.builder.refresh()

        notes = []
        missed = templating.unknown(text, ALIASES)
        if missed:
            listed = ", ".join(f"`{{{u}}}`" for u in missed)
            notes.append(
                f"{listed} is not a field i know, so it will print as "
                "written. the fields are "
                + ", ".join(f"`{{{f}}}`" for f in FIELDS)
                + "."
            )

        dead = emojiutils.unresolved_names(text, interaction.guild)
        if dead:
            listed = ", ".join(f"`:{d}:`" for d in dead)
            notes.append(
                f"{listed} does not match an emoji in this server, so it "
                "will print as text. type a backslash before the emoji and "
                "paste what discord gives you instead."
            )

        if notes:
            await interaction.followup.send(
                embed=embeds.error(
                    "saved. two things to check:\n\n" + "\n\n".join(notes)
                    if len(notes) > 1
                    else "saved, but " + notes[0],
                    title="Check the format",
                ),
                ephemeral=True,
            )


class ChannelView(discord.ui.View):
    def __init__(self, builder):
        super().__init__(timeout=300)
        self.builder = builder

    async def interaction_check(self, interaction):
        return interaction.user.id == self.builder.ctx.author.id

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        placeholder="Where every tip lands",
        row=0,
    )
    async def pick(self, interaction, select):
        await interaction.response.defer()
        self.builder.settings["channel_id"] = select.values[0].id
        save_config()
        await self.builder.refresh()


class SetupView(discord.ui.View):
    def __init__(self, ctx, settings):
        super().__init__(timeout=900)
        self.ctx = ctx
        self.settings = settings
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                embed=embeds.error("this deck isn't yours.", title="Not yours"),
                ephemeral=True,
            )
            return False
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                embed=embeds.error(
                    "you need manage server permission.", title="Not allowed"
                ),
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    def status_embed(self):
        guild = self.ctx.guild
        settings = self.settings
        channel = guild.get_channel(settings.get("channel_id"))

        lines = [
            f"**Drops in** - {channel.mention if channel else 'not set'}",
            f"**Style** - {'raw text' if settings.get('raw') else 'embed'}",
            f"**Who can tip** - "
            f"{'staff only' if settings['staff_only'] else 'anyone'}",
            f"**Self tipping** - "
            f"{'allowed' if settings['allow_self'] else 'blocked'}",
            f"**Pings the person** - {'yes' if settings['ping'] else 'no'}",
            "",
            f"**Tips recorded** - {len(guild_tips(guild.id))}",
        ]

        embed = embeds.build("\n".join(lines), title="Tip setup")
        embed.add_field(
            name="Preview",
            value=render(settings["template"], SAMPLE, guild)[:1024],
            inline=False,
        )
        embed.set_footer(text="channel · format · who can tip — editable below")
        return embed

    async def refresh(self):
        if self.message is None:
            return
        try:
            await self.message.edit(embed=self.status_embed(), view=self)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="channel", style=discord.ButtonStyle.secondary, row=0)
    async def channel(self, interaction, button):
        await interaction.response.send_message(
            embed=embeds.notice("pick where every tip should land."),
            view=ChannelView(self),
            ephemeral=True,
        )

    @discord.ui.button(label="format", style=discord.ButtonStyle.secondary, row=0)
    async def format_button(self, interaction, button):
        await interaction.response.send_modal(TemplateModal(self))

    @discord.ui.button(label="fields", style=discord.ButtonStyle.secondary, row=0)
    async def fields(self, interaction, button):
        await interaction.response.send_message(
            embed=embeds.notice(
                "drop any of these into the format and the bot fills them "
                "in:\n\n"
                + "\n".join(f"`{{{f}}}`" for f in FIELDS)
                + "\n\ntype `:name:` for a server emoji and it gets resolved "
                "when the tip goes out. an attached image is added under "
                "the text automatically.",
                title="Format fields",
            ),
            ephemeral=True,
        )

    @discord.ui.button(label="who can tip", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_staff(self, interaction, button):
        await interaction.response.defer()
        self.settings["staff_only"] = not self.settings["staff_only"]
        save_config()
        await self.refresh()

    @discord.ui.button(label="self tipping", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_self(self, interaction, button):
        await interaction.response.defer()
        self.settings["allow_self"] = not self.settings["allow_self"]
        save_config()
        await self.refresh()

    @discord.ui.button(label="toggle ping", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_ping(self, interaction, button):
        await interaction.response.defer()
        self.settings["ping"] = not self.settings["ping"]
        save_config()
        await self.refresh()

    @discord.ui.button(label="output style", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_raw(self, interaction, button):
        await interaction.response.defer()
        self.settings["raw"] = not self.settings.get("raw", False)
        save_config()
        await self.refresh()

    @discord.ui.button(label="reset format", style=discord.ButtonStyle.danger, row=2)
    async def reset_format(self, interaction, button):
        await interaction.response.defer()
        self.settings["template"] = DEFAULT_TEMPLATE
        save_config()
        await self.refresh()


class Tip(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.NoPrivateMessage):
            await embeds.send(
                ctx, embeds.error("this command only works in a server.")
            )
            return
        if isinstance(error, commands.MissingPermissions):
            await embeds.send(
                ctx,
                embeds.error(
                    "you need manage server permission for that.",
                    title="Not allowed",
                ),
            )
            return
        if isinstance(error, commands.CommandOnCooldown):
            await embeds.send(
                ctx,
                embeds.error(
                    f"try again in {error.retry_after:.0f}s.", title="Slow down"
                ),
            )
            return

        log.exception("Unhandled error in %s", ctx.command, exc_info=error)
        await embeds.send(
            ctx, embeds.error("something broke on my end. it has been logged.")
        )

    @commands.hybrid_group(
        name="tip",
        invoke_without_command=True,
        fallback="add",
        description="Tip someone for their work.",
    )
    @app_commands.describe(
        user="Who you are tipping",
        amount="How much you are tipping",
        image="An optional screenshot or photo",
    )
    @commands.guild_only()
    @commands.cooldown(1, COOLDOWN_SECONDS, commands.BucketType.user)
    async def tip(
        self,
        ctx,
        user: discord.Member,
        image: discord.Attachment = None,
        *,
        amount: str,
    ):
        await ctx.defer(ephemeral=True)

        settings = settings_for(ctx.guild.id)

        channel = ctx.guild.get_channel(settings.get("channel_id"))
        if channel is None:
            await embeds.send(
                ctx,
                embeds.error(
                    "no tip channel set yet. someone with manage server "
                    f"needs to run `{display_prefix(ctx)}tip setup`.",
                    title="Not set up",
                ),
            )
            return

        if settings["staff_only"] and not ctx.author.guild_permissions.manage_messages:
            await embeds.send(
                ctx,
                embeds.error(
                    "only staff can post tips here.", title="Not allowed"
                ),
            )
            return

        if user.id == ctx.author.id and not settings["allow_self"]:
            await embeds.send(
                ctx, embeds.error("you cannot tip yourself.")
            )
            return

        if user.bot:
            await embeds.send(ctx, embeds.error("you cannot tip a bot."))
            return

        text = amount.strip()
        if not text:
            await embeds.send(ctx, embeds.error("say how much you are tipping."))
            return
        if len(text) > AMOUNT_LIMIT:
            await embeds.send(
                ctx,
                embeds.error(
                    f"keep the amount under {AMOUNT_LIMIT} characters."
                ),
            )
            return

        if not channel.permissions_for(ctx.guild.me).send_messages:
            await embeds.send(
                ctx, embeds.error("i cannot post in the tip channel.")
            )
            return

        record = {
            "id": uuid.uuid4().hex[:8],
            "guild_id": ctx.guild.id,
            "user_id": user.id,
            "tipper_id": ctx.author.id,
            "amount": text,
            "created_at": int(time.time()),
            "count": count_for(ctx.guild.id, user.id) + 1,
            "message_id": None,
        }

        picture, problem = await attach.read_image(image)
        if problem:
            await embeds.send(ctx, embeds.error(problem))
            return

        file = picture.file() if picture is not None else None
        ping = settings.get("ping", False)

        if settings.get("raw", False):
            kwargs = {
                "content": tip_text(ctx.guild, settings, record) or None,
                "file": file,
                "allowed_mentions": discord.AllowedMentions(
                    everyone=False, roles=False, users=[user] if ping else False
                ),
            }
        else:
            embed = tip_embed(ctx.guild, settings, record)
            if picture is not None:
                embed.set_image(url=picture.reference)
            kwargs = {
                "content": user.mention if ping else None,
                "embed": embed,
                "file": file,
                "allowed_mentions": discord.AllowedMentions(
                    everyone=False, roles=False, users=ping
                ),
            }

        try:
            sent = await channel.send(**kwargs)
        except discord.Forbidden:
            await embeds.send(
                ctx, embeds.error("i cannot post in the tip channel.")
            )
            return
        except discord.HTTPException:
            log.exception("tip post rejected in %s", channel.id)
            await embeds.send(
                ctx,
                embeds.error("discord turned that tip down. check the log."),
            )
            return

        record["message_id"] = sent.id
        tips.append(record)
        save_tips()

        await embeds.send(
            ctx,
            embeds.notice(
                f"posted in {channel.mention}. that is tip "
                f"#{record['count']} for {user.display_name}. {sent.jump_url}",
                title="Tip posted",
            ),
            ephemeral=True,
        )

    @tip.command(
        name="setup",
        description="Customise where tips drop and how they look.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    async def tip_setup(self, ctx):
        settings = ensure_config(ctx.guild.id)
        save_config()

        view = SetupView(ctx, settings)
        view.message = await ctx.send(
            embed=view.status_embed(),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @tip.command(
        name="count",
        description="How many tips someone has.",
    )
    @app_commands.describe(user="Who to check. Defaults to you.")
    async def tip_count(self, ctx, user: discord.Member = None):
        target = user or ctx.author
        total = count_for(ctx.guild.id, target.id)

        if not total:
            await embeds.send(
                ctx,
                embeds.notice(f"{target.display_name} has no tips yet."),
            )
            return

        await embeds.send(
            ctx,
            embeds.notice(
                f"{target.display_name} has {total} "
                f"tip{'' if total == 1 else 's'}.",
                title="Tips",
            ),
        )

    @tip.command(
        name="top",
        description="Who has the most tips.",
    )
    async def tip_top(self, ctx):
        tally = {}
        for record in guild_tips(ctx.guild.id):
            tally[record["user_id"]] = tally.get(record["user_id"], 0) + 1

        if not tally:
            await embeds.send(
                ctx, embeds.notice("no tips here yet.", title="Tips")
            )
            return

        ranked = sorted(tally.items(), key=lambda kv: -kv[1])[:10]
        lines = []
        for place, (user_id, count) in enumerate(ranked, 1):
            member = ctx.guild.get_member(user_id)
            name = member.display_name if member else f"<@{user_id}>"
            lines.append(f"`{place}.` {name} — {count}")

        await embeds.send(
            ctx, embeds.build("\n".join(lines), title="Most tipped")
        )


async def setup(bot):
    await bot.add_cog(Tip(bot))