import asyncio
import datetime
import logging
import re
import time
import uuid

import discord
from discord import app_commands
from discord.ext import commands

import embeds
from prefixes import display_prefix
import emojiutils
import templating
from scheduling import tz_for
from storage import Store, IntKeyStore

log = logging.getLogger(__name__)

_config_store = Store("queue_config.json")
_order_store = IntKeyStore("queue.json")

config = _config_store.load()
orders = _order_store.load()

MAX_STATUSES = 8
MENU_LIMIT = 25
TEMPLATE_LIMIT = 3800

PAD = "\u3164"
JOINER = "\u2060"

DEFAULT_TEMPLATE = (
    "order for {user}\n"
    "\n"
    "ticket: {ticket}\n"
    "{quantity}x {item}\n"
    "{price} via {payment}\n"
    "status: {status}\n"
    "handler: {handler}\n"
    "{date}"
)

DEFAULT_PLACEHOLDER = "update order status"
DEFAULT_OPTION_TEXT = "change order status"
DEFAULT_NOTIFY = "order queued in {channel}"

DEFAULT_STATUSES = [
    {"key": "noted", "label": "noted", "emoji": None,
     "description": DEFAULT_OPTION_TEXT, "menu": True},
    {"key": "processing", "label": "processing", "emoji": None,
     "description": DEFAULT_OPTION_TEXT, "menu": True},
    {"key": "done", "label": "done", "emoji": None,
     "description": DEFAULT_OPTION_TEXT, "menu": True},
    {"key": "cancelled", "label": "cancelled", "emoji": None,
     "description": DEFAULT_OPTION_TEXT, "menu": True},
]

FIELDS = ("user", "ticket", "quantity", "item", "price", "payment",
          "status", "handler", "date", "time", "queued")

ALIASES = {
    "user": "user", "customer": "user", "buyer": "user", "orderer": "user",
    "ticket": "ticket", "users ticket": "ticket", "user ticket": "ticket",
    "ticket id": "ticket", "ticket number": "ticket", "order number": "ticket",
    "quantity": "quantity", "qty": "quantity", "amount": "quantity",
    "item": "item", "order": "item", "product": "item",
    "price": "price", "cost": "price", "total": "price",
    "payment": "payment", "payment method": "payment", "method": "payment",
    "pay": "payment", "mop": "payment",
    "status": "status", "order status": "status",
    "handler": "handler", "claimed by": "handler", "staff": "handler",
    "whoever claimed the ticket": "handler", "ticket handled by": "handler",
    "handled by": "handler",
    "date": "date", "day": "date",
    "time": "time",
    "queued": "queued", "queued at": "queued", "when": "queued",
}

SAMPLE = {
    "user": "@customer",
    "ticket": "ticket-0001",
    "quantity": "1",
    "item": "the item",
    "price": "the price",
    "payment": "the method",
    "status": "noted",
    "handler": "@you",
    "date": "the date",
    "time": "the time",
    "queued": "just now",
}

def save_config():
    _config_store.save(config)


STALE_MARKERS = (":03dc_cake:", ":shortcake1:", ":strawberri:", ":IceCreamSundae:", ":dndexl:", ":MS_coffee1:")


def _migrate_templates():
    changed = False
    for _s in config.values():
        if isinstance(_s, dict) and isinstance(_s.get("template"), str):
            if any(_m in _s["template"] for _m in STALE_MARKERS):
                _s["template"] = DEFAULT_TEMPLATE
                changed = True
    if changed:
        save_config()


_migrate_templates()

def save_orders():
    _order_store.save(orders)

def get_config(guild_id):
    return config.get(str(guild_id))

def ensure_config(guild_id):
    key = str(guild_id)
    if key not in config:
        config[key] = {
            "channel_id": None,
            "template": DEFAULT_TEMPLATE,
            "notify": DEFAULT_NOTIFY,
            "ping": True,
            "placeholder": DEFAULT_PLACEHOLDER,
            "statuses": [dict(s) for s in DEFAULT_STATUSES],
        }

    settings = config[key]
    settings.setdefault("template", DEFAULT_TEMPLATE)
    settings.setdefault("notify", DEFAULT_NOTIFY)
    settings.setdefault("ping", True)
    settings.setdefault("placeholder", DEFAULT_PLACEHOLDER)
    settings.setdefault("channel_id", None)
    if not settings.get("statuses"):
        settings["statuses"] = [dict(s) for s in DEFAULT_STATUSES]
    return settings

def settings_for(guild_id):
    return get_config(guild_id) or {
        "channel_id": None,
        "template": DEFAULT_TEMPLATE,
        "notify": DEFAULT_NOTIFY,
        "ping": True,
        "placeholder": DEFAULT_PLACEHOLDER,
        "statuses": [dict(s) for s in DEFAULT_STATUSES],
    }

