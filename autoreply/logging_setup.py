"""Console + rotating file logging + an in-memory tail for the admin panel."""

from __future__ import annotations

import collections
import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

SECRET_RE = re.compile(r"(sk-or-[A-Za-z0-9-]{4})[A-Za-z0-9-]+")


class RedactFilter(logging.Filter):
    """Never let an API key end up in a log line."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = SECRET_RE.sub(r"\1…", message)
        if redacted != message:
            record.msg, record.args = redacted, ()
        return True


class BufferHandler(logging.Handler):
    def __init__(self, buffer: collections.deque):
        super().__init__()
        self.buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buffer.append(self.format(record))
        except Exception:  # noqa: BLE001
            pass


def setup_logging(level: str, data_dir: Path) -> collections.deque:
    data_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers.clear()

    buffer: collections.deque = collections.deque(maxlen=1000)
    handlers = [
        logging.StreamHandler(),
        RotatingFileHandler(data_dir / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=3,
                            encoding="utf-8"),
        BufferHandler(buffer),
    ]
    redact = RedactFilter()
    for handler in handlers:
        handler.setFormatter(fmt)
        handler.addFilter(redact)
        root.addHandler(handler)

    # Telethon/aiohttp/httpx are chatty at INFO.
    for name in ("telethon", "httpx", "aiohttp.access", "huggingface_hub"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return buffer
