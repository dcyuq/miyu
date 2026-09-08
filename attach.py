import io
import logging
import re

import discord

log = logging.getLogger(__name__)

MAX_BYTES = 8 * 1024 * 1024

IMAGE_EXTENSIONS = ("png", "jpg", "jpeg", "gif", "webp", "avif", "heic")
VIDEO_EXTENSIONS = ("mp4", "mov", "webm", "m4v")

UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def safe_name(filename, fallback="proof"):
    cleaned = UNSAFE.sub("_", filename or "").strip("._")
    if not cleaned or "." not in cleaned:
        return f"{fallback}.png"
    return cleaned[-90:]


def extension(filename):
    return filename.rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""


def kind_of(attachment):
    content_type = (attachment.content_type or "").lower()
    ext = extension(attachment.filename)

    if content_type.startswith("image/") or ext in IMAGE_EXTENSIONS:
        return "image"
    if content_type.startswith("video/") or ext in VIDEO_EXTENSIONS:
        return "video"
    return None


class Media:
    def __init__(self, name, data, kind):
        self.name = name
        self.data = data
        self.kind = kind

    @property
    def is_video(self):
        return self.kind == "video"

    @property
    def reference(self):
        return f"attachment://{self.name}"

    def file(self):
        return discord.File(io.BytesIO(self.data), filename=self.name)


def limit_for(guild):
    if guild is None:
        return MAX_BYTES
    return max(MAX_BYTES, guild.filesize_limit)


async def read_media(attachment, limit=MAX_BYTES):
    if attachment is None:
        return None, None

    kind = kind_of(attachment)
    if kind is None:
        return None, "that file has to be an **image** or a **video**."

    if attachment.size > limit:
        return None, f"keep the file under **{limit // (1024 * 1024)}MB**."

    try:
        data = await attachment.read()
    except discord.HTTPException:
        log.exception("failed to read attachment %s", attachment.id)
        return None, "i could not read that file. try uploading it again."

    if not data:
        return None, "that file came through empty."

    return Media(safe_name(attachment.filename), data, kind), None


read_image = read_media


def frame(media):
    if media is None:
        return None

    if media.is_video:
        return discord.ui.Container(discord.ui.File(media.reference))

    return discord.ui.Container(
        discord.ui.MediaGallery(discord.MediaGalleryItem(media.reference))
    )