def statuses_of(settings):
    return settings.get("statuses") or DEFAULT_STATUSES

def find_status(settings, key):
    for entry in statuses_of(settings):
        if entry["key"] == key:
            return entry
    return None

def status_by_name(settings, text):
    text = (text or "").strip().lower()
    if not text:
        return None
    for entry in statuses_of(settings):
        if entry["key"] == text or entry["label"].strip().lower() == text:
            return entry
    return None

def initial_status(settings):
    return statuses_of(settings)[0]["key"]

def status_label(settings, key):
    entry = find_status(settings, key)
    return entry["label"] if entry else key

def in_menu(entry):
    return entry.get("menu", True)

def menu_statuses(settings):
    return [s for s in statuses_of(settings) if in_menu(s)][:MENU_LIMIT]

def slug(text, taken):
    base = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    base = base[:20] or uuid.uuid4().hex[:6]
    candidate, n = base, 2
    while candidate in taken:
        candidate = f"{base}-{n}"
        n += 1
    return candidate

def render(template, values, guild):
    return templating.render(template, values, ALIASES, guild)

def unknown_placeholders(template):
    return templating.unknown(template, ALIASES)

NOTIFY_ALIASES = dict(ALIASES)
NOTIFY_ALIASES.update(
    {"channel": "channel", "queue": "channel",
     "link": "link", "post": "link", "jump": "link"}
)

def render_notify(template, values, guild):
    return templating.render(template, values, NOTIFY_ALIASES, guild)

def valid_link(url):
    return bool(url) and url.strip().lower().startswith(
        ("http://", "https://", "discord://")
    )

def add_link(view, label, emoji, url):
    if not url:
        return
    label = (label or "").strip() or None
    partial = emojiutils.to_partial(emoji)
    if label is None and partial is None:
        return
    view.add_item(discord.ui.Button(url=url, label=label, emoji=partial))

def notify_view(settings, jump_url):
    view = discord.ui.View(timeout=None)
    add_link(view, settings.get("notify_jump_label", "view order"),
             settings.get("notify_jump_emoji"), jump_url)
    add_link(view, settings.get("notify_link_label"),
             settings.get("notify_link_emoji"), settings.get("notify_link_url"))
    return view if view.children else None

def stamp_values(guild, order):
    stamp = int(order.get("created_at") or 0)

    if not stamp:
        legacy = order.get("date", "")
        return {"date": legacy, "time": "", "queued": legacy}

    moment = datetime.datetime.fromtimestamp(stamp, tz_for(guild.id))
    return {
        "date": moment.strftime("%B %d, %Y").lower(),
        "time": moment.strftime("%I:%M %p").lstrip("0").lower(),
        "queued": f"<t:{stamp}:R>",
    }

def order_values(guild, settings, order):
    values = {
        "user": f"<@{order['user_id']}>",
        "ticket": order["ticket"],
        "quantity": order["quantity"],
        "item": order["item"],
        "price": order["price"],
        "payment": order["payment"],
        "status": status_label(settings, order["status"]),
        "handler": f"<@{order['handler_id']}>",
    }
    values.update(stamp_values(guild, order))
    return values

IMAGE_URL = re.compile(
    r"(?<![(\[<])\bhttps?://[^\s<>()\[\]]+?"
    r"\.(?:png|jpe?g|gif|webp|avif)"
    r"(?:\?[^\s<>()\[\]]*)?",
    re.IGNORECASE,
)

def split_image(body):
    matches = list(IMAGE_URL.finditer(body))
    if not matches:
        return body, None

    last = matches[-1]
    trimmed = body[: last.start()] + body[last.end() :]
    return trimmed.strip(), last.group(0)

def order_embed(guild, settings, order):
    body = render(settings["template"], order_values(guild, settings, order), guild)
    body, image_url = split_image(body)
    embed = embeds.build(body[:4096] or None)

    if image_url:
        embed.set_image(url=image_url)

    if order.get("updated_by"):
        member = guild.get_member(order["updated_by"]) if guild else None
        if member:
            embed.set_footer(text=f"last updated by {member.display_name}")
    return embed

def order_text(guild, settings, order):
    body = render(settings["template"], order_values(guild, settings, order), guild)
    if order.get("updated_by"):
        member = guild.get_member(order["updated_by"]) if guild else None
        if member:
            body = f"{body}\n-# last updated by {member.display_name}"
    body = body.strip()
    return body[:2000] if body else "\u200b"

def can_update(member, order):
    return (
        member.id == order["handler_id"]
        or member.guild_permissions.manage_messages
    )

def channel_name_for(status_text, opener):
    raw = f"{status_text}-{opener}"
    name = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    return name[:100] or "ticket"

async def rename_source(guild, settings, order):
    channel_id = order.get("source_channel_id")
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        return
    name = channel_name_for(
        status_label(settings, order["status"]),
        order.get("opener_name") or "user",
    )
    if channel.name == name:
        return
    try:
        await channel.edit(name=name, reason="queue status")
    except (discord.Forbidden, discord.HTTPException):
        pass

