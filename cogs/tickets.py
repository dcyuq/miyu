import logging
import re
import time
import uuid
import io
import html
import datetime

import discord
from discord import app_commands
from discord.ext import commands

import embeds
from prefixes import display_prefix
import emojiutils
from storage import Store, IntKeyStore

log = logging.getLogger(__name__)

_config_store = Store("tickets_config.json")
_ticket_store = IntKeyStore("tickets.json")
_log_store = IntKeyStore("ticket_logs.json")

config = _config_store.load()
tickets = _ticket_store.load()
logs = _log_store.load()

CATEGORY_LIMIT = 50
MAX_BUTTONS = 10
MAX_QUESTIONS = 5
MAX_STAFF_ROLES = 10
MAX_OPEN_PER_USER = 5

STYLES = {
    "primary": discord.ButtonStyle.secondary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.secondary,
    "danger": discord.ButtonStyle.secondary,
}

STYLE_ALIASES = {
    "blurple": "primary",
    "grey": "secondary",
    "gray": "secondary",
    "green": "success",
    "red": "danger",
}

STYLE_CHOICES = [
    ("primary", "Blurple", "Discord's brand colour. Good for the main action."),
    ("secondary", "Grey", "Understated. Good for secondary options."),
    ("success", "Green", "Reads as positive or helpful."),
    ("danger", "Red", "Reads as serious. Good for reports or appeals."),
]

def canonical_style(value):
    value = (value or "primary").strip().lower()
    value = STYLE_ALIASES.get(value, value)
    return value if value in STYLES else "primary"

def style_label(value):
    value = canonical_style(value)
    for key, label, _ in STYLE_CHOICES:
        if key == value:
            return label
    return "Blurple"

PANEL_MODES = [
    (
        "embed_title",
        "Embed with header",
        "Full embed with the big title text at the top.",
    ),
    (
        "embed_plain",
        "Embed without header",
        "Same embed, no title. Slimmer.",
    ),
    (
        "text",
        "Plain text",
        "No embed. A normal message with buttons under it.",
    ),
    (
        "bare",
        "Buttons only",
        "No text and no embed. Nothing but the buttons.",
    ),
]

DEFAULT_PANEL = {
    "channel_id": None,
    "message_id": None,
    "mode": "embed_title",
    "layout": "buttons",
    "placeholder": "open a ticket",
    "title": "Support Tickets",
    "description": "Click a button below to open a private ticket.",
    "color": embeds.ACCENT.value,
    "image_url": None,
    "thumbnail_url": None,
}

def panel_mode(panel):
    mode = (panel or {}).get("mode", "embed_title")
    return mode if mode in {m[0] for m in PANEL_MODES} else "embed_title"

def panel_mode_label(panel):
    current = panel_mode(panel)
    for key, label, _ in PANEL_MODES:
        if key == current:
            return label
    return "Embed with header"

DEFAULT_BUTTON = {
    "label": "Create Ticket",
    "style": "primary",
    "emoji": None,
    "category_id": None,
    "welcome": "Describe your issue and someone will be with you shortly.",
    "questions": [],
}

CUSTOM_TOKEN = re.compile(r"<(a?):([A-Za-z0-9_~]{2,32}):(\d{15,25})>")

NAME_TOKEN = re.compile(r"^:?([A-Za-z0-9_~]{2,32}):?$")

KEYCAP_HEADS = "0123456789#*"

EMOJI_BASE = (
    (0x00A9, 0x00A9),
    (0x00AE, 0x00AE),
    (0x203C, 0x2049),
    (0x2122, 0x2122),
    (0x2139, 0x2139),
    (0x2194, 0x21AA),
    (0x231A, 0x231B),
    (0x2328, 0x2328),
    (0x23CF, 0x23FA),
    (0x24C2, 0x24C2),
    (0x25AA, 0x25FE),
    (0x2600, 0x27BF),
    (0x2934, 0x2935),
    (0x2B00, 0x2BFF),
    (0x3030, 0x3030),
    (0x303D, 0x303D),
    (0x3297, 0x3299),
    (0x1F000, 0x1FAFF),
)

EMOJI_PARTS = (
    (0x200D, 0x200D),
    (0x20E3, 0x20E3),
    (0xFE0E, 0xFE0F),
    (0x1F1E6, 0x1F1FF),
    (0x1F3FB, 0x1F3FF),
    (0xE0020, 0xE007F),
)

MAX_ICON_POINTS = 16

def in_ranges(ranges, point):
    return any(low <= point <= high for low, high in ranges)

def is_unicode_emoji(text):
    points = [ord(ch) for ch in text]
    if not points or len(points) > MAX_ICON_POINTS:
        return False

    head = points[0]
    starts_ok = (
        in_ranges(EMOJI_BASE, head)
        or in_ranges(EMOJI_PARTS, head)
        or (chr(head) in KEYCAP_HEADS and 0x20E3 in points)
    )
    if not starts_ok:
        return False

    return all(
        in_ranges(EMOJI_BASE, p)
        or in_ranges(EMOJI_PARTS, p)
        or chr(p) in KEYCAP_HEADS
        for p in points
    )

def first_cluster(text):
    if not text:
        return text

    chars = list(text)
    head = ord(chars[0])

    if in_ranges(((0x1F1E6, 0x1F1FF),), head):
        return "".join(chars[:2])

    out = [chars[0]]
    for ch in chars[1:]:
        attaches = in_ranges(EMOJI_PARTS, ord(ch)) or ord(out[-1]) == 0x200D
        if not attaches:
            break
        out.append(ch)
    return "".join(out)

def find_emoji(name, guild, client):
    found = emojiutils.find_named(guild, name)
    if found is not None:
        return found

    if client is None:
        return None

    base = re.sub(r"~\d+$", "", name).lower()
    for emoji in client.emojis:
        if emoji.name.lower() == base and emoji.is_usable():
            return emoji
    return None

def resolve_icon(raw, guild, client):
    text = (raw or "").strip().replace("\\", "")

    if not text:
        return None, None

    match = CUSTOM_TOKEN.search(text)
    if match:
        emoji_id = int(match.group(3))
        known = (guild.get_emoji(emoji_id) if guild else None) or (
            client.get_emoji(emoji_id) if client else None
        )
        if known is not None:
            return str(known), None

        by_name = find_emoji(match.group(2), guild, client)
        if by_name is not None:
            return str(by_name), None

        return None, (
            "i cannot use that emoji. it has to be from this server, or "
            "another server i am in."
        )

    named = NAME_TOKEN.match(text)
    if named:
        by_name = find_emoji(named.group(1), guild, client)
        if by_name is not None:
            return str(by_name), None
        return None, (
            f"no emoji named `{named.group(1)}` that i can reach. check the "
            "name under server settings, or paste a normal emoji instead."
        )

    squeezed = first_cluster("".join(ch for ch in text if not ch.isspace()))
    if is_unicode_emoji(squeezed):
        return squeezed, None

    return None, (
        "that is not an emoji. paste one, or type a server emoji's name "
        "like `:sparkles:`. leave the field blank for no icon."
    )

