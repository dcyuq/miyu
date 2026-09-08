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

_config_store = Store("proof_config.json")
_proof_store = Store("proofs.json", default=list)

config = _config_store.load()
proofs = _proof_store.load()

TEMPLATE_LIMIT = 3800
COUNTER_LIMIT = 1000
ITEM_LIMIT = 200
FEEDBACK_LIMIT = 1000
COOLDOWN_SECONDS = 30

DEFAULT_TEMPLATE = (
    "**new proof from {user}**\n"
    "\n"
    "**item** · {item}\n"
    "**feedback** · {feedback}\n"
    "\n"
    "proof #{count} · {date}"
)

DEFAULT_COUNTER = (
    "**{total}** proofs posted here\n"
    "last updated {when}"
)

FIELDS = ("user", "item", "feedback", "date", "time", "when", "count", "total")

COUNTER_FIELDS = ("total", "date", "time", "when")

ALIASES = {
    "user": "user", "poster": "user", "buyer": "user", "customer": "user",
    "from": "user", "by": "user", "them": "user",
    "item": "item", "product": "item", "order": "item", "bought": "item",
    "purchase": "item",
    "feedback": "feedback", "review": "feedback", "comment": "feedback",
    "message": "feedback", "note": "feedback", "words": "feedback",
    "date": "date", "day": "date",
    "time": "time",
    "when": "when", "posted": "when",
    "count": "count", "number": "count", "proof number": "count",
    "total": "total", "proofs": "total", "all": "total",
}

COUNTER_ALIASES = {
    "total": "total", "count": "total", "proofs": "total", "number": "total",
    "date": "date", "day": "date",
    "time": "time",
    "when": "when", "updated": "when", "last updated": "when",
}

SAMPLE = {
    "user": "@customer",
    "item": "weekly diamond pass",
    "feedback": "super fast and legit, thank you!",
    "date": "september 07, 2026",
    "time": "6:50 pm",
    "when": "just now",
    "count": "3",
    "total": "42",
}

COUNTER_SAMPLE = {
    "total": "42",
    "date": "september 07, 2026",
    "time": "6:50 pm",
    "when": "just now",
}


def save_config():
    _config_store.save(config)


def save_proofs():
    _proof_store.save(proofs)


def get_config(guild_id):
    return config.get(str(guild_id))


def defaults():
    return {
        "channel_id": None,
        "template": DEFAULT_TEMPLATE,
        "counter": DEFAULT_COUNTER,
        "ping": True,
        "require_image": True,
        "staff_only": False,
        "sticky_id": None,
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


def guild_proofs(guild_id):
    return [p for p in proofs if p["guild_id"] == guild_id]


def total_for(guild_id):
    return len(guild_proofs(guild_id))


def count_for(guild_id, user_id):
    return sum(
        1 for p in proofs
        if p["guild_id"] == guild_id and p["user_id"] == user_id
    )


def stamp_values(guild, stamp):
    stamp = int(stamp or 0)
    if not stamp:
        return {"date": "", "time": "", "when": ""}

    moment = datetime.datetime.fromtimestamp(stamp, tz_for(guild.id))
    return {
        "date": moment.strftime("%B %d, %Y").lower(),
        "time": moment.strftime("%I:%M %p").lstrip("0").lower(),
        "when": f"<t:{stamp}:R>",
    }


def proof_values(guild, record):
    values = {
        "user": f"<@{record['user_id']}>",
        "item": record["item"],
        "feedback": record["feedback"],
        "count": str(record.get("count", 1)),
        "total": str(total_for(guild.id)),
    }
    values.update(stamp_values(guild, record.get("created_at")))
    return values


def proof_text(guild, settings, record):
    body = templating.render(
        settings["template"], proof_values(guild, record), ALIASES, guild
    )
    return body[:2000]


def counter_text(guild, settings):
    values = {"total": str(total_for(guild.id))}
    values.update(stamp_values(guild, int(time.time())))
    body = templating.render(
        settings.get("counter", DEFAULT_COUNTER), values, COUNTER_ALIASES, guild
    )
    return body[:2000]


class ProofPost(discord.ui.LayoutView):
    def __init__(self, text, media):
        super().__init__(timeout=None)
        self.add_item(discord.ui.TextDisplay(text or "\u200b"))
        frame = attach.frame(media)
        if frame is not None:
            self.add_item(frame)


class StickyNote(discord.ui.LayoutView):
    def __init__(self, text):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(discord.ui.TextDisplay(text or "\u200b"))
        )


