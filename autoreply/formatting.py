"""Pure helpers that turn Telegram messages into LLM chat history."""

from __future__ import annotations

from typing import Iterable

TELEGRAM_LIMIT = 4096


def describe_message(message) -> str:
    """Text of a message plus a short placeholder for any media it carries."""
    text = (getattr(message, "raw_text", None) or "").strip()
    media = _media_placeholder(message)
    parts = [p for p in (media, text) if p]
    if not parts:
        return ""
    result = " ".join(parts)
    if getattr(message, "fwd_from", None):
        result = "[boshqa joydan uzatilgan] " + result
    return result


def _media_placeholder(message) -> str:
    if getattr(message, "media", None) is None:
        return ""
    if getattr(message, "sticker", None):
        emoji = getattr(getattr(message, "file", None), "emoji", None) or ""
        return f"[stiker {emoji}]".replace(" ]", "]")
    if getattr(message, "photo", None):
        return "[rasm]"
    if getattr(message, "voice", None):
        return "[ovozli xabar]"
    if getattr(message, "video_note", None):
        return "[dumaloq video xabar]"
    if getattr(message, "gif", None):
        return "[GIF]"
    if getattr(message, "video", None):
        return "[video]"
    if getattr(message, "audio", None):
        return "[audio]"
    if getattr(message, "contact", None):
        return "[kontakt]"
    if getattr(message, "venue", None) or getattr(message, "geo", None):
        return "[joylashuv]"
    if getattr(message, "poll", None):
        return "[so'rovnoma]"
    if getattr(message, "dice", None):
        return "[o'yin zari]"
    if getattr(message, "document", None):
        name = getattr(getattr(message, "file", None), "name", None)
        return f"[fayl: {name}]" if name else "[fayl]"
    if getattr(message, "web_preview", None):
        return ""  # link preview: the URL is already in the text
    return "[media]"


def sender_label(sender) -> str:
    if sender is None:
        return "Noma'lum"
    name = " ".join(
        p for p in (getattr(sender, "first_name", None), getattr(sender, "last_name", None)) if p
    ) or getattr(sender, "title", None) or ""
    username = getattr(sender, "username", None)
    if username:
        return f"{name} (@{username})" if name else f"@{username}"
    return name or str(getattr(sender, "id", "Noma'lum"))


def build_history(
    messages: Iterable,
    *,
    after_id: int = 0,
    command_prefix: str = ".ai",
    with_names: bool = False,
) -> list[dict]:
    """Oldest-first messages -> OpenAI-style history, merging consecutive turns."""
    history: list[dict] = []
    prefix = command_prefix.lower()
    for message in messages:
        if getattr(message, "id", 0) <= after_id:
            continue
        if getattr(message, "action", None) is not None:  # service message
            continue
        text = describe_message(message)
        if not text:
            continue
        outgoing = bool(getattr(message, "out", False))
        if outgoing and is_command(text, prefix):
            continue
        role = "assistant" if outgoing else "user"
        if with_names and not outgoing:
            text = f"{sender_label(getattr(message, 'sender', None))}: {text}"
        if history and history[-1]["role"] == role:
            history[-1]["content"] += "\n" + text
        else:
            history.append({"role": role, "content": text})
    # A conversation sent to the model should start with the other person.
    while history and history[0]["role"] == "assistant":
        history.pop(0)
    return history


def is_command(text: str, prefix: str) -> bool:
    first = text.strip().split(maxsplit=1)
    return bool(first) and first[0].lower() == prefix.lower()


def split_text(text: str, limit: int = TELEGRAM_LIMIT - 96) -> list[str]:
    """Split a long reply into Telegram-sized chunks on paragraph/line/word borders."""
    text = text.strip()
    chunks: list[str] = []
    while len(text) > limit:
        cut = -1
        for sep in ("\n\n", "\n", ". ", " "):
            cut = text.rfind(sep, 0, limit)
            if cut > limit // 2:
                cut += len(sep)
                break
        if cut <= limit // 2:
            cut = limit
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks


def typing_delay(text: str, chars_per_second: float, max_seconds: float) -> float:
    if chars_per_second <= 0:
        return 0.0
    return max(0.0, min(max_seconds, len(text) / chars_per_second))


def matches_any(sender, chat_id: int, entries: Iterable[str]) -> bool:
    """True if the sender's id/username (or the chat id) is in ``entries``."""
    ids = {str(chat_id), str(getattr(sender, "id", ""))}
    username = (getattr(sender, "username", None) or "").lower()
    for entry in entries:
        value = entry.strip().lstrip("@").lower()
        if not value:
            continue
        if value in ids or (username and value == username):
            return True
    return False
