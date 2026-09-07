import logging
import os
import time

import discord
from discord import app_commands
from discord.ext import commands

import embeds
from storage import Store

try:
    from PIL import Image
except ImportError:  # pillow 
    Image = None

log = logging.getLogger(__name__)

PUBLIC_COMMANDS = ("stamps",)
PUBLIC_SUBCOMMANDS = ("stamp profile",)

MAX_STAGES = 21
ALLOWED_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".webp")

BLOCK_WORDS = ("off", "none", "no", "nothing", "remove")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGE_ROOT = os.path.join(ROOT, "data", "stamp")


def guild_dir(guild_id):
    return os.path.join(IMAGE_ROOT, str(guild_id))


def stage_path(guild_id, filename):
    return os.path.join(guild_dir(guild_id), filename)


def default_config():
    return {"stages": {}, "milestones": {}, "version": 0}


def milestones_for(config):
    marks = {}
    for key, value in config.get("milestones", {}).items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue
        if value:
            marks[index] = value
    return marks


def next_milestone(config, count, maximum):
    marks = milestones_for(config)
    for index in range(count + 1, maximum + 1):
        if index in marks:
            return index, marks[index]
    return 0, ""


def blank_record():
    return {"count": 0, "completed": 0, "first_seen": 0.0, "last": 0.0, "active": False}


def card_active(record):
    """Whether this member is currently holding a card.

    Records written before cards had to be issued carry no active flag, so
    a missing key means yes. blank_record sets it to False explicitly, which
    is what makes an unknown member read as having no card.
    """
    return bool(record.get("active", True))


def suffix_for(attachment):
    name = (attachment.filename or "").lower()
    for suffix in ALLOWED_SUFFIX:
        if name.endswith(suffix):
            return suffix
    return ""


def trim_margins(path):
    """Crop fully transparent padding off a card image so it fills the frame.

    Stamp art is often exported on a larger transparent canvas, which Discord
    then shows at full width with empty space down the sides. Cropping to the
    alpha bounding box makes the card itself fill the image.

    Best effort and safe: does nothing without Pillow, on formats with no alpha
    channel (jpg), on animated images, or when the art is already tight. Every
    stage shares the same card outline, so each trims to the same box and the
    stages stay aligned.
    """
    if Image is None:
        return
    if not path.lower().endswith((".png", ".webp")):
        return
    try:
        with Image.open(path) as img:
            if getattr(img, "is_animated", False):
                return
            if img.mode not in ("RGBA", "LA") and not (
                img.mode == "P" and "transparency" in img.info
            ):
                return
            rgba = img.convert("RGBA")
            bbox = rgba.getchannel("A").getbbox()
            if bbox and bbox != (0, 0, rgba.width, rgba.height):
                rgba.crop(bbox).save(path)
    except (OSError, ValueError):
        return


class OverrideModal(discord.ui.Modal, title="Override stamps"):
    def __init__(self, cog, member, maximum, record):
        super().__init__(timeout=300)
        self.cog = cog
        self.member = member
        self.maximum = maximum
        self.count = discord.ui.TextInput(
            label="Stamps on the current card",
            placeholder="0 to " + str(maximum),
            default=str(record["count"]),
            max_length=3,
            required=True,
        )
        self.completed = discord.ui.TextInput(
            label="Full cards finished before now",
            placeholder="Leave as is to keep the current total",
            default=str(record["completed"]),
            max_length=4,
            required=False,
        )
        self.add_item(self.count)
        self.add_item(self.completed)

    async def on_submit(self, interaction):
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message(
                embed=embeds.error(
                    "you need the manage messages permission to do that.",
                    title="Not allowed",
                ),
                ephemeral=True,
            )
            return

        try:
            count = int(self.count.value.strip())
        except ValueError:
            await interaction.response.send_message(
                embed=embeds.error("stamps must be a whole number."), ephemeral=True
            )
            return

        if count < 0 or count > self.maximum:
            await interaction.response.send_message(
                embed=embeds.error(
                    "stamps must be between 0 and " + str(self.maximum) + "."
                ),
                ephemeral=True,
            )
            return

        completed = None
        raw = self.completed.value.strip()
        if raw:
            try:
                completed = max(0, int(raw))
            except ValueError:
                await interaction.response.send_message(
                    embed=embeds.error("full cards must be a whole number."),
                    ephemeral=True,
                )
                return

        record = self.cog.write_record(interaction.guild.id, self.member.id, count, completed)
        await self.cog.refresh_card(
            interaction, self.member, count, record["completed"], "Card was set manually.", False
        )