_rename_tasks = set()

def schedule_rename(guild, settings, order):
    task = asyncio.create_task(rename_source(guild, settings, order))
    _rename_tasks.add(task)
    task.add_done_callback(_rename_tasks.discard)

def completed_view(settings):
    view = discord.ui.View(timeout=None)
    add_link(view, settings.get("vouch_label", "vouch"),
             settings.get("vouch_emoji"), settings.get("vouch_url"))
    return view if view.children else None

async def send_completed(guild, settings, order):
    if order.get("completed_sent"):
        return
    if order.get("status") != (settings.get("completed_status") or "done"):
        return
    channel_id = order.get("source_channel_id")
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        return
    text = render_notify(
        settings.get("completed", ""), order_values(guild, settings, order), guild
    ).strip()
    if not text:
        return
    try:
        await channel.send(
            content=text[:2000],
            view=completed_view(settings),
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=settings.get("ping", True)
            ),
        )
        order["completed_sent"] = True
        save_orders()
    except discord.HTTPException:
        pass

class StatusSelect(discord.ui.Select):

    def __init__(self, settings, current=None):
        entries = menu_statuses(settings)

        options = [
            discord.SelectOption(
                label=entry["label"][:100],
                value=entry["key"],
                emoji=emojiutils.to_partial(entry.get("emoji")),
                description=(
                    entry.get("description") or DEFAULT_OPTION_TEXT
                )[:100],
                default=(entry["key"] == current),
            )
            for entry in entries
        ]

        super().__init__(
            placeholder=settings.get("placeholder") or DEFAULT_PLACEHOLDER,
            custom_id="queue:status",
            min_values=1,
            max_values=1,
            options=options or [discord.SelectOption(label="none", value="none")],
            disabled=not options,
        )

    async def callback(self, interaction):
        settings = settings_for(interaction.guild.id)
        order = orders.get(interaction.message.id)

        if order is None:
            await interaction.response.send_message(
                embed=embeds.error("i no longer have this order on record."),
                ephemeral=True,
            )
            return

        if not can_update(interaction.user, order):
            await interaction.response.send_message(
                embed=embeds.error(
                    "only the handler or someone with manage messages can "
                    "change this.",
                    title="Not allowed",
                ),
                ephemeral=True,
            )
            return

        chosen = self.values[0]
        if find_status(settings, chosen) is None:
            await interaction.response.send_message(
                embed=embeds.error("that status no longer exists."),
                ephemeral=True,
            )
            return

        order["status"] = chosen
        order["updated_by"] = interaction.user.id
        save_orders()

        await interaction.response.edit_message(
            content=order_text(interaction.guild, settings, order),
            embed=None,
            view=QueueView(settings, chosen),
        )

        await send_completed(interaction.guild, settings, order)
        schedule_rename(interaction.guild, settings, order)

class QueueView(discord.ui.View):

    def __init__(self, settings, current=None):
        super().__init__(timeout=None)
        self.add_item(StatusSelect(settings, current))

class TemplateModal(discord.ui.Modal, title="Queue Format"):
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

        unknown = unknown_placeholders(text)
        missing = emojiutils.unresolved_names(text, interaction.guild)
        notes = []

        if unknown:
            listed = ", ".join(f"`{{{u}}}`" for u in unknown)
            notes.append(
                f"{listed} is not a field i know, so it will print as "
                "written. the fields are "
                + ", ".join(f"`{{{f}}}`" for f in FIELDS)
                + "."
            )

        if missing:
            listed = ", ".join(f"`:{m}:`" for m in missing)
            notes.append(
                f"{listed} does not match an emoji in this server, so it "
                "will print as text. type a backslash before the emoji and "
                "paste what discord gives you instead."
            )

        _, banner = split_image(text)
        if banner and "discordapp.com" in banner and "ex=" in banner:
            notes.append(
                "that image link is a discord attachment link, which stops "
                "working after about a day. upload the image somewhere that "
                "keeps it, or post it in a channel nobody deletes and use "
                "that link instead."
            )

        if notes:
            await interaction.followup.send(
                embed=embeds.error(
                    f"saved. {len(notes)} things to check:\n\n"
                    + "\n\n".join(notes)
                    if len(notes) > 1
                    else "saved, but " + notes[0],
                    title="Check the format",
                ),
                ephemeral=True,
            )

class NotifyModal(discord.ui.Modal, title="Queued Message"):
    def __init__(self, builder):
        super().__init__()
        self.builder = builder
        self.f_text = discord.ui.TextInput(
            label="Posted when an order is queued",
            default=(builder.settings.get("notify") or "")[:2000],
            placeholder="blank turns it off. {channel} {link} and :emoji: work",
            style=discord.TextStyle.paragraph,
            max_length=2000,
            required=False,
        )
        self.add_item(self.f_text)

    async def on_submit(self, interaction):
        await interaction.response.defer()
        self.builder.settings["notify"] = self.f_text.value.strip()
        save_config()
        await self.builder.refresh()

