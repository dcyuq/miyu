import datetime
import re

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from storage import Store

DEFAULT_TZ = "Asia/Singapore"
FALLBACK_TZ = datetime.timezone(datetime.timedelta(hours=8))
MIN_LEAD_SECONDS = 30
MAX_LEAD_DAYS = 365

_tz_store = Store("timezones.json")
timezones = _tz_store.load()

RELATIVE_PATTERN = re.compile(
    r"(?:(\d+)\s*(?:d|day|days))?\s*"
    r"(?:(\d+)\s*(?:h|hr|hrs|hour|hours))?\s*"
    r"(?:(\d+)\s*(?:m|min|mins|minute|minutes))?\s*$",
    re.IGNORECASE,
)

CLOCK_PATTERN = re.compile(
    r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$",
    re.IGNORECASE,
)

DATE_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M",
    "%d-%m-%Y %H:%M",
    "%d/%m/%Y %H:%M",
    "%b %d %Y %H:%M",
    "%d %b %Y %H:%M",
    "%Y-%m-%d",
)

WEEKDAYS = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}

COMMON_ZONES = [
    ("Asia/Manila", "Manila (Philippines)"),
    ("Asia/Hong_Kong", "Hong Kong"),
    ("Asia/Singapore", "Singapore"),
    ("Asia/Tokyo", "Tokyo"),
    ("Asia/Kolkata", "India"),
    ("Asia/Dubai", "Dubai"),
    ("Europe/London", "London"),
    ("Europe/Berlin", "Berlin"),
    ("Europe/Moscow", "Moscow"),
    ("America/New_York", "New York"),
    ("America/Chicago", "Chicago"),
    ("America/Denver", "Denver"),
    ("America/Los_Angeles", "Los Angeles"),
    ("America/Sao_Paulo", "Sao Paulo"),
    ("Australia/Sydney", "Sydney"),
    ("Pacific/Auckland", "Auckland"),
    ("UTC", "UTC"),
]

QUICK_OFFSETS = [
    (15, "In 15 minutes"),
    (60, "In 1 hour"),
    (180, "In 3 hours"),
    (360, "In 6 hours"),
    (720, "In 12 hours"),
    (1440, "Tomorrow, same time"),
]

REPEAT_CHOICES = [
    ("none", "Once", "Send it a single time, then forget it."),
    ("daily", "Every day", "Repeat at the same clock time each day."),
    ("weekly", "Every week", "Repeat on the same weekday each week."),
]

def save_timezones():
    _tz_store.save(timezones)

def tz_name(guild_id):
    return timezones.get(str(guild_id), DEFAULT_TZ)

def tz_for(guild_id):
    for name in (tz_name(guild_id), DEFAULT_TZ):
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return FALLBACK_TZ

def set_timezone(guild_id, name):
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False, (
            f"`{name}` isn't a timezone I recognise. Use a name like "
            "`Asia/Manila`, `Europe/London` or `America/New_York`."
        )

    timezones[str(guild_id)] = name
    save_timezones()
    return True, f"Timezone set to `{name}`."

def now_in(guild_id):
    return datetime.datetime.now(tz_for(guild_id))

def clock_parts(text):
    match = CLOCK_PATTERN.match(text.strip())
    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    suffix = (match.group(3) or "").lower()

    if suffix == "pm" and hour != 12:
        hour += 12
    elif suffix == "am" and hour == 12:
        hour = 0

    if hour > 23 or minute > 59:
        return None

    return hour, minute