def resolve_text(text, guild, client):
    if not text:
        return text

    kept = []

    def stash(match):
        kept.append(match.group(0))
        return f"\x00{len(kept) - 1}\x00"

    text = emojiutils.RESOLVED.sub(stash, text)

    def swap(match):
        found = find_emoji(match.group(1), guild, client)
        return str(found) if found else match.group(0)

    text = emojiutils.SHORTCODE.sub(swap, text)

    return re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], text)

def missing_names(text, guild, client):
    if not text:
        return []

    stripped = emojiutils.RESOLVED.sub("", text)
    dead = [
        name
        for name in emojiutils.SHORTCODE.findall(stripped)
        if find_emoji(name, guild, client) is None
    ]
    return sorted(dict.fromkeys(dead))

def icon_partial(raw):
    text = (raw or "").strip()
    if not text:
        return None

    if CUSTOM_TOKEN.fullmatch(text):
        try:
            return discord.PartialEmoji.from_str(text)
        except (ValueError, TypeError):
            return None

    if is_unicode_emoji(text):
        return discord.PartialEmoji(name=text)

    return None

def icon_text(button_data):
    return button_data.get("emoji") or "none"

def save_config():
    _config_store.save(config)

def save_tickets():
    _ticket_store.save(tickets)

def save_logs():
    _log_store.save(logs)

def get_config(guild_id):
    return config.get(str(guild_id))

def ensure_config(guild_id):
    key = str(guild_id)
    if key not in config:
        config[key] = {
            "staff_role_ids": [],
            "log_channel_id": None,
            "category_id": None,
            "counter": 0,
            "panel": dict(DEFAULT_PANEL),
            "buttons": [],
        }

    settings = config[key]
    settings.setdefault("panel", dict(DEFAULT_PANEL))
    settings.setdefault("buttons", [])
    settings.setdefault("counter", 0)
    settings.setdefault("staff_role_ids", [])
    settings["panel"].setdefault("mode", "embed_title")
    settings["panel"].setdefault("layout", "buttons")
    settings["panel"].setdefault("placeholder", "open a ticket")

    legacy = settings.pop("staff_role_id", None)
    if legacy and legacy not in settings["staff_role_ids"]:
        settings["staff_role_ids"].append(legacy)

    return settings

def staff_role_ids(settings):
    if not settings:
        return []
    ids = settings.get("staff_role_ids")
    if ids:
        return ids
    legacy = settings.get("staff_role_id")
    return [legacy] if legacy else []

def staff_roles(guild, settings):
    found = []
    for role_id in staff_role_ids(settings):
        role = guild.get_role(role_id)
        if role is not None:
            found.append(role)
    return found

def is_configured(settings):
    return bool(
        settings
        and settings.get("category_id")
        and staff_role_ids(settings)
        and settings.get("log_channel_id")
    )

def can_manage(member):
    perms = member.guild_permissions
    return perms.administrator or perms.manage_guild

def is_staff(member, settings):
    if member.guild_permissions.administrator:
        return True
    allowed = set(staff_role_ids(settings))
    return any(r.id in allowed for r in member.roles)

def find_button(settings, key):
    for entry in settings.get("buttons", []):
        if entry["key"] == key:
            return entry
    return None

def open_tickets_for(guild_id, user_id):
    return [
        channel_id
        for channel_id, data in tickets.items()
        if data["guild_id"] == guild_id and data["opener_id"] == user_id
    ]

def duration_text(seconds):
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)

def parse_color(text, fallback=embeds.ACCENT.value):
    if not text:
        return fallback
    text = text.strip().lstrip("#")
    try:
        value = int(text, 16)
    except ValueError:
        return fallback
    return value if 0 <= value <= 0xFFFFFF else fallback

def clean_url(text):
    if not text:
        return None
    text = text.strip()
    if text.lower().startswith(("http://", "https://")):
        return text
    return None

def build_panel_view(guild_id, settings):
    panel = settings["panel"]
    if panel.get("layout") == "dropdown":
        return DropdownPanelView(guild_id, settings["buttons"], panel.get("placeholder"))
    if panel_mode(panel) == "bare":
        return BarePanelView(guild_id, settings["buttons"])
    return PanelView(guild_id, settings["buttons"])

def build_panel_payload(settings):
    panel = settings["panel"]
    mode = panel_mode(panel)

    if mode == "bare":
        return None, None

    if mode == "text":
        return panel["description"][:2000], None

    embed = discord.Embed(
        description=panel["description"][:4096],
        color=panel["color"],
    )
    if mode == "embed_title":
        embed.title = panel["title"][:256]
    if panel.get("image_url"):
        embed.set_image(url=panel["image_url"])
    if panel.get("thumbnail_url"):
        embed.set_thumbnail(url=panel["thumbnail_url"])

    return None, embed

async def send_log(guild, embed):
    settings = get_config(guild.id)
    if not settings:
        return
    channel = guild.get_channel(settings.get("log_channel_id"))
    if channel is None:
        return
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException):
        pass

async def create_ticket(interaction, button_data, answers):
    guild = interaction.guild
    settings = get_config(guild.id)

    owned = open_tickets_for(guild.id, interaction.user.id)
    live = [c for c in owned if guild.get_channel(c) is not None]
    if len(live) != len(owned):
        for stale in owned:
            if stale not in live:
                tickets.pop(stale, None)
        save_tickets()
    if len(live) >= MAX_OPEN_PER_USER:
        await interaction.followup.send(
            embed=embeds.error(f"you already have {MAX_OPEN_PER_USER} open tickets. close one before opening another."),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    category_id = button_data.get("category_id") or settings["category_id"]
    category = guild.get_channel(category_id)
    if not isinstance(category, discord.CategoryChannel):
        await interaction.followup.send(
            embed=embeds.error("the ticket category is missing. ask an admin to run setup again."),
            ephemeral=True,
        )
        return

    if len(category.channels) >= CATEGORY_LIMIT:
        await interaction.followup.send(
            embed=embeds.error("that ticket category is full. ask staff to close some tickets."),
            ephemeral=True,
        )
        return

    roles = staff_roles(guild, settings)

    number = settings.get("counter", 0) + 1
    settings["counter"] = number
    save_config()

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_channels=True,
            read_message_history=True,
        ),
        interaction.user: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            attach_files=True,
            read_message_history=True,
        ),
    }
    for role in roles:
        overwrites[role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        )

    try:
        channel = await guild.create_text_channel(
            name=f"ticket-{number:04d}",
            category=category,
            overwrites=overwrites,
            reason=f"Ticket opened by {interaction.user}",
        )
    except discord.Forbidden:
        await interaction.followup.send(
            embed=embeds.error("i don't have permission to create channels there."), ephemeral=True
        )
        return
    except discord.HTTPException as exc:
        await interaction.followup.send(
            embed=embeds.error("discord turned that request down. check the log."),
            ephemeral=True,
        )
        return

    tickets[channel.id] = {
        "guild_id": guild.id,
        "opener_id": interaction.user.id,
        "number": number,
        "opened_at": time.time(),
        "claimed_by": None,
        "kind": button_data["label"],
        "answers": answers,
    }
    save_tickets()

    welcome = button_data.get("welcome") or DEFAULT_BUTTON["welcome"]
    parts = [f"**Ticket {number:04d} - {button_data['label']}**", "", welcome]
    for question, answer in answers:
        parts.append("")
        parts.append(f"**{question[:256]}**")
        parts.append((answer or "-")[:1024])
    body = "\n".join(parts)

    mentions = " ".join(r.mention for r in roles)
    ping = f"{interaction.user.mention} {mentions}".strip()
    await channel.send(
        view=TicketControlView(ping, body),
        allowed_mentions=discord.AllowedMentions(users=True, roles=roles or False),
    )

    await interaction.followup.send(
        embed=embeds.notice(f"ticket created: {channel.mention}"),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )

    log_embed = discord.Embed(
        title="Ticket Opened",
        timestamp=discord.utils.utcnow(),
    )
    log_embed.add_field(name="Ticket ID", value=str(number), inline=True)
    log_embed.add_field(name="Opened By", value=interaction.user.mention, inline=True)
    log_embed.add_field(name="Type", value=button_data["label"], inline=True)
    log_embed.add_field(name="Channel", value=channel.mention, inline=False)
    for question, answer in answers:
        log_embed.add_field(
            name=question[:256], value=(answer or "-")[:1024], inline=False
        )
    await send_log(guild, log_embed)

