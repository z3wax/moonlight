"""Admin authentication: scrypt password hash, sessions, CSRF, brute-force lock."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

SESSION_TTL = 12 * 3600
IDLE_TTL = 2 * 3600
MAX_FAILS = 5
FAIL_WINDOW = 15 * 60
LOCK_SECONDS = 15 * 60
MIN_PASSWORD_LEN = 8


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    candidate = hash_password(password, bytes.fromhex(salt_hex)).split("$")[2]
    return hmac.compare_digest(candidate, digest_hex)


@dataclass
class AdminSession:
    token: str
    csrf: str
    ip: str
    created: float
    last_seen: float


class Auth:
    """Admin login. Sessions and lockouts live in SQLite, so they survive
    restarts and work when the web app runs as several WSGI processes."""

    def __init__(self, store, boot):
        self.store = store
        self.boot = boot
        self._ensure_credentials()

    # --- credentials -----------------------------------------------------

    def _ensure_credentials(self) -> None:
        if not self.store.get("admin_username"):
            self.store.set("admin_username", self.boot.admin_username)
        if self.store.get("admin_password_hash"):
            return
        password = self.boot.admin_password
        if not password:
            password = secrets.token_urlsafe(12)
            log.warning("ADMIN_PASSWORD berilmagan. Vaqtinchalik parol: %s "
                        "(kirgach panelda o'zgartiring)", password)
        self.store.set("admin_password_hash", hash_password(password))

    @property
    def username(self) -> str:
        return self.store.get("admin_username") or self.boot.admin_username

    def change_password(self, current: str, new: str, new_username: str | None = None) -> None:
        if not verify_password(current, self.store.get("admin_password_hash") or ""):
            raise ValueError("Joriy parol noto'g'ri")
        if len(new) < MIN_PASSWORD_LEN:
            raise ValueError(f"Yangi parol kamida {MIN_PASSWORD_LEN} belgidan iborat bo'lsin")
        self.store.set("admin_password_hash", hash_password(new))
        if new_username:
            self.store.set("admin_username", new_username.strip())
        self.store.delete_admin_session()  # log out everywhere

    # --- brute-force protection -----------------------------------------

    def locked_for(self, ip: str) -> int:
        return max(0, int(self.store.locked_until(ip) - time.time()))

    def _record_fail(self, ip: str) -> None:
        now = time.time()
        self.store.add_auth_fail(ip, now)
        if self.store.count_auth_fails(ip, now - FAIL_WINDOW) >= MAX_FAILS:
            self.store.lock_ip(ip, now + LOCK_SECONDS)
            self.store.clear_auth_fails(ip)
            log.warning("Admin panel: %s IP %s daqiqaga bloklandi (ko'p noto'g'ri urinish)",
                        ip, LOCK_SECONDS // 60)

    def login(self, username: str, password: str, ip: str) -> AdminSession | None:
        if self.locked_for(ip):
            return None
        ok_user = hmac.compare_digest(username.strip().lower(), self.username.lower())
        ok_pass = verify_password(password, self.store.get("admin_password_hash") or "")
        if not (ok_user and ok_pass):
            self._record_fail(ip)
            log.warning("Admin panel: noto'g'ri login urinish (%s)", ip)
            return None
        self.store.clear_auth_fails(ip)
        now = time.time()
        session = AdminSession(secrets.token_urlsafe(32), secrets.token_urlsafe(24), ip, now, now)
        self.store.save_admin_session(session.token, session.csrf, ip, now)
        log.info("Admin panelga kirildi (%s)", ip)
        return session

    def get(self, token: str | None) -> AdminSession | None:
        if not token:
            return None
        row = self.store.admin_session(token)
        if row is None:
            return None
        session = AdminSession(*row)
        now = time.time()
        if now - session.created > SESSION_TTL or now - session.last_seen > IDLE_TTL:
            self.store.delete_admin_session(token)
            return None
        self.store.touch_admin_session(token, now)
        session.last_seen = now
        return session

    def logout(self, token: str | None) -> None:
        if token:
            self.store.delete_admin_session(token)