class ConfirmReset(discord.ui.View):
    def __init__(self, cog, member, parent):
        super().__init__(timeout=60)
        self.cog = cog
        self.member = member
        self.parent = parent

    async def interaction_check(self, interaction):
        if interaction.user.guild_permissions.manage_messages:
            return True
        await interaction.response.send_message(
            embed=embeds.error(
                "you need the manage messages permission to do that.",
                title="Not allowed",
            ),
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="reset stamps", style=discord.ButtonStyle.secondary)
    async def confirm(self, interaction, button):
        record = self.cog.write_record(interaction.guild.id, self.member.id, 0, None)
        await interaction.response.edit_message(
            embed=embeds.notice(
                "reset the current card for " + self.member.display_name + "."
            ),
            view=None,
        )
        await self.cog.refresh_card(
            None, self.member, 0, record["completed"], "Card was reset.", False, message=self.parent
        )
        self.stop()

    @discord.ui.button(label="cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(
            embed=embeds.notice("nothing was changed."), view=None
        )
        self.stop()


class StaffView(discord.ui.View):
    def __init__(self, cog, member):
        super().__init__(timeout=900)
        self.cog = cog
        self.member = member

    async def interaction_check(self, interaction):
        if interaction.user.guild_permissions.manage_messages:
            return True
        await interaction.response.send_message(
            embed=embeds.error(
                "you need the manage messages permission to do that.",
                title="Not allowed",
            ),
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="stamp", style=discord.ButtonStyle.secondary)
    async def add_one(self, interaction, button):
        status, record, notes, celebrate, display = self.cog.push_stamp(
            interaction.guild.id, self.member.id, 1
        )
        if status == "nosetup":
            await interaction.response.send_message(
                embed=embeds.error("stamp cards are not set up."), ephemeral=True
            )
            return
        if status == "nocard":
            await interaction.response.send_message(
                embed=embeds.error(
                    self.member.display_name + " has no active card. give them one first."
                ),
                ephemeral=True,
            )
            return
        await self.cog.refresh_card(
            interaction, self.member, display, record["completed"], notes, celebrate
        )

    @discord.ui.button(label="override", style=discord.ButtonStyle.secondary)
    async def override(self, interaction, button):
        config = self.cog.config_for(interaction.guild.id)
        maximum = self.cog.maximum_for(interaction.guild.id, config)
        record = self.cog.record_for(interaction.guild.id, self.member.id)
        await interaction.response.send_modal(
            OverrideModal(self.cog, self.member, maximum, record)
        )

    @discord.ui.button(label="reset", style=discord.ButtonStyle.secondary)
    async def reset(self, interaction, button):
        await interaction.response.send_message(
            embed=embeds.notice(
                "reset the current card for " + self.member.display_name
                + "? finished cards are kept.",
                title="Reset card",
            ),
            view=ConfirmReset(self.cog, self.member, interaction.message),
            ephemeral=True,
        )


class IssueView(discord.ui.View):
    def __init__(self, cog, member):
        super().__init__(timeout=900)
        self.cog = cog
        self.member = member

    async def interaction_check(self, interaction):
        if interaction.user.guild_permissions.manage_messages:
            return True
        await interaction.response.send_message(
            embed=embeds.error(
                "you need the manage messages permission to do that.",
                title="Not allowed",
            ),
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="give a card", style=discord.ButtonStyle.secondary)
    async def give(self, interaction, button):
        record = self.cog.issue_card(interaction.guild.id, self.member.id)
        if record is None:
            await interaction.response.send_message(
                embed=embeds.error(self.member.display_name + " already has a card."),
                ephemeral=True,
            )
            return
        await self.cog.refresh_card(
            interaction, self.member, 0, record["completed"], "New card issued.", False
        )


