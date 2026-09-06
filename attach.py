import asyncio
import io
import re

import discord

FALLBACK_NAME = "image.png"

# How many attachments a single reading (or message) may carry. Discord
# caps a message at ten files, so there is no point taking more.
MAX_FILES = 10

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp")


def safe_filename(name):
    """attachment:// cannot reference a name with spaces or odd characters."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name or FALLBACK_NAME)
    return cleaned[-60:] or FALLBACK_NAME


class Picture:
    """An uploaded file, re-readable.

    Discord's CDN links are signed and stop working after about a day, so
    the bytes are held and re-uploaded rather than linked. A discord.File
    is single use, so each destination gets a fresh one from the same bytes.

    Despite the name it happily holds any file, not just images; is_image
    tells callers which ones an embed can actually show.
    """

    def __init__(self, data, filename, content_type=None):
        self.data = data
        self.filename = safe_filename(filename)
        self.content_type = content_type or ""

    @property
    def is_image(self):
        if self.content_type.startswith("image/"):
            return True
        return self.filename.lower().endswith(IMAGE_EXTS)

    @property
    def reference(self):
        return f"attachment://{self.filename}"

    def file(self):
        return discord.File(io.BytesIO(self.data), filename=self.filename)


async def read_image(attachment):
    """Returns (picture, error). Blank attachment is not an error.

    Image only; used where an embed image is the whole point (vouches).
    """
    if attachment is None:
        return None, None

    if not (attachment.content_type or "").startswith("image/"):
        return None, "that attachment is not an image."

    try:
        data = await attachment.read()
    except discord.HTTPException:
        return None, "i could not read that image."

    return Picture(data, attachment.filename, attachment.content_type), None


async def read_file(attachment):
    """Read one attachment of any type. Returns (picture, error)."""
    if attachment is None:
        return None, None

    try:
        data = await attachment.read()
    except discord.HTTPException:
        return None, f"i could not read `{attachment.filename}`."

    return Picture(data, attachment.filename, attachment.content_type), None


async def read_files(attachments, limit=MAX_FILES):
    """Read several attachments of any type. Returns (pictures, error).

    Downloads run concurrently, so ten photos take about as long as one.
    An empty list in, an empty list out - no attachments is not an error.
    """
    picked = [a for a in attachments if a is not None]
    if len(picked) > limit:
        return [], f"that is too many files. i can take up to {limit} at once."

    results = await asyncio.gather(*(read_file(a) for a in picked))

    pictures = []
    for picture, problem in results:
        if problem:
            return [], problem
        pictures.append(picture)
    return pictures, None


def attachment_payload(pictures, embed_target=None):
    """Fresh discord.File list with unique names, safe to send together.

    Returns (files, reference). Two uploads that share a filename would
    collide, so repeats get a numeric suffix. reference is the
    attachment:// url for embed_target once its final (deduped) name is
    known, or None when embed_target is not in the list.
    """
    used = set()
    files = []
    reference = None

    for picture in pictures:
        name = picture.filename
        stem, dot, ext = picture.filename.rpartition(".")
        attempt = 1
        while name in used:
            attempt += 1
            name = f"{stem}-{attempt}{dot}{ext}" if dot else f"{picture.filename}-{attempt}"
        used.add(name)

        files.append(discord.File(io.BytesIO(picture.data), filename=name))
        if picture is embed_target:
            reference = f"attachment://{name}"

    return files, reference