def build_close_embed(guild, entry):
    opener = guild.get_member(entry["opener_id"])
    closer = guild.get_member(entry["closer_id"])
    claimer = guild.get_member(entry.get("claimed_by") or 0)
    opener_text = opener.mention if opener else f"<@{entry['opener_id']}>"
    closer_text = closer.mention if closer else f"<@{entry['closer_id']}>"

    lines = [
        f"**ticket #{entry['number']:04d} closed** · {entry.get('kind', 'Ticket')}",
        "",
        f"opened by {opener_text} · closed by {closer_text}",
        f"open for {duration_text(entry['closed_at'] - entry['opened_at'])} · {entry.get('message_count', 0)} messages",
    ]
    if claimer:
        lines.append(f"claimed by {claimer.mention}")
    lines.append(f"opened <t:{int(entry['opened_at'])}:f>")
    lines.append(f"**reason** {entry.get('reason') or 'no reason given'}")

    for question, answer in entry.get("answers", []):
        lines.append(f"**{question[:100]}** {(answer or '-')[:500]}")

    return embeds.build("\n".join(lines)[:4096])

class EditReasonModal(discord.ui.Modal, title="Edit Close Reason"):
    def __init__(self, entry):
        super().__init__()
        self.entry = entry
        self.f_reason = discord.ui.TextInput(
            label="Reason",
            default=entry.get("reason") or "",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=True,
        )
        self.add_item(self.f_reason)

    async def on_submit(self, interaction):
        self.entry["reason"] = self.f_reason.value
        save_logs()

        embed = build_close_embed(interaction.guild, self.entry)
        embed.set_footer(text=f"Reason last edited by {interaction.user.display_name}")
        await interaction.response.edit_message(embed=embed, view=LogControlView())

class LogControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Edit Reason",
        style=discord.ButtonStyle.secondary,
        custom_id="ticket:editreason",
    )
    async def edit_reason(self, interaction, button):
        entry = logs.get(interaction.message.id)
        if entry is None:
            await interaction.response.send_message(
                embed=embeds.error("i no longer have this ticket on record."), ephemeral=True
            )
            return

        settings = get_config(interaction.guild.id)
        if not settings or not is_staff(interaction.user, settings):
            await interaction.response.send_message(
                embed=embeds.error("only staff can edit the close reason."), ephemeral=True
            )
            return

        await interaction.response.send_modal(EditReasonModal(entry))

TRANSCRIPT_LIMIT = 2000

TRANSCRIPT_CSS = """<style>
body{margin:0;background:#1e1e1f;color:#dcdcdc;font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
.head{padding:24px;background:#252527;border-bottom:2px solid #585858}
.head h1{margin:0 0 4px;font-size:20px;color:#fff}
.head .sub{color:#9a9a9a;margin-bottom:14px}
.info{border-collapse:collapse;font-size:13px}
.info td{padding:2px 18px 2px 0;color:#c4c4c4;vertical-align:top}
.info td:first-child{color:#7c7c7c;text-transform:uppercase;font-size:11px;letter-spacing:.05em;white-space:nowrap}
.log{padding:14px 24px 40px}
.msg{padding:9px 0;border-bottom:1px solid #2c2c2e}
.meta{display:flex;gap:10px;align-items:baseline}
.who{font-weight:600;color:#fff}
.when{color:#6f6f6f;font-size:12px}
.body{margin-top:3px;word-wrap:break-word}
.body img{max-width:420px;max-height:320px;border-radius:6px;margin-top:6px;display:block}\n.body img.emoji{width:20px;height:20px;display:inline;vertical-align:-4px;margin:0 1px;border-radius:0}\n.body img.sticker{max-width:160px;max-height:160px}
.body a{color:#8ab4f8}
.empty{color:#7c7c7c;padding:24px}
</style>"""

async def collect_messages(channel):
    collected = []
    try:
        async for message in channel.history(limit=TRANSCRIPT_LIMIT, oldest_first=True):
            collected.append(message)
    except (discord.Forbidden, discord.HTTPException):
        pass
    return collected

def _is_image(attachment):
    if (attachment.content_type or "").startswith("image/"):
        return True
    name = (attachment.filename or "").lower()
    return name.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif"))

def first_image_url(messages):
    for message in messages:
        for attachment in message.attachments:
            if _is_image(attachment):
                return attachment.url
    return None

def transcript_file(document, number):
    return discord.File(io.BytesIO(document.encode("utf-8")), filename=f"transcript-ticket-{number:04d}.html")

TX_EMOJI = re.compile(r"<(a)?:([A-Za-z0-9_]{2,32}):(\d{15,25})>")


def _render_content(text):
    stash = []

    def hold(match):
        ext = "gif" if match.group(1) else "png"
        stash.append(
            f'<img class="emoji" src="https://cdn.discordapp.com/emojis/{match.group(3)}.{ext}" alt=":{match.group(2)}:" title=":{match.group(2)}:">'
        )
        return f"\x00{len(stash) - 1}\x00"

    held = TX_EMOJI.sub(hold, text or "")
    out = html.escape(held).replace("\n", "<br>")
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], out)