async def post_proof(channel, guild, settings, record, media, author):
    text = proof_text(guild, settings, record)
    ping = settings.get("ping", True)
    mentions = discord.AllowedMentions(
        everyone=False, roles=False, users=[author] if ping else False
    )

    return await channel.send(
        view=ProofPost(text, media),
        files=[media.file()] if media is not None else [],
        allowed_mentions=mentions,
    )


async def refresh_sticky(guild, settings, channel):
    if channel is None:
        return
    if not channel.permissions_for(guild.me).send_messages:
        return

    old_id = settings.get("sticky_id")
    if old_id:
        try:
            old = await channel.fetch_message(old_id)
            await old.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    try:
        sent = await channel.send(
            view=StickyNote(counter_text(guild, settings)),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        log.exception("sticky post failed in %s", channel.id)
        return

    settings["sticky_id"] = sent.id
    save_config()


class ProofModal(discord.ui.Modal, title="Edit proof"):
    def __init__(self, preview):
        super().__init__()
        self.preview = preview
        self.f_item = discord.ui.TextInput(
            label="Item",
            default=preview.record["item"][:ITEM_LIMIT],
            max_length=ITEM_LIMIT,
            required=True,
        )
        self.f_feedback = discord.ui.TextInput(
            label="Feedback",
            default=preview.record["feedback"][:FEEDBACK_LIMIT],
            style=discord.TextStyle.paragraph,
            max_length=FEEDBACK_LIMIT,
            required=True,
        )
        self.add_item(self.f_item)
        self.add_item(self.f_feedback)

    async def on_submit(self, interaction):
        item = self.f_item.value.strip()
        feedback = self.f_feedback.value.strip()

        if not item:
            await interaction.response.send_message(
                embed=embeds.error("the **item** cannot be empty."),
                ephemeral=True,
            )
            return
        if not feedback:
            await interaction.response.send_message(
                embed=embeds.error("the **feedback** cannot be empty."),
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        self.preview.record["item"] = item
        self.preview.record["feedback"] = feedback
        await self.preview.refresh()


class ConfirmRow(discord.ui.ActionRow):
    def __init__(self, preview):
        super().__init__()
        self.preview = preview

    @discord.ui.button(label="confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction, button):
        await self.preview.confirm(interaction)

    @discord.ui.button(label="redo", style=discord.ButtonStyle.secondary)
    async def redo(self, interaction, button):
        await interaction.response.send_modal(ProofModal(self.preview))


class PreviewView(discord.ui.LayoutView):
    def __init__(self, ctx, settings, record, media):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.settings = settings
        self.record = record
        self.media = media
        self.message = None
        self.done = False
        self.build()

    def proof_body(self):
        return proof_text(self.ctx.guild, self.settings, self.record) or "\u200b"

    def files(self):
        return [self.media.file()] if self.media is not None else []

    def build(self):
        self.clear_items()
        self.add_item(discord.ui.TextDisplay(self.proof_body()))
        frame = attach.frame(self.media)
        if frame is not None:
            self.add_item(frame)
        self.add_item(discord.ui.Separator(visible=True))
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    "**Does this look good?**\n"
                    "press **confirm** to post, or **redo** to re-enter."
                ),
                ConfirmRow(self),
            )
        )

    async def interaction_check(self, interaction):
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                embed=embeds.error("this proof isn't yours."),
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self):
        if self.message is None or self.done:
            return
        self.clear_items()
        self.add_item(
            discord.ui.TextDisplay("this preview expired. run the command again.")
        )
        try:
            await self.message.edit(view=self, attachments=[])
        except discord.HTTPException:
            pass

    async def refresh(self):
        if self.message is None:
            return
        self.build()
        try:
            await self.message.edit(view=self, attachments=self.files())
        except discord.HTTPException:
            pass

    async def confirm(self, interaction):
        settings = self.settings
        guild = self.ctx.guild
        channel = guild.get_channel(settings.get("channel_id"))

        if channel is None:
            await interaction.response.send_message(
                embed=embeds.error(
                    "the proof channel is gone. an admin needs to set it "
                    f"again with `{display_prefix(self.ctx)}proof setup`."
                ),
                ephemeral=True,
            )
            return

        if not channel.permissions_for(guild.me).send_messages:
            await interaction.response.send_message(
                embed=embeds.error("i cannot post in the proof channel."),
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        try:
            sent = await post_proof(
                channel, guild, settings, self.record, self.media, self.ctx.author
            )
        except discord.Forbidden:
            await interaction.followup.send(
                embed=embeds.error("i cannot post in the proof channel."),
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            log.exception("proof post rejected in %s", channel.id)
            await interaction.followup.send(
                embed=embeds.error("discord turned that proof down."),
                ephemeral=True,
            )
            return

        self.record["message_id"] = sent.id
        proofs.append(self.record)
        save_proofs()

        await refresh_sticky(guild, settings, channel)

        self.done = True
        self.clear_items()
        self.add_item(
            discord.ui.TextDisplay(
                "**posted!** thank you for trusting us, come again."
            )
        )
        try:
            await self.message.edit(view=self, attachments=[])
        except discord.HTTPException:
            pass

        link = discord.ui.View()
        link.add_item(discord.ui.Button(label="check proof", url=sent.jump_url))
        await interaction.followup.send(
            embed=embeds.notice("your proof has been **posted**!"),
            view=link,
            ephemeral=True,
        )
        self.stop()


class TemplateModal(discord.ui.Modal, title="Proof Format"):
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
                embed=embeds.error("the format cannot be empty."),
                ephemeral=True,
            )
            return

        if len(text) > TEMPLATE_LIMIT:
            await interaction.response.send_message(
                embed=embeds.error(
                    f"keep the format under **{TEMPLATE_LIMIT}** characters."
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
                f"{listed} is not a field i know, so it prints as written. "
                "the fields are "
                + ", ".join(f"`{{{f}}}`" for f in FIELDS)
                + "."
            )

        dead = emojiutils.unresolved_names(text, interaction.guild)
        if dead:
            listed = ", ".join(f"`:{d}:`" for d in dead)
            notes.append(
                f"{listed} does not match an emoji in this server, so it "
                "prints as text."
            )

        if notes:
            await interaction.followup.send(
                embed=embeds.error("saved, but " + "\n\n".join(notes)),
                ephemeral=True,
            )


class CounterModal(discord.ui.Modal, title="Counter Format"):
    def __init__(self, builder):
        super().__init__()
        self.builder = builder
        self.f_counter = discord.ui.TextInput(
            label="Counter",
            default=builder.settings.get("counter", DEFAULT_COUNTER)[:1000],
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=True,
        )
        self.add_item(self.f_counter)

    async def on_submit(self, interaction):
        text = self.f_counter.value.strip()

        if not text:
            await interaction.response.send_message(
                embed=embeds.error("the counter cannot be empty."),
                ephemeral=True,
            )
            return

        if len(text) > COUNTER_LIMIT:
            await interaction.response.send_message(
                embed=embeds.error(
                    f"keep the counter under **{COUNTER_LIMIT}** characters."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        self.builder.settings["counter"] = text
        save_config()
        await self.builder.refresh()

        guild = interaction.guild
        channel = guild.get_channel(self.builder.settings.get("channel_id"))
        await refresh_sticky(guild, self.builder.settings, channel)


class ChannelView(discord.ui.View):
    def __init__(self, builder):
        super().__init__(timeout=300)
        self.builder = builder

    async def interaction_check(self, interaction):
        return interaction.user.id == self.builder.ctx.author.id

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        placeholder="Where every proof lands",
        row=0,
    )
    async def pick(self, interaction, select):
        await interaction.response.defer()
        guild = interaction.guild
        self.builder.settings["channel_id"] = select.values[0].id
        self.builder.settings["sticky_id"] = None
        save_config()
        await self.builder.refresh()
        await refresh_sticky(
            guild, self.builder.settings, guild.get_channel(select.values[0].id)
        )


class SetupView(discord.ui.View):
    def __init__(self, ctx, settings):
        super().__init__(timeout=900)
        self.ctx = ctx
        self.settings = settings
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                embed=embeds.error("this deck isn't yours."),
                ephemeral=True,
            )
            return False
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                embed=embeds.error("you need **manage server** permission."),
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
            f"**Drops in** · {channel.mention if channel else 'not set'}",
            f"**Image or video** · "
            f"{'required' if settings.get('require_image') else 'optional'}",
            f"**Who can post** · "
            f"{'staff only' if settings.get('staff_only') else 'anyone'}",
            f"**Pings the poster** · {'yes' if settings.get('ping') else 'no'}",
            f"**Proofs counted** · {total_for(guild.id)}",
            "",
            "**Preview**",
            templating.render(settings["template"], SAMPLE, ALIASES, guild),
            "",
            "**Sticky counter**",
            templating.render(
                settings.get("counter", DEFAULT_COUNTER),
                COUNTER_SAMPLE,
                COUNTER_ALIASES,
                guild,
            ),
        ]

        return embeds.build("\n".join(lines)[:4096])

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
            embed=embeds.notice("pick where every proof should land."),
            view=ChannelView(self),
            ephemeral=True,
        )

    @discord.ui.button(label="format", style=discord.ButtonStyle.secondary, row=0)
    async def format_button(self, interaction, button):
        await interaction.response.send_modal(TemplateModal(self))

    @discord.ui.button(label="counter", style=discord.ButtonStyle.secondary, row=0)
    async def counter_button(self, interaction, button):
        await interaction.response.send_modal(CounterModal(self))

    @discord.ui.button(label="fields", style=discord.ButtonStyle.secondary, row=0)
    async def fields(self, interaction, button):
        await interaction.response.send_message(
            embed=embeds.notice(
                "proof format fields:\n\n"
                + "\n".join(f"`{{{f}}}`" for f in FIELDS)
                + "\n\ncounter fields:\n\n"
                + "\n".join(f"`{{{f}}}`" for f in COUNTER_FIELDS)
                + "\n\ntype `:name:` for a server emoji. the proof image or "
                "video is added under the text automatically."
            ),
            ephemeral=True,
        )

    @discord.ui.button(label="require proof", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_image(self, interaction, button):
        await interaction.response.defer()
        self.settings["require_image"] = not self.settings.get("require_image", True)
        save_config()
        await self.refresh()

    @discord.ui.button(label="who can post", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_staff(self, interaction, button):
        await interaction.response.defer()
        self.settings["staff_only"] = not self.settings.get("staff_only", False)
        save_config()
        await self.refresh()

    @discord.ui.button(label="toggle ping", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_ping(self, interaction, button):
        await interaction.response.defer()
        self.settings["ping"] = not self.settings.get("ping", True)
        save_config()
        await self.refresh()

    @discord.ui.button(label="repost counter", style=discord.ButtonStyle.secondary, row=2)
    async def repost(self, interaction, button):
        await interaction.response.defer()
        guild = interaction.guild
        channel = guild.get_channel(self.settings.get("channel_id"))
        await refresh_sticky(guild, self.settings, channel)
        await self.refresh()

    @discord.ui.button(label="reset format", style=discord.ButtonStyle.danger, row=2)
    async def reset_format(self, interaction, button):
        await interaction.response.defer()
        self.settings["template"] = DEFAULT_TEMPLATE
        self.settings["counter"] = DEFAULT_COUNTER
        save_config()
        await self.refresh()


class Proof(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.NoPrivateMessage):
            await embeds.send(
                ctx, embeds.error("this command only works in a **server**.")
            )
            return
        if isinstance(error, commands.MissingPermissions):
            await embeds.send(
                ctx,
                embeds.error("you need **manage server** permission for that."),
            )
            return
        if isinstance(error, commands.CommandOnCooldown):
            await embeds.send(
                ctx,
                embeds.error(
                    f"slow down, try again in **{error.retry_after:.0f}s**."
                ),
            )
            return

        log.exception("Unhandled error in %s", ctx.command, exc_info=error)
        await embeds.send(
            ctx, embeds.error("something broke on my end. it has been logged.")
        )

    @commands.hybrid_group(
        name="proof",
        invoke_without_command=True,
        fallback="add",
        description="Post a proof with an image or video and feedback.",
    )
    @app_commands.describe(
        item="What the proof is for",
        proof="A screenshot, photo, or video as proof",
        feedback="Your feedback",
    )
    @commands.guild_only()
    @commands.cooldown(1, COOLDOWN_SECONDS, commands.BucketType.user)
    async def proof(
        self,
        ctx,
        item: str,
        proof: discord.Attachment = None,
        *,
        feedback: str,
    ):
        await ctx.defer(ephemeral=True)

        settings = settings_for(ctx.guild.id)
        channel = ctx.guild.get_channel(settings.get("channel_id"))

        if channel is None:
            await embeds.send(
                ctx,
                embeds.error(
                    "no proof channel set yet. someone with **manage server** "
                    f"needs to run `{display_prefix(ctx)}proof setup`."
                ),
            )
            return

        if settings.get("staff_only") and not ctx.author.guild_permissions.manage_messages:
            await embeds.send(
                ctx, embeds.error("only **staff** can post proofs here.")
            )
            return

        item = item.strip()
        feedback = feedback.strip()

        if not item:
            await embeds.send(ctx, embeds.error("tell me **what** this proof is for."))
            return
        if len(item) > ITEM_LIMIT:
            await embeds.send(
                ctx,
                embeds.error(f"keep the **item** under **{ITEM_LIMIT}** characters."),
            )
            return
        if not feedback:
            await embeds.send(ctx, embeds.error("add some **feedback**."))
            return
        if len(feedback) > FEEDBACK_LIMIT:
            await embeds.send(
                ctx,
                embeds.error(
                    f"keep the **feedback** under **{FEEDBACK_LIMIT}** characters."
                ),
            )
            return

        if proof is None and settings.get("require_image", True):
            await embeds.send(
                ctx, embeds.error("attach an **image or video** as proof.")
            )
            return

        media, problem = await attach.read_media(proof, limit=attach.limit_for(ctx.guild))
        if problem:
            await embeds.send(ctx, embeds.error(problem))
            return

        if not channel.permissions_for(ctx.guild.me).send_messages:
            await embeds.send(
                ctx, embeds.error("i cannot post in the proof channel.")
            )
            return

        record = {
            "id": uuid.uuid4().hex[:8],
            "guild_id": ctx.guild.id,
            "user_id": ctx.author.id,
            "item": item,
            "feedback": feedback,
            "created_at": int(time.time()),
            "count": count_for(ctx.guild.id, ctx.author.id) + 1,
            "message_id": None,
        }

        view = PreviewView(ctx, settings, record, media)

        view.message = await ctx.send(
            view=view,
            files=view.files(),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @proof.command(
        name="setup",
        description="Customise where proofs drop and how the counter looks.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    async def proof_setup(self, ctx):
        settings = ensure_config(ctx.guild.id)
        save_config()

        view = SetupView(ctx, settings)
        view.message = await ctx.send(
            embed=view.status_embed(),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @proof.command(name="count", description="How many proofs someone has posted.")
    @app_commands.describe(user="Who to check. Defaults to you.")
    async def proof_count(self, ctx, user: discord.Member = None):
        target = user or ctx.author
        total = count_for(ctx.guild.id, target.id)

        if not total:
            await embeds.send(
                ctx, embeds.notice(f"{target.display_name} has no proofs yet.")
            )
            return

        await embeds.send(
            ctx,
            embeds.notice(
                f"{target.display_name} has posted **{total}** "
                f"proof{'' if total == 1 else 's'}."
            ),
        )

    @proof.command(name="total", description="How many proofs this server has.")
    async def proof_total(self, ctx):
        await embeds.send(
            ctx,
            embeds.notice(f"this server has **{total_for(ctx.guild.id)}** proofs."),
        )

    @proof.command(name="top", description="Who has posted the most proofs.")
    async def proof_top(self, ctx):
        tally = {}
        for record in guild_proofs(ctx.guild.id):
            tally[record["user_id"]] = tally.get(record["user_id"], 0) + 1

        if not tally:
            await embeds.send(ctx, embeds.notice("no proofs here yet."))
            return

        ranked = sorted(tally.items(), key=lambda kv: -kv[1])[:10]
        lines = []
        for place, (user_id, count) in enumerate(ranked, 1):
            member = ctx.guild.get_member(user_id)
            name = member.display_name if member else f"<@{user_id}>"
            lines.append(f"`{place}.` {name} · {count}")

        await embeds.send(ctx, embeds.build("\n".join(lines)))


async def setup(bot):
    await bot.add_cog(Proof(bot))