class NotifyButtonsModal(discord.ui.Modal, title="Queued Message Buttons"):
    def __init__(self, builder):
        super().__init__()
        self.builder = builder
        s = builder.settings
        self.f_jump_label = discord.ui.TextInput(
            label="Jump button text",
            default=s.get("notify_jump_label", "view order"),
            placeholder="links to the queue post. blank hides it",
            max_length=80,
            required=False,
        )
        self.f_jump_emoji = discord.ui.TextInput(
            label="Jump button icon",
            default=s.get("notify_jump_emoji") or "",
            placeholder="an emoji, or blank",
            max_length=64,
            required=False,
        )
        self.f_link_label = discord.ui.TextInput(
            label="Extra button text",
            default=s.get("notify_link_label") or "",
            placeholder="e.g. vouch",
            max_length=80,
            required=False,
        )
        self.f_link_url = discord.ui.TextInput(
            label="Extra button link",
            default=s.get("notify_link_url") or "",
            placeholder="https://... blank hides it",
            max_length=400,
            required=False,
        )
        self.f_link_emoji = discord.ui.TextInput(
            label="Extra button icon",
            default=s.get("notify_link_emoji") or "",
            placeholder="an emoji, or blank",
            max_length=64,
            required=False,
        )
        for item in (
            self.f_jump_label,
            self.f_jump_emoji,
            self.f_link_label,
            self.f_link_url,
            self.f_link_emoji,
        ):
            self.add_item(item)

    async def on_submit(self, interaction):
        jump_emoji, p1 = emojiutils.parse(self.f_jump_emoji.value, interaction.guild)
        if p1:
            await interaction.response.send_message(
                embed=embeds.error(p1, title="Bad icon"), ephemeral=True
            )
            return
        link_emoji, p2 = emojiutils.parse(self.f_link_emoji.value, interaction.guild)
        if p2:
            await interaction.response.send_message(
                embed=embeds.error(p2, title="Bad icon"), ephemeral=True
            )
            return

        link_url = self.f_link_url.value.strip()
        if link_url and not valid_link(link_url):
            await interaction.response.send_message(
                embed=embeds.error(
                    "the extra button link has to start with http:// or https://."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        s = self.builder.settings
        s["notify_jump_label"] = self.f_jump_label.value.strip()
        s["notify_jump_emoji"] = jump_emoji
        s["notify_link_label"] = self.f_link_label.value.strip()
        s["notify_link_url"] = link_url
        s["notify_link_emoji"] = link_emoji
        save_config()
        await self.builder.refresh()

class CompletedModal(discord.ui.Modal, title="Completed Message"):
    def __init__(self, builder):
        super().__init__()
        self.builder = builder
        s = builder.settings
        self.f_text = discord.ui.TextInput(
            label="Message when the order is completed",
            default=(s.get("completed") or "")[:2000],
            placeholder="blank turns it off. {user} and :emoji: work",
            style=discord.TextStyle.paragraph,
            max_length=2000,
            required=False,
        )
        self.f_status = discord.ui.TextInput(
            label="Which status counts as completed",
            default=s.get("completed_status") or "done",
            placeholder="a status name, e.g. done",
            max_length=80,
            required=False,
        )
        self.f_vouch_label = discord.ui.TextInput(
            label="Vouch button text",
            default=s.get("vouch_label", "vouch"),
            placeholder="blank hides the button",
            max_length=80,
            required=False,
        )
        self.f_vouch_url = discord.ui.TextInput(
            label="Vouch channel link",
            default=s.get("vouch_url") or "",
            placeholder="right-click your vouch channel, copy link",
            max_length=400,
            required=False,
        )
        self.f_vouch_emoji = discord.ui.TextInput(
            label="Vouch button icon",
            default=s.get("vouch_emoji") or "",
            placeholder="an emoji, or blank",
            max_length=64,
            required=False,
        )
        for item in (
            self.f_text,
            self.f_status,
            self.f_vouch_label,
            self.f_vouch_url,
            self.f_vouch_emoji,
        ):
            self.add_item(item)

    async def on_submit(self, interaction):
        vouch_emoji, problem = emojiutils.parse(
            self.f_vouch_emoji.value, interaction.guild
        )
        if problem:
            await interaction.response.send_message(
                embed=embeds.error(problem, title="Bad icon"), ephemeral=True
            )
            return

        vouch_url = self.f_vouch_url.value.strip()
        if vouch_url and not valid_link(vouch_url):
            await interaction.response.send_message(
                embed=embeds.error(
                    "the vouch link has to start with http:// or https://."
                ),
                ephemeral=True,
            )
            return

        status_text = self.f_status.value.strip()
        resolved = status_by_name(self.builder.settings, status_text)
        note = None
        if status_text and resolved is None:
            listed = ", ".join(s["label"] for s in statuses_of(self.builder.settings))
            note = f"no status called `{status_text}`. it has to be one of: {listed}."

        await interaction.response.defer()
        s = self.builder.settings
        s["completed"] = self.f_text.value.strip()
        if resolved is not None:
            s["completed_status"] = resolved["key"]
        s["vouch_label"] = self.f_vouch_label.value.strip()
        s["vouch_url"] = vouch_url
        s["vouch_emoji"] = vouch_emoji
        save_config()
        await self.builder.refresh()

        if note:
            await interaction.followup.send(
                embed=embeds.error(note, title="Check the status"), ephemeral=True
            )

class StatusModal(discord.ui.Modal, title="Status"):
    def __init__(self, builder, existing=None):
        super().__init__()
        self.builder = builder
        self.existing = existing

        self.f_label = discord.ui.TextInput(
            label="Label",
            default=(existing or {}).get("label", ""),
            placeholder="shown on the button and in the post",
            max_length=80,
            required=True,
        )
        self.f_emoji = discord.ui.TextInput(
            label="Icon",
            default=(existing or {}).get("emoji") or "",
            placeholder="An emoji, or blank for none",
            required=False,
            max_length=64,
        )
        self.f_description = discord.ui.TextInput(
            label="Menu description",
            default=(existing or {}).get("description") or DEFAULT_OPTION_TEXT,
            placeholder="the small grey line under the label",
            required=False,
            max_length=100,
        )
        self.add_item(self.f_label)
        self.add_item(self.f_emoji)
        self.add_item(self.f_description)

    async def on_submit(self, interaction):
        emoji_value, problem = emojiutils.parse(
            self.f_emoji.value, interaction.guild
        )
        if problem:
            await interaction.response.send_message(
                embed=embeds.error(problem, title="Bad icon"), ephemeral=True
            )
            return

        await interaction.response.defer()
        settings = self.builder.settings

        if self.existing is None:
            taken = {s["key"] for s in statuses_of(settings)}
            settings["statuses"].append(
                {
                    "key": slug(self.f_label.value, taken),
                    "label": self.f_label.value.strip(),
                    "emoji": emoji_value,
                    "description": self.f_description.value.strip()
                    or DEFAULT_OPTION_TEXT,
                    "menu": True,
                }
            )
        else:
            self.existing["label"] = self.f_label.value.strip()
            self.existing["emoji"] = emoji_value
            self.existing["description"] = (
                self.f_description.value.strip() or DEFAULT_OPTION_TEXT
            )

        save_config()
        await self.builder.refresh()

class StatusManageView(discord.ui.View):
    def __init__(self, builder, entry):
        super().__init__(timeout=300)
        self.builder = builder
        self.entry = entry

    async def interaction_check(self, interaction):
        return interaction.user.id == self.builder.ctx.author.id

    def summary(self):
        settings = self.builder.settings
        first = statuses_of(settings)[0]["key"] == self.entry["key"]

        return embeds.build(
            f"**Icon** - {self.entry.get('emoji') or 'none'}\n"
            f"**Description** - "
            f"{self.entry.get('description') or DEFAULT_OPTION_TEXT}\n"
            f"**In the menu** - {'yes' if in_menu(self.entry) else 'no'}\n"
            f"**Starting status** - {'yes' if first else 'no'}",
            title=f"Status: {self.entry['label']}",
        )

    @discord.ui.button(label="edit", style=discord.ButtonStyle.secondary, row=1)
    async def edit(self, interaction, button):
        await interaction.response.send_modal(
            StatusModal(self.builder, self.entry)
        )

    @discord.ui.button(label="show in menu", style=discord.ButtonStyle.secondary, row=1)
    async def toggle(self, interaction, button):
        wanted = not in_menu(self.entry)

        if not wanted and len(menu_statuses(self.builder.settings)) <= 1:
            await interaction.response.send_message(
                embed=embeds.error("the menu needs at least one status in it."),
                ephemeral=True,
            )
            return

        self.entry["menu"] = wanted
        save_config()
        await interaction.response.edit_message(
            embed=self.summary(), view=self
        )
        await self.builder.refresh()

    @discord.ui.button(label="make it the start", style=discord.ButtonStyle.secondary, row=1)
    async def make_first(self, interaction, button):
        statuses = self.builder.settings["statuses"]
        statuses.remove(self.entry)
        statuses.insert(0, self.entry)
        save_config()
        await interaction.response.edit_message(
            embed=self.summary(), view=self
        )
        await self.builder.refresh()

    @discord.ui.button(label="delete", style=discord.ButtonStyle.secondary, row=2)
    async def delete(self, interaction, button):
        statuses = self.builder.settings["statuses"]

        if len(statuses) <= 1:
            await interaction.response.send_message(
                embed=embeds.error("you need at least one status."),
                ephemeral=True,
            )
            return

        statuses.remove(self.entry)
        save_config()
        await interaction.response.edit_message(
            embed=embeds.notice(f"removed `{self.entry['label']}`."), view=None
        )
        await self.builder.refresh()
        self.stop()

class StatusPickSelect(discord.ui.Select):
    def __init__(self, builder):
        self.builder = builder
        entries = statuses_of(builder.settings)
        super().__init__(
            placeholder="Pick a status to manage",
            options=[
                discord.SelectOption(
                    label=e["label"][:100],
                    value=e["key"],
                    emoji=emojiutils.to_partial(e.get("emoji")),
                    description="in the menu" if in_menu(e) else "hidden",
                )
                for e in entries
            ] or [discord.SelectOption(label="none", value="none")],
            disabled=not entries,
        )

    async def callback(self, interaction):
        entry = find_status(self.builder.settings, self.values[0])
        if entry is None:
            await interaction.response.send_message(
                embed=embeds.error("that status is gone."), ephemeral=True
            )
            return

        manage = StatusManageView(self.builder, entry)
        await interaction.response.edit_message(
            embed=manage.summary(), view=manage
        )

class PlaceholderModal(discord.ui.Modal, title="Menu Text"):
    def __init__(self, builder):
        super().__init__()
        self.builder = builder
        self.f_text = discord.ui.TextInput(
            label="What the closed menu says",
            default=builder.settings.get("placeholder") or DEFAULT_PLACEHOLDER,
            placeholder=DEFAULT_PLACEHOLDER,
            max_length=150,
            required=True,
        )
        self.add_item(self.f_text)

    async def on_submit(self, interaction):
        await interaction.response.defer()
        self.builder.settings["placeholder"] = (
            self.f_text.value.strip() or DEFAULT_PLACEHOLDER
        )
        save_config()
        await self.builder.refresh()

class StatusesView(discord.ui.View):
    def __init__(self, builder):
        super().__init__(timeout=300)
        self.builder = builder
        self.add_item(StatusPickSelect(builder))

    async def interaction_check(self, interaction):
        return interaction.user.id == self.builder.ctx.author.id

    @discord.ui.button(label="add status", style=discord.ButtonStyle.secondary, row=1)
    async def add(self, interaction, button):
        if len(statuses_of(self.builder.settings)) >= MAX_STATUSES:
            await interaction.response.send_message(
                embed=embeds.error(f"you already have {MAX_STATUSES} statuses."),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(StatusModal(self.builder))

    @discord.ui.button(label="menu text", style=discord.ButtonStyle.secondary, row=1)
    async def menu_text(self, interaction, button):
        await interaction.response.send_modal(PlaceholderModal(self.builder))

    @discord.ui.button(label="reset to defaults", style=discord.ButtonStyle.secondary, row=1)
    async def reset(self, interaction, button):
        self.builder.settings["statuses"] = [dict(s) for s in DEFAULT_STATUSES]
        save_config()
        await interaction.response.edit_message(
            embed=embeds.notice("statuses put back to the defaults."), view=None
        )
        await self.builder.refresh()
        self.stop()

class ChannelView(discord.ui.View):
    def __init__(self, builder):
        super().__init__(timeout=300)
        self.builder = builder

    async def interaction_check(self, interaction):
        return interaction.user.id == self.builder.ctx.author.id

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        placeholder="Where every queue post lands",
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
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message(
                embed=embeds.error(
                    "you need manage messages permission.", title="Not allowed"
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
            f"**Pings the customer** - {'yes' if settings['ping'] else 'no'}",
            f"**Starts at** - {statuses_of(settings)[0]['label']}",
            f"**Menu says** - {settings.get('placeholder') or DEFAULT_PLACEHOLDER}",
            f"**Queued msg** - {(settings.get('notify') or 'off')[:80]}",
            f"**Queued buttons** - {(settings.get('notify_jump_label', 'view order') or 'none')}"
            + (f" · {settings.get('notify_link_label') or 'link'}" if settings.get('notify_link_url') else ""),
            f"**Completed msg** - {(settings.get('completed') or 'off')[:60]}"
            + (f" → {settings.get('vouch_label', 'vouch') or 'vouch'}" if settings.get('vouch_url') else ""),
            "",
            "**Statuses**",
        ]

        for entry in statuses_of(settings):
            icon = entry.get("emoji")
            shown = f"{icon} {entry['label']}" if icon else entry["label"]
            kind = "in the menu" if in_menu(entry) else "hidden"
            lines.append(f"- {shown} ({kind})")

        embed = embeds.build("\n".join(lines), title="Queue setup")
        embed.add_field(
            name="Preview",
            value=render(settings["template"], SAMPLE, guild)[:1024],
            inline=False,
        )
        embed.set_footer(
            text="channel · format · statuses · ping — all editable below"
        )
        embed.add_field(
            name="Status menu",
            value="\n".join(
                f"{(e.get('emoji') + ' ') if e.get('emoji') else ''}"
                f"{e['label']} — "
                f"{e.get('description') or DEFAULT_OPTION_TEXT}"
                for e in menu_statuses(settings)
            )[:1024]
            or "nothing in the menu",
            inline=False,
        )
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
            embed=embeds.notice("pick where every queue post should land."),
            view=ChannelView(self),
            ephemeral=True,
        )

    @discord.ui.button(label="format", style=discord.ButtonStyle.secondary, row=0)
    async def format_button(self, interaction, button):
        await interaction.response.send_modal(TemplateModal(self))

    @discord.ui.button(label="statuses", style=discord.ButtonStyle.secondary, row=0)
    async def statuses(self, interaction, button):
        await interaction.response.send_message(
            embed=embeds.notice(
                "add a status, or pick one to change its colour, icon and "
                "whether it gets a button."
            ),
            view=StatusesView(self),
            ephemeral=True,
        )

    @discord.ui.button(label="queued msg", style=discord.ButtonStyle.secondary, row=0)
    async def queued_msg(self, interaction, button):
        await interaction.response.send_modal(NotifyModal(self))

    @discord.ui.button(label="queued buttons", style=discord.ButtonStyle.secondary, row=0)
    async def queued_buttons(self, interaction, button):
        await interaction.response.send_modal(NotifyButtonsModal(self))

    @discord.ui.button(label="completed msg", style=discord.ButtonStyle.secondary, row=1)
    async def completed_msg(self, interaction, button):
        await interaction.response.send_modal(CompletedModal(self))

    @discord.ui.button(label="fields", style=discord.ButtonStyle.secondary, row=1)
    async def fields(self, interaction, button):
        await interaction.response.send_message(
            embed=embeds.notice(
                "drop any of these into the format and the bot fills them "
                "in:\n\n"
                + "\n".join(f"`{{{f}}}`" for f in FIELDS)
                + "\n\ntype `:name:` for a server emoji and it gets resolved "
                "when the post goes out.\n\nthe queued message can also use "
                "`{channel}` and `{link}` for the queue post.",
                title="Format fields",
            ),
            ephemeral=True,
        )

    @discord.ui.button(label="toggle ping", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_ping(self, interaction, button):
        await interaction.response.defer()
        self.settings["ping"] = not self.settings["ping"]
        save_config()
        await self.refresh()

    @discord.ui.button(label="reset format", style=discord.ButtonStyle.secondary, row=1)
    async def reset_format(self, interaction, button):
        await interaction.response.defer()
        self.settings["template"] = DEFAULT_TEMPLATE
        save_config()
        await self.refresh()

class ConfirmView(discord.ui.View):

    def __init__(self, ctx, settings, order):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.settings = settings
        self.order = order
        self.channel_id = settings.get("channel_id")
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            embed=embeds.error("this order isn't yours.", title="Not yours"),
            ephemeral=True,
        )
        return False

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        placeholder="Post somewhere else instead",
        row=0,
    )
    async def override(self, interaction, select):
        await interaction.response.defer()
        self.channel_id = select.values[0].id

    @discord.ui.button(label="post", style=discord.ButtonStyle.secondary, row=1)
    async def post(self, interaction, button):
        if self.channel_id is None:
            await interaction.response.send_message(
                embed=embeds.error(
                    "no queue channel set. pick one above, or run "
                    f"`{display_prefix(self.ctx)}queue setup`."
                ),
                ephemeral=True,
            )
            return

        channel = interaction.guild.get_channel(self.channel_id)
        if channel is None or not channel.permissions_for(
            interaction.guild.me
        ).send_messages:
            await interaction.response.send_message(
                embed=embeds.error("i cannot post in that channel."),
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        ping = self.settings.get("ping", True)

        try:
            body = order_text(interaction.guild, self.settings, self.order)
            mention = f"<@{self.order['user_id']}>"
            if ping and mention not in body:
                body = f"{mention}\n{body}"[:2000]
            sent = await channel.send(
                content=body,
                view=QueueView(self.settings, self.order["status"]),
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, users=ping
                ),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                embed=embeds.error("i cannot post in that channel."),
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            log.exception("queue post rejected in %s", channel.id)
            await interaction.followup.send(
                embed=embeds.error("discord turned that post down. check the log."),
                ephemeral=True,
            )
            return

        orders[sent.id] = self.order
        save_orders()

        schedule_rename(interaction.guild, self.settings, self.order)

        values = order_values(interaction.guild, self.settings, self.order)
        values.update({"channel": channel.mention, "link": sent.jump_url})
        notify = render_notify(
            self.settings.get("notify", DEFAULT_NOTIFY), values, interaction.guild
        ).strip()
        if notify:
            try:
                await self.ctx.channel.send(
                    content=notify[:2000],
                    view=notify_view(self.settings, sent.jump_url),
                    allowed_mentions=discord.AllowedMentions(
                        everyone=False, roles=False, users=ping
                    ),
                )
            except discord.HTTPException:
                pass

        for item in self.children:
            item.disabled = True

        if self.message:
            try:
                await self.message.edit(
                    content=None,
                    embed=embeds.notice(
                        f"posted in {channel.mention}. {sent.jump_url}",
                        title="Order queued",
                    ),
                    view=self,
                )
            except discord.HTTPException:
                pass
        self.stop()

    @discord.ui.button(label="cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(self, interaction, button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=None,
            embed=embeds.notice("dropped this one. nothing was posted."),
            view=self,
        )
        self.stop()

class Queue(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self._views_added = False

    @commands.Cog.listener()
    async def on_ready(self):
        if self._views_added:
            return
        self._views_added = True

        self.bot.add_view(QueueView(settings_for(0)))

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
                    "you need manage messages permission to take orders.",
                    title="Not allowed",
                ),
            )
            return

        log.exception("Unhandled error in %s", ctx.command, exc_info=error)
        await embeds.send(
            ctx, embeds.error("something broke on my end. it has been logged.")
        )

    @commands.hybrid_group(
        name="queue",
        invoke_without_command=True,
        fallback="new",
        description="Take an order and post it to the queue.",
    )
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(
        user="Who the order is for",
        ticket="Their ticket, order number or note",
        quantity="How many",
        item="What they ordered",
        price="What it costs",
        payment="How they are paying",
        handler="Who is handling it. Defaults to you.",
    )
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def queue(
        self,
        ctx,
        user: discord.Member,
        ticket: str,
        quantity: str,
        price: str,
        payment: str,
        handler: discord.Member = None,
        *,
        item: str,
    ):
        settings = settings_for(ctx.guild.id)

        order = {
            "user_id": user.id,
            "ticket": ticket,
            "quantity": quantity,
            "item": item,
            "price": price,
            "payment": payment,
            "handler_id": (handler or ctx.author).id,
            "created_at": int(time.time()),
            "status": initial_status(settings),
            "updated_by": None,
            "source_channel_id": ctx.channel.id,
            "opener_name": user.display_name,
        }

        view = ConfirmView(ctx, settings, order)
        view.message = await ctx.send(
            content=order_text(ctx.guild, settings, order),
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @queue.command(
        name="setup",
        description="Customise where queues drop, the format and the status menu.",
    )
    @commands.has_permissions(manage_messages=True)
    async def queue_setup(self, ctx):
        settings = ensure_config(ctx.guild.id)
        save_config()

        view = SetupView(ctx, settings)
        view.message = await ctx.send(
            embed=view.status_embed(),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @queue.command(
        name="status",
        description="Set an order's status by its message link or ID.",
    )
    @app_commands.describe(
        message="The queue post's message ID or link",
        status="The status to set",
    )
    @commands.has_permissions(manage_messages=True)
    async def queue_set_status(self, ctx, message: str, *, status: str):
        settings = settings_for(ctx.guild.id)
        raw = message.strip().rsplit("/", 1)[-1]

        if not raw.isdigit():
            await embeds.send(
                ctx,
                embeds.error(
                    "give me the queue post's message id or its link.",
                    title="Not found",
                ),
            )
            return

        order = orders.get(int(raw))
        if order is None:
            await embeds.send(
                ctx, embeds.error("that isn't an order i have on record.")
            )
            return

        entry = find_status(settings, status.strip().lower())
        if entry is None:
            listed = ", ".join(s["key"] for s in statuses_of(settings))
            await embeds.send(
                ctx, embeds.error(f"status has to be one of: {listed}.")
            )
            return

        order["status"] = entry["key"]
        order["updated_by"] = ctx.author.id
        save_orders()

        channel = ctx.guild.get_channel(settings.get("channel_id")) or ctx.channel
        try:
            target = await channel.fetch_message(int(raw))
            await target.edit(
                content=order_text(ctx.guild, settings, order),
                embed=None,
                view=QueueView(settings, entry["key"]),
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

        await send_completed(ctx.guild, settings, order)
        schedule_rename(ctx.guild, settings, order)

        await embeds.send(
            ctx,
            embeds.notice(f"order marked {entry['label']}.", title="Order updated"),
        )

    @queue_set_status.autocomplete("status")
    async def status_autocomplete(self, interaction, current):
        settings = settings_for(interaction.guild_id)
        current = current.lower()
        return [
            app_commands.Choice(name=s["label"], value=s["key"])
            for s in statuses_of(settings)
            if current in s["key"].lower()
        ][:25]

async def setup(bot):
    await bot.add_cog(Queue(bot))