def build_transcript_html(guild, entry, messages):
    opener = guild.get_member(entry["opener_id"])
    closer = guild.get_member(entry["closer_id"])
    opener_name = opener.display_name if opener else str(entry["opener_id"])
    closer_name = closer.display_name if closer else str(entry["closer_id"])
    opened = datetime.datetime.fromtimestamp(entry["opened_at"], datetime.timezone.utc)
    closed = datetime.datetime.fromtimestamp(entry["closed_at"], datetime.timezone.utc)

    rows = []
    for message in messages:
        stamp = message.created_at.strftime("%Y-%m-%d %H:%M UTC")
        author = html.escape(message.author.display_name)
        body = _render_content(message.content)
        media = ""
        for attachment in message.attachments:
            if _is_image(attachment):
                media += f'<div><img src="{html.escape(attachment.url)}" alt=""></div>'
            else:
                media += f'<div><a href="{html.escape(attachment.url)}">{html.escape(attachment.filename)}</a></div>'
        for sticker in getattr(message, "stickers", []):
            url = getattr(sticker, "url", None)
            name = html.escape(getattr(sticker, "name", "sticker"))
            if url:
                media += f'<div><img class="sticker" src="{html.escape(str(url))}" alt=":{name}:" title=":{name}:"></div>'
            else:
                media += f"<div>:{name}:</div>"
        if not body and not media:
            continue
        rows.append(
            f'<div class="msg"><div class="meta"><span class="who">{author}</span>'
            f'<span class="when">{stamp}</span></div>'
            f'<div class="body">{body}{media}</div></div>'
        )

    joined = "\n".join(rows) or '<div class="empty">no messages were recorded.</div>'
    reason = html.escape(entry.get("reason") or "no reason given")
    number = f"{entry['number']:04d}"

    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>ticket {number} transcript</title>"
        + TRANSCRIPT_CSS
        + "</head><body><div class='head'>"
        f"<h1>ticket {number}</h1>"
        f"<div class='sub'>{html.escape(entry.get('kind', 'Ticket'))}</div>"
        "<table class='info'>"
        f"<tr><td>opened by</td><td>{html.escape(opener_name)}</td></tr>"
        f"<tr><td>closed by</td><td>{html.escape(closer_name)}</td></tr>"
        f"<tr><td>opened</td><td>{opened.strftime('%Y-%m-%d %H:%M UTC')}</td></tr>"
        f"<tr><td>closed</td><td>{closed.strftime('%Y-%m-%d %H:%M UTC')}</td></tr>"
        f"<tr><td>messages</td><td>{entry.get('message_count', len(messages))}</td></tr>"
        f"<tr><td>reason</td><td>{reason}</td></tr>"
        "</table></div>"
        f"<div class='log'>{joined}</div>"
        "</body></html>"
    )

def build_user_close_card(guild, entry):
    closer = guild.get_member(entry["closer_id"])
    closer_text = closer.mention if closer else f"<@{entry['closer_id']}>"
    description = (
        "**your ticket has been closed**\n"
        "thank you for stopping by. do come again soon.\n\n"
        f"closed by : {closer_text}\n"
        f"total msg : {entry.get('message_count', 0)}\n"
        f"closed on : <t:{int(entry['closed_at'])}:R>"
    )
    return embeds.build(description)

class TranscriptLink(discord.ui.View):
    def __init__(self, url):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="view transcript", url=url))

async def dm_transcript(client, guild, entry, document, transcript_url):
    opener = guild.get_member(entry["opener_id"])
    if opener is None:
        try:
            opener = await client.fetch_user(entry["opener_id"])
        except discord.HTTPException:
            return

    card = build_user_close_card(guild, entry)
    try:
        if transcript_url:
            await opener.send(embed=card, view=TranscriptLink(transcript_url))
        else:
            await opener.send(embed=card, file=transcript_file(document, entry["number"]))
    except (discord.Forbidden, discord.HTTPException):
        pass

