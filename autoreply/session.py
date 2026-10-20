"""Telegram session handling.

The session lives in ``sessions/<name>.session`` (SQLite, Telethon format)
and is the single source of truth. A ``sessions/<name>.string`` backup is
written after every successful start, and the file is re-created from that
backup (or from ``TELEGRAM_STRING_SESSION``) if it ever goes missing, so the
account never has to log in again unless Telegram itself revokes the session.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import unquote, urlparse

from telethon import TelegramClient, errors
from telethon.sessions import SQLiteSession, StringSession

from .config import Settings

log = logging.getLogger(__name__)

# Errors after which the session is no longer usable: log in again.
FATAL_AUTH_ERRORS = (
    errors.AuthKeyUnregisteredError,
    errors.AuthKeyDuplicatedError,
    errors.SessionRevokedError,
    errors.SessionExpiredError,
    errors.UserDeactivatedError,
    errors.UserDeactivatedBanError,
)

RELOGIN_HINT = (
    "Telegram sessiyani bekor qildi yoki u yaroqsiz. Qaytadan kiring:\n"
    "    python login.py\n"
    "Eslatma: bitta sessiyani bir vaqtda ikki joyda (masalan kompyuter va "
    "server) ishga tushirmang - Telegram ikkalasini ham o'chirib yuboradi."
)


class SessionLock:
    """Prevents two processes from using the same session file at once."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            raise SystemExit(
                f"'{self.path.stem}' sessiyasi allaqachon boshqa jarayonda ishlayapti. "
                "Avval uni to'xtating (bir sessiyani ikki marta ishga tushirish "
                "uni buzib qo'yadi)."
            )

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


def parse_proxy(url: str):
    """``socks5://user:pass@host:port`` / ``http://host:port`` -> Telethon proxy dict."""
    if not url:
        return None
    parsed = urlparse(url)
    scheme = (parsed.scheme or "socks5").lower()
    if scheme not in {"socks5", "socks4", "http"}:
        raise SystemExit(f"TELEGRAM_PROXY sxemasi qo'llab-quvvatlanmaydi: {scheme}")
    proxy = {
        "proxy_type": scheme,
        "addr": parsed.hostname,
        "port": parsed.port or (1080 if scheme.startswith("socks") else 8080),
        "rdns": True,
    }
    if parsed.username:
        proxy["username"] = unquote(parsed.username)
    if parsed.password:
        proxy["password"] = unquote(parsed.password)
    return proxy


def _secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _write_secret(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _seed_session_file(settings: Settings, string: str, source: str) -> None:
    try:
        seed = StringSession(string.strip())
    except ValueError as exc:
        raise SystemExit(f"{source} dagi string sessiya yaroqsiz: {exc}") from exc
    if not seed.auth_key:
        raise SystemExit(f"{source} dagi string sessiya bo'sh.")
    file_session = SQLiteSession(str(settings.sessions_dir / settings.session_name))
    file_session.set_dc(seed.dc_id, seed.server_address, seed.port)
    file_session.auth_key = seed.auth_key
    file_session.save()
    file_session.close()
    log.info("Sessiya fayli %s dan tiklandi: %s", source, settings.session_path)


def prepare_session_file(settings: Settings) -> None:
    """Make sure ``sessions/<name>.session`` exists if we have any backup of it."""
    _secure_dir(settings.sessions_dir)
    if settings.session_path.exists():
        return
    if settings.string_session:
        _seed_session_file(settings, settings.string_session, "TELEGRAM_STRING_SESSION")
    elif settings.string_session_path.exists():
        _seed_session_file(
            settings,
            settings.string_session_path.read_text(encoding="utf-8"),
            str(settings.string_session_path),
        )


def build_client(settings: Settings) -> TelegramClient:
    prepare_session_file(settings)
    client = TelegramClient(
        str(settings.sessions_dir / settings.session_name),
        settings.api_id,
        settings.api_hash,
        device_model=settings.device_model,
        system_version=settings.system_version,
        app_version=settings.app_version,
        lang_code=settings.lang_code,
        system_lang_code=settings.lang_code,
        proxy=parse_proxy(settings.proxy_url),
        connection_retries=10,
        retry_delay=5,
        request_retries=5,
        auto_reconnect=True,
        flood_sleep_threshold=60,
        timeout=20,
    )
    return client


def save_string_backup(client: TelegramClient, settings: Settings) -> str:
    """Write ``sessions/<name>.string`` so the session can always be restored."""
    string = StringSession.save(client.session)
    path = settings.string_session_path
    if not path.exists() or path.read_text(encoding="utf-8").strip() != string:
        _write_secret(path, string)
        log.info("Sessiya zaxirasi yangilandi: %s", path)
    return string