def parse_when(raw, guild_id):
    if not raw or not raw.strip():
        return None, "Tell me when to send it."

    text = " ".join(raw.strip().split())
    lowered = text.lower()
    zone = tz_for(guild_id)
    now = datetime.datetime.now(zone)

    if lowered.startswith("in "):
        lowered = lowered[3:].strip()

    match = RELATIVE_PATTERN.fullmatch(lowered)
    if match and any(match.groups()):
        days = int(match.group(1) or 0)
        hours = int(match.group(2) or 0)
        minutes = int(match.group(3) or 0)
        delta = datetime.timedelta(days=days, hours=hours, minutes=minutes)
        if delta.total_seconds() <= 0:
            return None, "That works out to no time at all."
        return now + delta, None

    for word, offset in (("today", 0), ("tomorrow", 1)):
        if lowered.startswith(word):
            rest = lowered[len(word) :].strip().lstrip("at").strip()
            parts = clock_parts(rest) if rest else (9, 0)
            if parts is None:
                return None, f"I couldn't read the time after `{word}`."
            target = (now + datetime.timedelta(days=offset)).replace(
                hour=parts[0], minute=parts[1], second=0, microsecond=0
            )
            if target <= now:
                target += datetime.timedelta(days=1)
            return target, None

    first = lowered.split()[0]

    if first == "next" and len(lowered.split()) > 1:
        lowered = lowered[5:].strip()
        first = lowered.split()[0]

    if first in WEEKDAYS:
        rest = lowered[len(first) :].strip().lstrip("at").strip()
        parts = clock_parts(rest) if rest else (9, 0)
        if parts is None:
            return None, "I couldn't read the time after the weekday."

        target = now.replace(hour=parts[0], minute=parts[1], second=0, microsecond=0)
        ahead = (WEEKDAYS[first] - target.weekday()) % 7
        target += datetime.timedelta(days=ahead)
        if target <= now:
            target += datetime.timedelta(days=7)
        return target, None

    parts = clock_parts(lowered)
    if parts is not None:
        target = now.replace(hour=parts[0], minute=parts[1], second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        return target, None

    for fmt in DATE_FORMATS:
        try:
            naive = datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=zone), None

    return None, (
        "I couldn't read that time. Try `2h30m`, `9pm`, `tomorrow 8am`, "
        "`friday 18:00` or `2026-08-20 14:30`."
    )

def validate_when(target, guild_id):
    now = datetime.datetime.now(tz_for(guild_id))
    delta = (target - now).total_seconds()

    if delta < MIN_LEAD_SECONDS:
        return f"That's too soon. Give me at least {MIN_LEAD_SECONDS} seconds."
    if delta > MAX_LEAD_DAYS * 86400:
        return f"That's further out than {MAX_LEAD_DAYS} days."
    return None

def next_occurrence(timestamp, repeat, guild_id):
    if repeat not in ("daily", "weekly"):
        return None

    zone = tz_for(guild_id)
    moment = datetime.datetime.fromtimestamp(timestamp, zone)
    now = datetime.datetime.now(zone)
    step = datetime.timedelta(days=1 if repeat == "daily" else 7)

    while moment <= now:
        moment += step

    return moment.timestamp()

def repeat_label(repeat):
    for key, label, _ in REPEAT_CHOICES:
        if key == repeat:
            return label
    return "Once"

DATE_ONLY_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%d %b %Y",
    "%b %d %Y",
    "%d %b",
    "%b %d",
)

def parse_date_only(raw, guild_id):
    if not raw or not raw.strip():
        return None, "Give me a date."

    text = " ".join(raw.strip().split())
    today = datetime.datetime.now(tz_for(guild_id)).date()

    if text.lower() == "today":
        return today, None
    if text.lower() == "tomorrow":
        return today + datetime.timedelta(days=1), None

    for fmt in DATE_ONLY_FORMATS:
        try:
            parsed = datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        if "%Y" not in fmt:
            parsed = parsed.replace(year=today.year)
            if parsed < today:
                parsed = parsed.replace(year=today.year + 1)
        return parsed, None

    return None, (
        f"`{text}` isn't a date I can read. Try `2026-08-20`, `20/08/2026`, "
        "`Aug 20`, `today` or `tomorrow`."
    )

def parse_time_only(raw):
    if not raw or not raw.strip():
        return None, "Give me a time."

    parts = clock_parts(raw)
    if parts is None:
        return None, (
            f"`{raw.strip()}` isn't a time I can read. Try `14:30`, `2:30pm`, "
            "`9am` or `21:00`."
        )
    return parts, None

def build_datetime(guild_id, year, month, day, hour, minute):
    zone = tz_for(guild_id)

    try:
        naive = datetime.datetime(year, month, day, hour, minute)
    except ValueError:
        return None, f"{year}-{month:02d}-{day:02d} isn't a real date."

    return naive.replace(tzinfo=zone), None

def upcoming_days(guild_id, count=25):
    today = datetime.datetime.now(tz_for(guild_id)).date()
    return [today + datetime.timedelta(days=i) for i in range(count)]

def local_now_text(guild_id):
    moment = datetime.datetime.now(tz_for(guild_id))
    return moment.strftime("%a %d %b, %H:%M")

def describe_when(timestamp):
    stamp = int(timestamp)
    return f"<t:{stamp}:f> (<t:{stamp}:R>)"