class CloseReasonModal(discord.ui.Modal, title="Close Ticket"):
    def __init__(self, data, channel):
        super().__init__()
        self.data = data
        self.channel = channel
        self.f_reason = discord.ui.TextInput(
            label="Reason for closing",
            placeholder="Shown to staff in the log",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=True,
        )
        self.add_item(self.f_reason)

    async def on_submit(self, interaction):
        await interaction.response.send_message(
            embed=embeds.notice("closing this ticket. a transcript is on its way."),
            ephemeral=True,
        )

        messages = await collect_messages(self.channel)

        entry = {
            "guild_id": self.data["guild_id"],
            "number": self.data["number"],
            "opener_id": self.data["opener_id"],
            "closer_id": interaction.user.id,
            "claimed_by": self.data.get("claimed_by"),
            "opened_at": self.data["opened_at"],
            "closed_at": time.time(),
            "kind": self.data.get("kind", "Ticket"),
            "answers": self.data.get("answers", []),
            "reason": self.f_reason.value,
            "message_count": len(messages),
        }

        document = build_transcript_html(interaction.guild, entry, messages)
        sample_url = first_image_url(messages)
        transcript_url = None

        settings = get_config(interaction.guild.id)
        log_channel = interaction.guild.get_channel(settings.get("log_channel_id"))

        if log_channel is not None:
            try:
                embed = build_close_embed(interaction.guild, entry)
                if sample_url:
                    embed.set_image(url=sample_url)
                sent = await log_channel.send(
                    embed=embed,
                    file=transcript_file(document, entry["number"]),
                    view=LogControlView(),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                logs[sent.id] = entry
                save_logs()
                if sent.attachments:
                    transcript_url = sent.attachments[0].url
                    linked = LogControlView()
                    linked.add_item(discord.ui.Button(label="download transcript", url=transcript_url))
                    try:
                        await sent.edit(view=linked)
                    except discord.HTTPException:
                        pass
            except (discord.Forbidden, discord.HTTPException):
                pass

        await dm_transcript(
            interaction.client, interaction.guild, entry, document, transcript_url
        )

        tickets.pop(self.channel.id, None)
        save_tickets()

        try:
            await self.channel.delete(reason=f"Ticket closed by {interaction.user}")
        except (discord.Forbidden, discord.HTTPException):
            pass

class TicketQuestionModal(discord.ui.Modal):
    def __init__(self, button_data):
        super().__init__(title=button_data["label"][:45])
        self.button_data = button_data
        self.inputs = []

        for question in button_data["questions"][:MAX_QUESTIONS]:
            field = discord.ui.TextInput(
                label=question["label"][:45],
                style=discord.TextStyle.paragraph,
                required=True,
                max_length=1000,
            )
            self.inputs.append((question["label"], field))
            self.add_item(field)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        answers = [(label, field.value) for label, field in self.inputs]
        await create_ticket(interaction, self.button_data, answers)

class TicketOpenButton(discord.ui.Button):
    def __init__(self, guild_id, button_data):
        super().__init__(
            label=button_data["label"][:80],
            emoji=icon_partial(button_data.get("emoji")),
            style=STYLES[canonical_style(button_data.get("style"))],
            custom_id=f"ticket:open:{guild_id}:{button_data['key']}",
        )
        self.button_key = button_data["key"]

    async def callback(self, interaction):
        settings = get_config(interaction.guild.id)
        if not is_configured(settings):
            await interaction.response.send_message(
                embed=embeds.error("the ticket system isn't finished being set up."), ephemeral=True
            )
            return

        button_data = find_button(settings, self.button_key)
        if button_data is None:
            await interaction.response.send_message(
                embed=embeds.error("this button is no longer configured."), ephemeral=True
            )
            return

        if button_data.get("questions"):
            await interaction.response.send_modal(TicketQuestionModal(button_data))
            return

        await interaction.response.defer(ephemeral=True)
        await create_ticket(interaction, button_data, [])

class PanelView(discord.ui.View):
    def __init__(self, guild_id, buttons):
        super().__init__(timeout=None)
        for button_data in buttons[:MAX_BUTTONS]:
            self.add_item(TicketOpenButton(guild_id, button_data))

class BarePanelView(discord.ui.LayoutView):

    def __init__(self, guild_id, buttons):
        super().__init__(timeout=None)
        row = discord.ui.ActionRow()
        for button_data in buttons[:5]:
            row.add_item(TicketOpenButton(guild_id, button_data))
        self.add_item(row)

class TicketSelect(discord.ui.Select):
    def __init__(self, guild_id, buttons, placeholder):
        options = [
            discord.SelectOption(
                label=button_data["label"][:100],
                value=button_data["key"],
                emoji=icon_partial(button_data.get("emoji")),
            )
            for button_data in buttons[:25]
        ]
        super().__init__(
            placeholder=(placeholder or "open a ticket")[:150],
            custom_id=f"ticket:select:{guild_id}",
            min_values=1,
            max_values=1,
            options=options or [discord.SelectOption(label="none", value="none")],
            disabled=not options,
        )

    async def callback(self, interaction):
        settings = get_config(interaction.guild.id)
        if not is_configured(settings):
            await interaction.response.send_message(
                embed=embeds.error("the ticket system isn't finished being set up."), ephemeral=True
            )
            return

        button_data = find_button(settings, self.values[0])
        if button_data is None:
            await interaction.response.send_message(
                embed=embeds.error("that option is no longer configured."), ephemeral=True
            )
            return

        if button_data.get("questions"):
            await interaction.response.send_modal(TicketQuestionModal(button_data))
            return

        await interaction.response.defer(ephemeral=True)
        await create_ticket(interaction, button_data, [])


class DropdownPanelView(discord.ui.View):
    def __init__(self, guild_id, buttons, placeholder):
        super().__init__(timeout=None)
        self.add_item(TicketSelect(guild_id, buttons, placeholder))


class TicketControls(discord.ui.ActionRow):
    @discord.ui.button(
        label="Claim", style=discord.ButtonStyle.secondary, custom_id="ticket:claim"
    )
    async def claim(self, interaction, button):
        data = tickets.get(interaction.channel.id)
        if data is None:
            await interaction.response.send_message(
                embed=embeds.error("this isn't a tracked ticket."), ephemeral=True
            )
            return

        settings = get_config(interaction.guild.id)
        if not settings or not is_staff(interaction.user, settings):
            await interaction.response.send_message(
                embed=embeds.error("only staff can claim tickets."), ephemeral=True
            )
            return

        if data.get("claimed_by"):
            claimer = interaction.guild.get_member(data["claimed_by"])
            await interaction.response.send_message(
                embed=embeds.error(f"already claimed by {claimer.mention if claimer else 'someone'}."),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        data["claimed_by"] = interaction.user.id
        save_tickets()

        await interaction.response.send_message(
            embed=embeds.notice(f"{interaction.user.mention} claimed this ticket."),
            allowed_mentions=discord.AllowedMentions.none(),
        )

        embed = discord.Embed(
            title="Ticket Claimed",
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Ticket ID", value=str(data["number"]), inline=True)
        embed.add_field(name="Claimed By", value=interaction.user.mention, inline=True)
        embed.add_field(name="Channel", value=interaction.channel.mention, inline=True)
        await send_log(interaction.guild, embed)

    @discord.ui.button(
        label="Close", style=discord.ButtonStyle.secondary, custom_id="ticket:close"
    )
    async def close(self, interaction, button):
        data = tickets.get(interaction.channel.id)
        if data is None:
            await interaction.response.send_message(
                embed=embeds.error("this isn't a tracked ticket."), ephemeral=True
            )
            return

        settings = get_config(interaction.guild.id)
        if not settings:
            await interaction.response.send_message(
                embed=embeds.error("ticket system is not configured."), ephemeral=True
            )
            return

        if (
            not is_staff(interaction.user, settings)
            and interaction.user.id != data["opener_id"]
        ):
            await interaction.response.send_message(
                embed=embeds.error("only staff or the ticket opener can close this."), ephemeral=True
            )
            return

        await interaction.response.send_modal(
            CloseReasonModal(data, interaction.channel)
        )

class TicketControlView(discord.ui.LayoutView):
    def __init__(self, ping=None, body=None):
        super().__init__(timeout=None)
        if ping:
            self.add_item(discord.ui.TextDisplay(ping))
        if body:
            self.add_item(discord.ui.Container(discord.ui.TextDisplay(body)))
        self.add_item(TicketControls())

class EmbedEditModal(discord.ui.Modal, title="Panel Appearance"):
    def __init__(self, settings, builder):
        super().__init__()
        self.settings = settings
        self.builder = builder
        panel = settings["panel"]

        self.f_title = discord.ui.TextInput(
            label="Title", default=panel["title"], max_length=256, required=True
        )
        self.f_desc = discord.ui.TextInput(
            label="Description",
            default=panel["description"],
            style=discord.TextStyle.paragraph,
            max_length=4000,
            required=True,
        )
        self.f_color = discord.ui.TextInput(
            label="Colour hex",
            default=f"{panel['color']:06X}",
            placeholder="5865F2",
            max_length=7,
            required=False,
        )
        self.f_image = discord.ui.TextInput(
            label="Large image URL",
            default=panel.get("image_url") or "",
            placeholder="https://...",
            required=False,
        )
        self.f_thumb = discord.ui.TextInput(
            label="Thumbnail URL",
            default=panel.get("thumbnail_url") or "",
            placeholder="https://...",
            required=False,
        )

        for item in (
            self.f_title,
            self.f_desc,
            self.f_color,
            self.f_image,
            self.f_thumb,
        ):
            self.add_item(item)

    async def on_submit(self, interaction):
        await interaction.response.defer()

        guild, client = interaction.guild, interaction.client

        panel = self.settings["panel"]
        panel["title"] = resolve_text(self.f_title.value, guild, client)
        panel["description"] = resolve_text(self.f_desc.value, guild, client)
        panel["color"] = parse_color(self.f_color.value, panel["color"])
        panel["image_url"] = clean_url(self.f_image.value)
        panel["thumbnail_url"] = clean_url(self.f_thumb.value)
        save_config()

        await self.builder.refresh()

        dead = missing_names(
            f"{self.f_title.value}\n{self.f_desc.value}", guild, client
        )
        if dead:
            listed = ", ".join(f"`:{d}:`" for d in dead)
            await interaction.followup.send(
                embed=embeds.error(
                    f"saved, but {listed} does not match an emoji i can "
                    "reach, so it will print as text. check the name under "
                    "server settings, or paste the emoji itself instead.",
                    title="Check the format",
                ),
                ephemeral=True,
            )

class ButtonEditModal(discord.ui.Modal, title="Ticket Button"):
    def __init__(self, settings, builder, existing=None):
        super().__init__()
        self.settings = settings
        self.builder = builder
        self.existing = existing
        base = existing or DEFAULT_BUTTON

        self.f_label = discord.ui.TextInput(
            label="Button label", default=base["label"], max_length=80, required=True
        )
        self.f_emoji = discord.ui.TextInput(
            label="Icon",
            default=base.get("emoji") or "",
            placeholder="😀 or :servername: - blank for none",
            required=False,
            max_length=100,
        )
        self.f_welcome = discord.ui.TextInput(
            label="Opening message",
            default=base.get("welcome", ""),
            style=discord.TextStyle.paragraph,
            max_length=2000,
            required=False,
        )
        self.f_category = discord.ui.TextInput(
            label="Category ID override",
            default=str(base.get("category_id") or ""),
            placeholder="Blank uses the default category",
            required=False,
            max_length=25,
        )

        for item in (self.f_label, self.f_emoji, self.f_welcome, self.f_category):
            self.add_item(item)

    async def on_submit(self, interaction):
        category_id = None
        raw = self.f_category.value.strip()
        if raw:
            if not raw.isdigit():
                await interaction.response.send_message(
                    embed=embeds.error("category ID must be numbers only."), ephemeral=True
                )
                return
            candidate = interaction.guild.get_channel(int(raw))
            if not isinstance(candidate, discord.CategoryChannel):
                await interaction.response.send_message(
                    embed=embeds.error("that ID isn't a category in this server."), ephemeral=True
                )
                return
            category_id = candidate.id

        emoji_value, emoji_problem = resolve_icon(
            self.f_emoji.value, interaction.guild, interaction.client
        )
        if emoji_problem:
            await interaction.response.send_message(
                embed=embeds.error(emoji_problem, title="Bad icon"), ephemeral=True
            )
            return

        await interaction.response.defer()

        welcome = resolve_text(
            self.f_welcome.value, interaction.guild, interaction.client
        ) or DEFAULT_BUTTON["welcome"]

        if self.existing is None:
            self.settings["buttons"].append(
                {
                    "key": uuid.uuid4().hex[:8],
                    "label": self.f_label.value,
                    "style": "primary",
                    "emoji": emoji_value,
                    "category_id": category_id,
                    "welcome": welcome,
                    "questions": [],
                }
            )
        else:
            self.existing["label"] = self.f_label.value
            self.existing["emoji"] = emoji_value
            self.existing["category_id"] = category_id
            self.existing["welcome"] = welcome

        save_config()
        await self.builder.refresh()

class QuestionsModal(discord.ui.Modal, title="Ticket Questions"):
    def __init__(self, builder, button_data):
        super().__init__()
        self.builder = builder
        self.button_data = button_data

        existing = button_data.get("questions", [])
        self.fields = []
        for i in range(MAX_QUESTIONS):
            current = existing[i]["label"] if i < len(existing) else ""
            field = discord.ui.TextInput(
                label=f"Question {i + 1}",
                default=current,
                placeholder="Leave blank to skip",
                required=False,
                max_length=45,
            )
            self.fields.append(field)
            self.add_item(field)

    async def on_submit(self, interaction):
        await interaction.response.defer()

        questions = []
        for field in self.fields:
            text = field.value.strip()
            if text:
                questions.append({"label": text})

        self.button_data["questions"] = questions
        save_config()
        await self.builder.refresh()

class PanelModeSelect(discord.ui.Select):
    def __init__(self, builder):
        self.builder = builder
        current = panel_mode(builder.settings["panel"])
        options = [
            discord.SelectOption(
                label=label, value=key, description=blurb, default=(key == current)
            )
            for key, label, blurb in PANEL_MODES
        ]
        super().__init__(placeholder="Panel style", options=options, row=0)

    async def callback(self, interaction):
        self.builder.settings["panel"]["mode"] = self.values[0]
        save_config()

        refreshed = AppearanceView(self.builder)
        await interaction.response.edit_message(
            content=refreshed.blurb(), view=refreshed
        )
        await self.builder.refresh()

class MenuTextModal(discord.ui.Modal, title="Dropdown text"):
    def __init__(self, builder):
        super().__init__()
        self.builder = builder
        self.field = discord.ui.TextInput(
            label="dropdown placeholder",
            default=(builder.settings["panel"].get("placeholder") or "open a ticket")[:150],
            max_length=150,
            required=True,
        )
        self.add_item(self.field)

    async def on_submit(self, interaction):
        self.builder.settings["panel"]["placeholder"] = self.field.value.strip() or "open a ticket"
        save_config()
        refreshed = AppearanceView(self.builder)
        await interaction.response.edit_message(content=refreshed.blurb(), view=refreshed)
        await self.builder.refresh()


class AppearanceView(discord.ui.View):
    def __init__(self, builder):
        super().__init__(timeout=300)
        self.builder = builder
        self.add_item(PanelModeSelect(builder))

    async def interaction_check(self, interaction):
        return interaction.user.id == self.builder.ctx.author.id

    def blurb(self):
        mode = panel_mode(self.builder.settings["panel"])
        notes = {
            "embed_title": "Title, description, images and colour all apply.",
            "embed_plain": "Title is hidden. Everything else applies.",
            "text": "Only the description is used, as plain message text.",
            "bare": "Nothing but buttons. Text, colour and images are ignored.",
        }
        layout = self.builder.settings["panel"].get("layout", "buttons")
        pick = "a dropdown menu" if layout == "dropdown" else "buttons"
        text = (
            f"**{panel_mode_label(self.builder.settings['panel'])}** - {notes[mode]}\n"
            f"options show as **{pick}**"
        )
        if layout == "dropdown":
            placeholder = self.builder.settings["panel"].get("placeholder") or "open a ticket"
            text += f"\ndropdown text : {placeholder}"
        return text

    @discord.ui.button(label="Edit Text & Images", style=discord.ButtonStyle.secondary, row=1)
    async def edit_text(self, interaction, button):
        await interaction.response.send_modal(
            EmbedEditModal(self.builder.settings, self.builder)
        )

    @discord.ui.button(label="Buttons / Dropdown", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_layout(self, interaction, button):
        panel = self.builder.settings["panel"]
        panel["layout"] = "buttons" if panel.get("layout", "buttons") == "dropdown" else "dropdown"
        save_config()
        refreshed = AppearanceView(self.builder)
        await interaction.response.edit_message(content=refreshed.blurb(), view=refreshed)
        await self.builder.refresh()

    @discord.ui.button(label="Dropdown text", style=discord.ButtonStyle.secondary, row=1)
    async def edit_menu_text(self, interaction, button):
        await interaction.response.send_modal(MenuTextModal(self.builder))

class SettingsView(discord.ui.View):
    def __init__(self, builder):
        super().__init__(timeout=300)
        self.builder = builder

    async def interaction_check(self, interaction):
        return interaction.user.id == self.builder.ctx.author.id

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.category],
        placeholder="Ticket category",
        row=0,
    )
    async def pick_category(self, interaction, select):
        await interaction.response.defer()
        self.builder.settings["category_id"] = select.values[0].id
        save_config()
        await self.builder.refresh()

    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="Staff roles (pick as many as you like)",
        min_values=0,
        max_values=MAX_STAFF_ROLES,
        row=1,
    )
    async def pick_roles(self, interaction, select):
        await interaction.response.defer()
        self.builder.settings["staff_role_ids"] = [r.id for r in select.values]
        save_config()
        await self.builder.refresh()

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        placeholder="Log channel",
        row=2,
    )
    async def pick_log(self, interaction, select):
        await interaction.response.defer()
        self.builder.settings["log_channel_id"] = select.values[0].id
        save_config()
        await self.builder.refresh()

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        placeholder="Where the panel gets posted",
        row=3,
    )
    async def pick_panel_channel(self, interaction, select):
        await interaction.response.defer()
        self.builder.settings["panel"]["channel_id"] = select.values[0].id
        save_config()
        await self.builder.refresh()

