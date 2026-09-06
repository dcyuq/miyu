import discord

ACCENT = discord.Color(0x585858)

_installed = False


def install():
    global _installed
    if _installed:
        return

    original = discord.Embed.__init__

    def patched(self, *, colour=None, color=None, **kwargs):
        if colour is None and color is None:
            colour = ACCENT
        original(self, colour=colour, color=color, **kwargs)

    discord.Embed.__init__ = patched
    _installed = True


def build(description=None, *, title=None, color=None, **kwargs):
    return discord.Embed(
        title=title,
        description=description,
        color=ACCENT if color is None else color,
        **kwargs,
    )


def notice(description, *, title=None, **kwargs):
    return build(description, title=title, **kwargs)


def error(description, *, title=None, **kwargs):
    return build(description, title=title, **kwargs)


ok = notice
success = notice
info = notice
note = build


async def send(target, embed=None, *, view=None, content=None, ephemeral=False,
               allowed_mentions=None, **kwargs):
    if allowed_mentions is None:
        allowed_mentions = discord.AllowedMentions.none()

    payload = {"allowed_mentions": allowed_mentions, **kwargs}
    if content is not None:
        payload["content"] = content
    if embed is not None:
        payload["embed"] = embed
    if view is not None:
        payload["view"] = view

    if isinstance(target, discord.Interaction):
        try:
            if target.response.is_done():
                return await target.followup.send(ephemeral=ephemeral, **payload)
            return await target.response.send_message(ephemeral=ephemeral, **payload)
        except discord.HTTPException:
            return None

    try:
        return await target.send(**payload)
    except discord.Forbidden:
        pass
    except discord.HTTPException:
        return None

    if embed is not None:
        fallback = "\n".join(part for part in (embed.title, embed.description) if part)
        if fallback:
            try:
                return await target.send(fallback[:2000], allowed_mentions=allowed_mentions)
            except discord.HTTPException:
                return None
    return None