class Stamp(commands.Cog):
    """Loyalty stamp cards that fill up as members earn stamps."""

    def __init__(self, bot):
        self.bot = bot
        self.store = Store("stamps.json")

    async def cog_check(self, ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        qualified = ctx.command.qualified_name
        if qualified in PUBLIC_SUBCOMMANDS:
            return True
        root = qualified.split()[0]
        if root in PUBLIC_COMMANDS:
            return True
        if ctx.author.guild_permissions.manage_messages:
            return True
        raise commands.MissingPermissions(["manage_messages"])

    async def cog_command_error(self, ctx, error):
        error = getattr(error, "original", error)
        if isinstance(error, commands.MissingPermissions):
            await embeds.send(
                ctx,
                embeds.error(
                    "you need the manage messages permission to use that.",
                    title="Not allowed",
                ),
            )
        elif isinstance(error, commands.CommandOnCooldown):
            await embeds.send(
                ctx,
                embeds.error(
                    "slow down, try again in " + str(round(error.retry_after, 1)) + " seconds.",
                    title="Slow down",
                ),
            )
        elif isinstance(error, commands.MemberNotFound):
            await embeds.send(ctx, embeds.error("i could not find that member."))
        elif isinstance(error, commands.NoPrivateMessage):
            await embeds.send(ctx, embeds.error("stamp cards only work inside a server."))
        elif isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
            await embeds.send(
                ctx,
                embeds.error(
                    "bad arguments. run `" + ctx.prefix + "stamp setup` to see the usage.",
                    title="Bad arguments",
                ),
            )
        else:
            raise error

    def guild_block(self, data, guild_id):
        key = str(guild_id)
        block = data.get(key)
        if block is None:
            block = {"config": default_config(), "users": {}}
            data[key] = block
        block.setdefault("config", default_config())
        block.setdefault("users", {})
        for field, value in default_config().items():
            block["config"].setdefault(field, value)
        return block

    def config_for(self, guild_id):
        return self.guild_block(self.store.load(), guild_id)["config"]

    def stage_count(self, guild_id, config):
        stages = config.get("stages", {})
        total = 0
        while str(total) in stages and os.path.isfile(
            stage_path(guild_id, stages[str(total)])
        ):
            total += 1
        return total

    def maximum_for(self, guild_id, config):
        return max(0, self.stage_count(guild_id, config) - 1)

    def missing_stages(self, guild_id, config):
        stages = config.get("stages", {})
        highest = -1
        for key in stages:
            try:
                highest = max(highest, int(key))
            except ValueError:
                continue
        gaps = []
        for index in range(highest + 1):
            filename = stages.get(str(index))
            if not filename or not os.path.isfile(stage_path(guild_id, filename)):
                gaps.append(index)
        return gaps, highest

    def stage_file(self, guild_id, config, count):
        filename = config.get("stages", {}).get(str(count))
        if not filename:
            return None
        path = stage_path(guild_id, filename)
        if not os.path.isfile(path):
            return None
        return path

    def record_for(self, guild_id, user_id):
        block = self.guild_block(self.store.load(), guild_id)
        return block["users"].get(str(user_id), blank_record())

    def write_record(self, guild_id, user_id, count, completed, active=True):
        data = self.store.load()
        block = self.guild_block(data, guild_id)
        record = block["users"].get(str(user_id), blank_record())
        now = time.time()
        if not record["first_seen"]:
            record["first_seen"] = now
        record["count"] = count
        if completed is not None:
            record["completed"] = max(0, completed)
        record["active"] = bool(active)
        record["last"] = now
        block["users"][str(user_id)] = record
        self.store.save(data)
        return record

    def issue_card(self, guild_id, user_id):
        """Hand a member a fresh blank card.

        Returns None when they are already holding one, so the caller can
        say so rather than quietly wiping progress.
        """
        data = self.store.load()
        block = self.guild_block(data, guild_id)
        record = block["users"].get(str(user_id), blank_record())
        if str(user_id) in block["users"] and card_active(record):
            return None

        now = time.time()
        if not record["first_seen"]:
            record["first_seen"] = now
        record["count"] = 0
        record["active"] = True
        record["last"] = now
        block["users"][str(user_id)] = record
        self.store.save(data)
        return record

    def push_stamp(self, guild_id, user_id, delta):
        """Move a member's count and report what happened.

        Returns a status first so callers can tell the three failure modes
        apart. A card stops at its last stamp rather than rolling over, and
        anything left in the batch is reported instead of silently starting
        the next card.
        """
        config = self.config_for(guild_id)
        if self.stage_count(guild_id, config) < 2:
            return "nosetup", None, "", False, 0

        maximum = self.maximum_for(guild_id, config)
        marks = milestones_for(config)

        data = self.store.load()
        block = self.guild_block(data, guild_id)
        record = block["users"].get(str(user_id), blank_record())

        if not card_active(record):
            return "nocard", record, "", False, 0

        now = time.time()
        if not record["first_seen"]:
            record["first_seen"] = now

        total = record["count"]
        finished = False
        reached = []
        leftover = 0

        if delta > 0:
            for step in range(delta):
                if finished:
                    leftover = delta - step
                    break
                total += 1
                if total in marks:
                    reached.append((total, marks[total]))
                if total >= maximum:
                    finished = True
        else:
            total = max(0, total + delta)

        display = total

        if finished:
            record["completed"] = max(0, record["completed"] + 1)
            record["active"] = False
            record["count"] = 0
        else:
            record["count"] = total

        record["last"] = now
        block["users"][str(user_id)] = record
        self.store.save(data)

        notes = []
        for index, prize in reached:
            notes.append("Reward unlocked at " + str(index) + ": " + prize)
        if finished:
            notes.append("Card complete. Ask staff for a new one to keep going.")
        if leftover:
            word = "stamp" if leftover == 1 else "stamps"
            notes.append(
                str(leftover) + " " + word + " were not applied, the card filled up first."
            )

        return "ok", record, "\n".join(notes), bool(finished or reached), display

    def build_card(self, guild_id, member, count, completed, staff):
        """The card is just the stamp image, plus staff buttons.

        Returns (file, view). The remaining count lives on `stamp profile`
        now, so the card message itself carries no text. note/complete are
        accepted by callers for compatibility but ignored here.
        """
        config = self.config_for(guild_id)
        path = self.stage_file(guild_id, config, count)
        if path is None:
            return None, None

        filename = "stamp-card" + os.path.splitext(path)[1]
        file = discord.File(path, filename=filename)
        view = StaffView(self, member) if staff else None
        return file, view

    async def send_card(self, ctx, member, config, count, completed, note="", complete=False):
        staff = ctx.author.guild_permissions.manage_messages
        file, view = self.build_card(ctx.guild.id, member, count, completed, staff)
        if file is None:
            await embeds.send(
                ctx,
                embeds.error(
                    "no image is set for " + str(count) + " stamps. run `"
                    + ctx.prefix + "stamp setup`."
                ),
            )
            return
        await ctx.send(
            file=file,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def refresh_card(
        self, interaction, member, count, completed, note="", complete=False, message=None
    ):
        guild_id = interaction.guild.id if interaction is not None else message.guild.id
        file, view = self.build_card(guild_id, member, count, completed, True)
        if file is None:
            if interaction is not None:
                await interaction.response.send_message(
                    embed=embeds.error("no image is set for " + str(count) + " stamps."),
                    ephemeral=True,
                )
            return

        if interaction is not None:
            await interaction.response.edit_message(
                content=None, attachments=[file], view=view
            )
        else:
            await message.edit(content=None, attachments=[file], view=view)

    def collect_images(self, ctx):
        attachments = list(ctx.message.attachments)
        reference = ctx.message.reference
        if not attachments and reference is not None:
            resolved = reference.resolved
            if isinstance(resolved, discord.Message):
                attachments = list(resolved.attachments)
        return attachments

    async def store_stage(self, ctx, attachment, index):
        suffix = suffix_for(attachment)
        if not suffix:
            return "stage " + str(index) + ": unsupported file type, use png, jpg, gif or webp."
        if attachment.size > ctx.guild.filesize_limit:
            return "stage " + str(index) + ": file is too large for this server."

        folder = guild_dir(ctx.guild.id)
        os.makedirs(folder, exist_ok=True)

        for old in ALLOWED_SUFFIX:
            stale = os.path.join(folder, "stage_" + str(index) + old)
            if os.path.isfile(stale):
                try:
                    os.remove(stale)
                except OSError:
                    pass

        filename = "stage_" + str(index) + suffix
        dest = os.path.join(folder, filename)
        await attachment.save(dest)
        trim_margins(dest)
        return filename

    @commands.hybrid_command(
        name="stamps",
        aliases=["mycard", "stampcard"],
        description="Show a stamp card.",
    )
    @app_commands.describe(member="Whose card to show. Defaults to your own.")
    @commands.guild_only()
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def stamps(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        config = self.config_for(ctx.guild.id)
        if self.stage_count(ctx.guild.id, config) < 2:
            ctx.command.reset_cooldown(ctx)
            await embeds.send(ctx, embeds.error("stamp cards are not set up here yet."))
            return
        record = self.guild_block(self.store.load(), ctx.guild.id)["users"].get(
            str(member.id), blank_record()
        )
        if not card_active(record):
            ctx.command.reset_cooldown(ctx)
            await self.send_no_card(ctx, member)
            return
        await self.send_card(ctx, member, config, record["count"], record["completed"], "")

    @commands.hybrid_command(
        name="givestampcard",
        aliases=["givecard", "issuecard", "newcard"],
        description="Hand a member a fresh blank stamp card.",
    )
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(member="Who to give a card to")
    @commands.guild_only()
    async def give_stamp_card(self, ctx, member: discord.Member):
        if member.bot:
            await embeds.send(ctx, embeds.error("bots do not collect stamps."))
            return

        config = self.config_for(ctx.guild.id)
        if self.stage_count(ctx.guild.id, config) < 2:
            await embeds.send(
                ctx, embeds.error("run `" + ctx.prefix + "stamp setup` first.")
            )
            return

        record = self.issue_card(ctx.guild.id, member.id)
        if record is None:
            current = self.record_for(ctx.guild.id, member.id)
            await embeds.send(
                ctx,
                embeds.error(
                    member.display_name + " already has a card with "
                    + str(current["count"]) + " stamp(s) on it. use `"
                    + ctx.prefix + "stamp clear` to wipe them first."
                ),
            )
            return

        note = "New card issued."
        if record["completed"]:
            word = "card" if record["completed"] == 1 else "cards"
            note += " That is " + str(record["completed"] + 1) + " overall, with "
            note += str(record["completed"]) + " full " + word + " already done."
        await self.send_card(ctx, member, config, 0, record["completed"], note)

    @commands.hybrid_group(
        name="stamp",
        invoke_without_command=True,
        fallback="give",
        description="Give a stamp, or manage cards and setup.",
    )
    @commands.guild_only()
    @app_commands.describe(member="Who to stamp. Leave empty to see setup.")
    async def stamp(self, ctx, member: discord.Member = None):
        if member is None:
            await self.show_setup(ctx)
            return
        await self.apply(ctx, member, 1)

    @stamp.command(name="profile", description="Check stamp card progress.")
    @app_commands.describe(member="Whose progress to check. Defaults to your own.")
    async def stamp_profile(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        config = self.config_for(ctx.guild.id)
        if self.stage_count(ctx.guild.id, config) < 2:
            await embeds.send(ctx, embeds.error("stamp cards are not set up here yet."))
            return

        maximum = self.maximum_for(ctx.guild.id, config)
        record = self.record_for(ctx.guild.id, member.id)
        count = record["count"]
        completed = record.get("completed", 0)

        who = "Your" if member.id == ctx.author.id else member.display_name + "'s"
        lines = []

        if card_active(record):
            remaining = max(0, maximum - count)
            word = "stamp" if remaining == 1 else "stamps"
            lines.append(str(count) + " of " + str(maximum) + " stamps on the current card.")
            lines.append(str(remaining) + " " + word + " to go until it is complete.")

            upcoming, prize = next_milestone(config, count, maximum)
            if prize:
                left = upcoming - count
                word = "stamp" if left == 1 else "stamps"
                lines.append(
                    "next reward at " + str(upcoming)
                    + " (" + str(left) + " " + word + " away): " + prize
                )
        else:
            lines.append("no active card right now. ask a staff member for one.")

        if completed:
            word = "card" if completed == 1 else "cards"
            lines.append(str(completed) + " full " + word + " finished so far.")

        await embeds.send(
            ctx, embeds.build("\n".join(lines), title=who + " stamp progress")
        )

    @stamp.command(name="add", description="Give a member one or more stamps.")
    @app_commands.describe(member="Who to stamp", amount="How many to add")
    async def stamp_add(self, ctx, member: discord.Member, amount: int = 1):
        if amount < 1:
            await embeds.send(ctx, embeds.error("amount must be at least 1."))
            return
        await self.apply(ctx, member, amount)

    @stamp.command(
        name="remove",
        aliases=["undo"],
        description="Take stamps back off a member's card.",
    )
    @app_commands.describe(member="Whose card to correct", amount="How many to remove")
    async def stamp_remove(self, ctx, member: discord.Member, amount: int = 1):
        if amount < 1:
            await embeds.send(ctx, embeds.error("amount must be at least 1."))
            return
        await self.apply(ctx, member, -amount)

    @stamp.command(
        name="set",
        aliases=["override"],
        description="Set a member's card to an exact count.",
    )
    @app_commands.describe(
        member="Whose card to set",
        count="Stamps on the current card",
        completed="Full cards finished before now",
    )
    async def stamp_set(self, ctx, member: discord.Member, count: int, completed: int = None):
        config = self.config_for(ctx.guild.id)
        if self.stage_count(ctx.guild.id, config) < 2:
            await embeds.send(
                ctx, embeds.error("run `" + ctx.prefix + "stamp setup` first.")
            )
            return
        maximum = self.maximum_for(ctx.guild.id, config)
        if count < 0 or count > maximum:
            await embeds.send(
                ctx, embeds.error("count must be between 0 and " + str(maximum) + ".")
            )
            return

        record = self.write_record(ctx.guild.id, member.id, count, completed)
        await self.send_card(
            ctx, member, config, count, record["completed"], "Card was set manually."
        )

    @stamp.command(name="clear", description="Delete a member's stamp record.")
    @app_commands.describe(member="Whose record to delete")
    async def stamp_clear(self, ctx, member: discord.Member):
        data = self.store.load()
        block = self.guild_block(data, ctx.guild.id)
        if block["users"].pop(str(member.id), None) is None:
            await embeds.send(
                ctx, embeds.error(member.display_name + " has no stamp record.")
            )
            return
        self.store.save(data)
        await embeds.send(
            ctx,
            embeds.notice(
                "cleared the stamp record for " + member.display_name + ".",
                title="Record cleared",
            ),
        )

    async def apply(self, ctx, member, delta):
        if member.bot:
            await embeds.send(ctx, embeds.error("bots do not collect stamps."))
            return

        status, record, notes, celebrate, display = self.push_stamp(
            ctx.guild.id, member.id, delta
        )
        if status == "nosetup":
            await embeds.send(
                ctx, embeds.error("run `" + ctx.prefix + "stamp setup` first.")
            )
            return
        if status == "nocard":
            await embeds.send(
                ctx,
                embeds.error(
                    member.display_name + " has no active card. issue one with `"
                    + ctx.prefix + "givestampcard " + member.display_name + "`."
                ),
            )
            return

        config = self.config_for(ctx.guild.id)
        await self.send_card(
            ctx, member, config, display, record["completed"], notes, complete=celebrate
        )

    async def send_no_card(self, ctx, member):
        """No active card means there is no image to show, so this stays a
        short line, plus a button for staff to hand one over."""
        staff = ctx.author.guild_permissions.manage_messages
        mine = member.id == ctx.author.id

        if mine:
            content = "you do not have a card yet. ask a staff member for one."
        else:
            content = member.display_name + " does not have a card right now."

        view = IssueView(self, member) if staff else None
        await embeds.send(ctx, embeds.notice(content), view=view)

    @stamp.group(
        name="setup",
        aliases=["config"],
        invoke_without_command=True,
        fallback="show",
        description="Show the stamp card setup and what is still missing.",
    )
    async def setup(self, ctx):
        await self.show_setup(ctx)

    async def show_setup(self, ctx):
        config = self.config_for(ctx.guild.id)
        total = self.stage_count(ctx.guild.id, config)
        gaps, highest = self.missing_stages(ctx.guild.id, config)
        prefix = ctx.prefix

        if total >= 2:
            status = (
                "ready. "
                + str(total)
                + " images loaded, covering 0 through "
                + str(total - 1)
                + " stamps."
            )
        elif highest < 0:
            status = "nothing uploaded yet."
        else:
            status = "incomplete. missing images for " + ", ".join(str(g) for g in gaps) + "."

        body = (
            status
            + "\n\nupload one image per stamp count, starting with the blank card. "
            "for an eight stamp card that is nine images, blank through full.\n\n"
            "`" + prefix + "stamp setup upload` — attach the images in order, blank card first\n"
            "`" + prefix + "stamp setup image 3` — attach one image to replace a single stage\n"
            "`" + prefix + "stamp setup reward 8 free milk tea` — set a reward at any stamp number\n"
            "`" + prefix + "stamp preview 3` — preview a stage\n\n"
            "discord allows **10 attachments per message**, so longer cards need a "
            "__second__ upload. just run `" + prefix + "stamp setup upload` again with the "
            "next batch and it carries on from where the last one stopped. the number of "
            "images sets the card length, so nine images means eight stamps then a reset."
        )

        await embeds.send(ctx, embeds.build(body, title="Stamp card setup"))

    @setup.command(name="upload", aliases=["images"], with_app_command=False)
    async def setup_upload(self, ctx, *, options: str = ""):
        """Save attached images to consecutive stages.

        With no options the batch is appended after the last stage already
        uploaded, so a card longer than ten images needs no arithmetic. A
        bare number overrides that and starts there instead. The word
        reverse flips the batch, which is the fix when a desktop file
        picker has handed the images over newest first.
        """
        attachments = self.collect_images(ctx)
        if not attachments:
            await embeds.send(
                ctx,
                embeds.error(
                    "attach your card images in order, or reply to a message holding them."
                ),
            )
            return

        start = None
        reverse = False
        for token in options.split():
            lowered = token.lower()
            if lowered in ("reverse", "rev", "desc", "backwards"):
                reverse = True
                continue
            try:
                start = int(token)
            except ValueError:
                await embeds.send(
                    ctx,
                    embeds.error(
                        "i did not understand `" + token + "`. give a stage number, or reverse."
                    ),
                )
                return

        config = self.config_for(ctx.guild.id)
        if start is None:
            start = self.stage_count(ctx.guild.id, config)

        if start < 0 or start + len(attachments) > MAX_STAGES:
            await embeds.send(
                ctx, embeds.error("a card can hold at most " + str(MAX_STAGES) + " stages.")
            )
            return

        if reverse:
            attachments = list(reversed(attachments))

        data = self.store.load()
        block = self.guild_block(data, ctx.guild.id)
        stages = block["config"]["stages"]

        saved = []
        problems = []
        for offset, attachment in enumerate(attachments):
            index = start + offset
            result = await self.store_stage(ctx, attachment, index)
            if result.startswith("stage "):
                problems.append(result)
                continue
            stages[str(index)] = result
            saved.append((index, attachment.filename))

        block["config"]["version"] = int(block["config"].get("version", 0)) + 1
        self.store.save(data)

        total = self.stage_count(ctx.guild.id, block["config"])
        lines = ["saved " + str(len(saved)) + " image(s)."]
        for index, filename in saved:
            label = "blank card" if index == 0 else "stamp " + str(index)
            lines.append("stage " + str(index) + " (" + label + ") is " + filename)
        if problems:
            lines.extend(problems)

        lines.append("")
        if total >= 2:
            lines.append("card is ready with " + str(total - 1) + " stamps before a reset.")
        else:
            lines.append("upload at least a blank card and one stamped version to go live.")

        gaps, highest = self.missing_stages(ctx.guild.id, block["config"])
        if gaps:
            listed = ", ".join(str(index) for index in gaps)
            lines.append(
                "stage " + listed + " is still empty, so the card stops short at "
                + str(total - 1) + ". images above the gap are ignored until it is filled."
            )
        elif len(saved) < MAX_STAGES - start:
            lines.append(
                "attach the next batch and run `" + ctx.prefix
                + "stamp setup upload` again to carry on from stage " + str(total) + "."
            )

        if saved:
            lines.append(
                "wrong way round? `" + ctx.prefix + "stamp setup upload "
                + str(start) + " reverse` redoes this batch backwards."
            )

        await embeds.send(
            ctx, embeds.notice("\n".join(lines)[:4000], title="Card images")
        )

    @setup.command(
        name="image",
        aliases=["stage"],
        description="Set the image for one stage of the card.",
    )
    @app_commands.describe(
        index="Which stage, where 0 is the blank card",
        image="The image to use. Prefix users can attach or reply instead.",
    )
    async def setup_image(self, ctx, index: int, image: discord.Attachment = None):
        if index < 0 or index >= MAX_STAGES:
            await embeds.send(
                ctx, embeds.error("stage must be between 0 and " + str(MAX_STAGES - 1) + ".")
            )
            return
        attachments = [image] if image is not None else self.collect_images(ctx)
        if not attachments:
            await embeds.send(
                ctx, embeds.error("attach one image, or reply to a message holding it.")
            )
            return

        result = await self.store_stage(ctx, attachments[0], index)
        if result.startswith("stage "):
            await embeds.send(ctx, embeds.error(result))
            return

        data = self.store.load()
        block = self.guild_block(data, ctx.guild.id)
        block["config"]["stages"][str(index)] = result
        block["config"]["version"] = int(block["config"].get("version", 0)) + 1
        self.store.save(data)
        await embeds.send(
            ctx, embeds.notice("stage " + str(index) + " updated.", title="Stage updated")
        )

    @setup.command(
        name="reward",
        aliases=["rewards"],
        description="Set, clear or list the reward attached to a stamp number.",
    )
    @app_commands.describe(
        target="A stamp number, or list, or clear",
        text="What they get. Leave blank to remove the reward.",
    )
    async def setup_reward(self, ctx, target: str = None, *, text: str = ""):
        target = (target or "").strip()
        text = text.strip()

        if not target or target.lower() in ("list", "show"):
            await self.show_rewards(ctx)
            return

        if target.lower() in ("clear", "unset", "remove"):
            await self.clear_reward(ctx, text)
            return

        try:
            count = int(target)
        except ValueError:
            await embeds.send(ctx, embeds.error("give a stamp number, or list, or clear."))
            return

        config = self.config_for(ctx.guild.id)
        maximum = self.maximum_for(ctx.guild.id, config)
        if count < 1 or count >= MAX_STAGES:
            await embeds.send(
                ctx,
                embeds.error("pick a stamp number between 1 and " + str(MAX_STAGES - 1) + "."),
            )
            return

        text = text[:200]
        if not text or text.lower() in BLOCK_WORDS:
            await self.drop_milestone(ctx, count)
            return

        data = self.store.load()
        block = self.guild_block(data, ctx.guild.id)
        block["config"].setdefault("milestones", {})[str(count)] = text
        self.store.save(data)

        reply = "reward at stamp " + str(count) + " set to: " + text
        if maximum and count > maximum:
            reply += "\nheads up, this card only goes to " + str(maximum) + " so nobody will reach it yet."
        await embeds.send(ctx, embeds.notice(reply, title="Reward set"))

    async def drop_milestone(self, ctx, count):
        data = self.store.load()
        block = self.guild_block(data, ctx.guild.id)
        marks = block["config"].setdefault("milestones", {})
        if marks.pop(str(count), None) is None:
            await embeds.send(
                ctx, embeds.error("there is no reward set at stamp " + str(count) + ".")
            )
            return
        self.store.save(data)
        await embeds.send(
            ctx, embeds.notice("removed the reward at stamp " + str(count) + ".")
        )

    async def clear_reward(self, ctx, target):
        """Remove one reward, or every reward when given all."""
        target = (target or "").strip().lower()

        if target == "all":
            data = self.store.load()
            block = self.guild_block(data, ctx.guild.id)
            block["config"]["milestones"] = {}
            self.store.save(data)
            await embeds.send(ctx, embeds.notice("every reward was removed."))
            return

        try:
            count = int(target)
        except ValueError:
            await embeds.send(ctx, embeds.error("give a stamp number, or all."))
            return
        await self.drop_milestone(ctx, count)

    async def show_rewards(self, ctx):
        config = self.config_for(ctx.guild.id)
        maximum = self.maximum_for(ctx.guild.id, config)
        marks = milestones_for(config)

        lines = []
        if not marks:
            lines.append("no rewards are set.")
        else:
            for index in sorted(marks):
                label = "stamp " + str(index) + ": " + marks[index]
                if maximum and index == maximum:
                    label += " (card complete)"
                elif maximum and index > maximum:
                    label += " (beyond this card)"
                lines.append(label)

        lines.append("")
        lines.append(
            "set one with `" + ctx.prefix + "stamp setup reward 4 free cookie`, "
            "remove it with `" + ctx.prefix + "stamp setup reward clear 4`."
        )

        await embeds.send(ctx, embeds.build("\n".join(lines), title="Rewards"))

    @setup.command(
        name="reset",
        description="Delete every uploaded card image. Collected stamps are kept.",
    )
    async def setup_reset(self, ctx):
        data = self.store.load()
        block = self.guild_block(data, ctx.guild.id)
        stages = block["config"].get("stages", {})
        for filename in list(stages.values()):
            try:
                os.remove(stage_path(ctx.guild.id, filename))
            except OSError:
                pass
        block["config"] = default_config()
        self.store.save(data)
        await embeds.send(
            ctx,
            embeds.notice(
                "stamp card images cleared. collected stamps were kept.",
                title="Setup reset",
            ),
        )

    @stamp.command(name="preview", description="Preview one stage of the card.")
    @app_commands.describe(count="Which stage to render. Defaults to the last one.")
    async def stamp_preview(self, ctx, count: int = None):
        config = self.config_for(ctx.guild.id)
        total = self.stage_count(ctx.guild.id, config)
        if total < 1:
            await embeds.send(ctx, embeds.error("nothing uploaded yet."))
            return
        maximum = total - 1
        if count is None:
            count = maximum
        count = max(0, min(count, maximum))
        path = self.stage_file(ctx.guild.id, config, count)
        if path is None:
            await embeds.send(ctx, embeds.error("no image is set for that stage."))
            return
        filename = "stamp-preview" + os.path.splitext(path)[1]
        await ctx.send(
            "-# stage " + str(count) + " of " + str(maximum),
            file=discord.File(path, filename=filename),
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot):
    await bot.add_cog(Stamp(bot))