class StyleSelect(discord.ui.Select):
    def __init__(self, builder, button_data):
        self.builder = builder
        self.button_data = button_data
        current = canonical_style(button_data.get("style"))

        options = [
            discord.SelectOption(
                label=label,
                value=key,
                description=blurb,
                default=(key == current),
            )
            for key, label, blurb in STYLE_CHOICES
        ]

        super().__init__(placeholder="Button colour", options=options, row=0)

    async def callback(self, interaction):
        self.button_data["style"] = self.values[0]
        save_config()

        refreshed = ButtonManageView(self.builder, self.button_data)
        await interaction.response.edit_message(
            embed=refreshed.summary(), view=refreshed
        )
        await self.builder.refresh()

class ButtonCategorySelect(discord.ui.ChannelSelect):
    def __init__(self, builder, button_data):
        self.builder = builder
        self.button_data = button_data
        super().__init__(
            channel_types=[discord.ChannelType.category],
            placeholder="Category for this button (blank = default)",
            min_values=0,
            max_values=1,
            row=1,
        )

    async def callback(self, interaction):
        self.button_data["category_id"] = self.values[0].id if self.values else None
        save_config()

        refreshed = ButtonManageView(self.builder, self.button_data)
        await interaction.response.edit_message(
            embed=refreshed.summary(), view=refreshed
        )
        await self.builder.refresh()

class ButtonManageView(discord.ui.View):
    def __init__(self, builder, button_data):
        super().__init__(timeout=300)
        self.builder = builder
        self.button_data = button_data
        self.add_item(StyleSelect(builder, button_data))
        self.add_item(ButtonCategorySelect(builder, button_data))

    async def interaction_check(self, interaction):
        return interaction.user.id == self.builder.ctx.author.id

    def summary(self):
        count = len(self.button_data.get("questions", []))
        if count:
            mode = f"Asks {count} question(s) before opening"
            listed = "\n".join(
                f"{i + 1}. {q['label']}"
                for i, q in enumerate(self.button_data["questions"])
            )
        else:
            mode = "Opens a ticket immediately"
            listed = "No questions set."

        override = self.button_data.get("category_id")
        category = self.builder.ctx.guild.get_channel(override) if override else None
        category_text = category.name if category else "default category"

        return discord.Embed(
            title=f"Button: {self.button_data['label']}",
            description=(
                f"**Icon** - {icon_text(self.button_data)}\n"
                f"**Colour** - {style_label(self.button_data.get('style'))}\n"
                f"**Category** - {category_text}\n"
                f"**Behaviour** - {mode}\n\n"
                f"{listed}"
            ),
        )

    @discord.ui.button(label="Edit Details", style=discord.ButtonStyle.secondary, row=2)
    async def edit_details(self, interaction, button):
        await interaction.response.send_modal(
            ButtonEditModal(self.builder.settings, self.builder, self.button_data)
        )

    @discord.ui.button(label="Set Questions", style=discord.ButtonStyle.secondary, row=2)
    async def set_questions(self, interaction, button):
        await interaction.response.send_modal(
            QuestionsModal(self.builder, self.button_data)
        )

    @discord.ui.button(label="Open Instantly", style=discord.ButtonStyle.secondary, row=2)
    async def clear_questions(self, interaction, button):
        await interaction.response.defer()
        self.button_data["questions"] = []
        save_config()
        await self.builder.refresh()
        await interaction.followup.send(
            embed=embeds.notice(f"`{self.button_data['label']}` now opens a ticket immediately."),
            ephemeral=True,
        )

    @discord.ui.button(label="Delete Button", style=discord.ButtonStyle.secondary, row=3)
    async def delete_button(self, interaction, button):
        await interaction.response.defer()
        if self.button_data in self.builder.settings["buttons"]:
            self.builder.settings["buttons"].remove(self.button_data)
            save_config()
        await self.builder.refresh()
        await interaction.followup.send(
            embed=embeds.notice(f"removed `{self.button_data['label']}`."), ephemeral=True
        )
        self.stop()

class ButtonPickSelect(discord.ui.Select):
    def __init__(self, builder):
        self.builder = builder
        options = [
            discord.SelectOption(
                label=b["label"][:100],
                value=b["key"],
                emoji=icon_partial(b.get("emoji")),
                description=(
                    f"{len(b.get('questions', []))} question(s)"
                    if b.get("questions")
                    else "Opens instantly"
                ),
            )
            for b in builder.settings["buttons"]
        ]
        super().__init__(
            placeholder="Pick a button to manage",
            options=options or [discord.SelectOption(label="none", value="none")],
            disabled=not options,
        )

    async def callback(self, interaction):
        entry = find_button(self.builder.settings, self.values[0])
        if entry is None:
            await interaction.response.send_message(
                embed=embeds.error("that button is gone."), ephemeral=True
            )
            return

        manage = ButtonManageView(self.builder, entry)
        await interaction.response.edit_message(embed=manage.summary(), view=manage)

class ButtonsView(discord.ui.View):
    def __init__(self, builder):
        super().__init__(timeout=300)
        self.builder = builder
        self.add_item(ButtonPickSelect(builder))

    async def interaction_check(self, interaction):
        return interaction.user.id == self.builder.ctx.author.id

    @discord.ui.button(label="Add New Button", style=discord.ButtonStyle.secondary, row=1)
    async def add_button(self, interaction, button):
        if len(self.builder.settings["buttons"]) >= MAX_BUTTONS:
            await interaction.response.send_message(
                embed=embeds.error(f"you already have {MAX_BUTTONS} buttons."), ephemeral=True
            )
            return
        await interaction.response.send_modal(
            ButtonEditModal(self.builder.settings, self.builder)
        )

class BuilderView(discord.ui.View):
    def __init__(self, ctx, settings):
        super().__init__(timeout=900)
        self.ctx = ctx
        self.settings = settings
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                embed=embeds.error("this builder isn't yours."), ephemeral=True
            )
            return False
        if not can_manage(interaction.user):
            await interaction.response.send_message(
                embed=embeds.error("you need Administrator or Manage Server permission."), ephemeral=True
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
        panel = settings["panel"]

        category = guild.get_channel(settings.get("category_id"))
        log = guild.get_channel(settings.get("log_channel_id"))
        target = guild.get_channel(panel.get("channel_id"))
        roles = staff_roles(guild, settings)

        lines = [
            f"**Category** - {category.name if category else 'not set'}",
            f"**Staff** - {' '.join(r.mention for r in roles) if roles else 'not set'}",
            f"**Logs** - {log.mention if log else 'not set'}",
            f"**Panel** - {target.mention if target else 'not set'}",
            "",
            f"**Style** - {panel_mode_label(panel)}",
        ]

        if settings["buttons"]:
            lines.append("")
            lines.append("**Buttons**")
            for entry in settings["buttons"]:
                count = len(entry.get("questions", []))
                mode = f"asks {count}" if count else "instant"
                colour = style_label(entry.get("style"))
                icon = entry.get("emoji")
                shown = f"{icon} {entry['label']}" if icon else entry["label"]
                override = entry.get("category_id")
                cat = guild.get_channel(override) if override else None
                where = cat.name if cat else "default"
                lines.append(f"- {shown} ({colour}, {mode}, {where})")
        else:
            lines.append("")
            lines.append("**Buttons** - none yet, add one before publishing")

        return discord.Embed(
            title="Ticket Builder",
            description="\n".join(lines),
            color=panel["color"],
        )

    async def refresh(self):
        if self.message is None:
            return
        try:
            await self.message.edit(embed=self.status_embed(), view=self)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Channels & Roles", style=discord.ButtonStyle.secondary)
    async def open_settings(self, interaction, button):
        await interaction.response.send_message(
            embed=embeds.notice("pick your category, staff roles, log channel and panel channel."),
            view=SettingsView(self),
            ephemeral=True,
        )

    @discord.ui.button(label="Appearance", style=discord.ButtonStyle.secondary)
    async def open_appearance(self, interaction, button):
        view = AppearanceView(self)
        await interaction.response.send_message(
            view.blurb(), view=view, ephemeral=True
        )

    @discord.ui.button(label="Buttons", style=discord.ButtonStyle.secondary)
    async def open_buttons(self, interaction, button):
        view = ButtonsView(self)
        await interaction.response.send_message(
            embed=embeds.notice("manage the buttons that appear on your panel."),
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="Publish", style=discord.ButtonStyle.secondary)
    async def publish(self, interaction, button):
        settings = self.settings
        problems = []

        if not settings.get("category_id"):
            problems.append("ticket category")
        if not staff_role_ids(settings):
            problems.append("staff role")
        if not settings.get("log_channel_id"):
            problems.append("log channel")
        if not settings["panel"].get("channel_id"):
            problems.append("panel channel")
        if not settings["buttons"]:
            problems.append("at least one button")
        if panel_mode(settings["panel"]) == "text" and not settings["panel"].get(
            "description"
        ):
            problems.append("some text for plain text mode")

        if problems:
            await interaction.response.send_message(
                embed=embeds.error("still missing: " + ", ".join(problems)), ephemeral=True
            )
            return

        channel = interaction.guild.get_channel(settings["panel"]["channel_id"])
        if channel is None:
            await interaction.response.send_message(
                embed=embeds.error("the panel channel no longer exists."), ephemeral=True
            )
            return

        await interaction.response.defer()

        old_id = settings["panel"].get("message_id")
        if old_id:
            try:
                old = await channel.fetch_message(old_id)
                await old.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        content, embed = build_panel_payload(settings)
        view = build_panel_view(interaction.guild.id, settings)

        try:
            if embed is not None:
                sent = await channel.send(embed=embed, view=view)
            elif content:
                sent = await channel.send(content=content, view=view)
            else:
                sent = await channel.send(view=view)
        except discord.Forbidden:
            await interaction.followup.send(
                embed=embeds.error("i can't post in that channel."), ephemeral=True
            )
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(
                embed=embeds.error("discord turned the panel down. check the log."),
                ephemeral=True,
            )
            return

        settings["panel"]["message_id"] = sent.id
        save_config()

        for item in self.children:
            item.disabled = True

        if self.message:
            try:
                await self.message.edit(
                    content=f"Panel published in {channel.mention}.",
                    embed=self.status_embed(),
                    view=self,
                )
            except discord.HTTPException:
                pass
        self.stop()

class Tickets(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self._views_added = False

    @commands.Cog.listener()
    async def on_ready(self):
        if self._views_added:
            return
        self._views_added = True

        self.bot.add_view(TicketControlView())
        self.bot.add_view(LogControlView())

        for guild_key, settings in config.items():
            panel = settings.get("panel") or {}
            message_id = panel.get("message_id")
            if not message_id or not settings.get("buttons"):
                continue
            try:
                self.bot.add_view(
                    build_panel_view(int(guild_key), settings),
                    message_id=message_id,
                )
            except (ValueError, TypeError):
                continue

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        if channel.id in tickets:
            tickets.pop(channel.id, None)
            save_tickets()

    async def cog_check(self, ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if can_manage(ctx.author):
            return True
        raise commands.MissingPermissions(["manage_guild"])

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(embed=embeds.error("you need Administrator or Manage Server permission."))
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send(embed=embeds.error("this command only works in a server."))
        else:
            log.exception("Unhandled error in %s", ctx.command, exc_info=error)
            await embeds.send(
                ctx,
                embeds.error("something broke on my end. it has been logged."),
            )

    async def open_builder(self, ctx, settings):
        view = BuilderView(ctx, settings)
        message = await ctx.send(
            embed=view.status_embed(),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        view.message = message

    @commands.hybrid_group(
        name="ticketsetup",
        aliases=["tsetup"],
        invoke_without_command=True,
        fallback="menu",
        description="Set up or reconfigure the ticket system.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @commands.guild_only()
    async def ticketsetup(self, ctx):
        settings = get_config(ctx.guild.id)
        state = "configured" if is_configured(settings) else "not set up yet"

        embed = discord.Embed(
            title="Ticket Setup",
            description=(
                f"This server is **{state}**.\n\n"
                f"`{display_prefix(ctx)}ticketsetup fast` - creates one default button and "
                "opens the builder so you can pick your channels and publish.\n\n"
                f"`{display_prefix(ctx)}ticketsetup custom` - same builder, starting empty.\n\n"
                f"`{display_prefix(ctx)}ticketsetup edit` - reopen the builder on an "
                "existing setup."
            ),
        )
        await ctx.send(embed=embed)

    @ticketsetup.command(
        name="fast",
        description="Quick setup with one default button, then open the builder.",
    )
    async def setup_fast(self, ctx):
        settings = ensure_config(ctx.guild.id)

        if not settings["buttons"]:
            entry = dict(DEFAULT_BUTTON)
            entry["key"] = uuid.uuid4().hex[:8]
            entry["questions"] = []
            settings["buttons"].append(entry)

        if not settings["panel"].get("channel_id"):
            settings["panel"]["channel_id"] = ctx.channel.id

        save_config()

        await ctx.send(
            embed=embeds.notice("fast setup. open Channels & Roles, pick your three settings, "
            "then Publish. The panel goes in this channel unless you change it.")
        )
        await self.open_builder(ctx, settings)

    @ticketsetup.command(
        name="custom",
        description="Open the full builder, starting empty.",
    )
    async def setup_custom(self, ctx):
        settings = ensure_config(ctx.guild.id)
        save_config()
        await self.open_builder(ctx, settings)

    @ticketsetup.command(
        name="edit",
        description="Reopen the builder on an existing setup.",
    )
    async def setup_edit(self, ctx):
        if get_config(ctx.guild.id) is None:
            await ctx.send(
                embed=embeds.error(f"nothing to edit yet. run `{display_prefix(ctx)}ticketsetup fast` or "
                f"`{display_prefix(ctx)}ticketsetup custom` first.")
            )
            return
        settings = ensure_config(ctx.guild.id)
        await self.open_builder(ctx, settings)

    @commands.hybrid_command(
        name="ticketstats",
        aliases=["tstats"],
        description="Show open, unclaimed and lifetime ticket counts.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @commands.guild_only()
    async def ticket_stats(self, ctx):
        settings = get_config(ctx.guild.id)
        if settings is None:
            await ctx.send(embed=embeds.notice(f"run `{display_prefix(ctx)}ticketsetup` first."))
            return

        open_here = [d for d in tickets.values() if d["guild_id"] == ctx.guild.id]
        unclaimed = sum(1 for d in open_here if not d.get("claimed_by"))

        embed = discord.Embed(title="Ticket Stats")
        embed.add_field(name="Currently open", value=str(len(open_here)))
        embed.add_field(name="Unclaimed", value=str(unclaimed))
        embed.add_field(name="Total ever opened", value=str(settings.get("counter", 0)))
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(Tickets(bot))