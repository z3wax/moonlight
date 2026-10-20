"""Owns the Telegram connection: login from the panel, reconnects, logout."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from telethon import errors, functions

from .formatting import sender_label
from .session import FATAL_AUTH_ERRORS, build_client, save_string_backup

log = logging.getLogger(__name__)

STATES = {
    "no_credentials": "API ID/Hash kiritilmagan",
    "connecting": "Ulanmoqda...",
    "logged_out": "Login qilinmagan",
    "code_sent": "Kod yuborildi",
    "password_needed": "2FA parol kerak",
    "online": "Ishlayapti",
    "offline": "Aloqa yo'q, qayta ulanmoqda",
    "revoked": "Sessiya bekor qilindi",
}


class TelegramService:
    def __init__(self, settings, store, bot, backup=None):
        self.s = settings
        self.store = store
        self.bot = bot
        self.backup = backup
        self.client = None
        self.state = "connecting"
        self.state_since = time.time()
        self.last_error = ""
        self.started_at = time.time()
        self._wake = asyncio.Event()
        self._reconfigure = False
        self._phone = ""
        self._phone_code_hash = ""

    # --- helpers ---------------------------------------------------------

    def _set_state(self, state: str, error: str = "") -> None:
        if state != self.state:
            self.state = state
            self.state_since = time.time()
        self.last_error = error

    def wake(self) -> None:
        self._wake.set()

    async def _sleep_or_wake(self, seconds: float | None) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        self._wake.clear()

    def _changed(self) -> None:
        if self.backup is not None:
            self.backup.mark_dirty()

    def _forget_session(self) -> None:
        for path in (self.s.session_path, self.s.string_session_path,
                     self.s.session_path.with_suffix(".session-journal")):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        self.s.boot.string_session = ""  # never re-seed a dead session from env
        self._changed()

    # --- main loop -------------------------------------------------------

    async def run_forever(self) -> None:
        backoff = 5
        while True:
            self._wake.clear()
            if not self.s.has_telegram_credentials:
                self._set_state("no_credentials")
                await self._sleep_or_wake(None)
                continue

            self._set_state("connecting")
            try:
                self.client = build_client(self.s)
            except SystemExit as exc:  # invalid string session etc.
                self._set_state("revoked", str(exc))
                self._forget_session()
                await self._sleep_or_wake(None)
                continue
            self.bot.attach(self.client)
            try:
                await self.client.connect()
                if not await self.client.is_user_authorized():
                    if self.state not in {"code_sent", "password_needed"}:
                        self._set_state("logged_out")
                    while not self._reconfigure and not await self.client.is_user_authorized():
                        await self._sleep_or_wake(None)
                    if self._reconfigure:
                        raise _Reconfigure()
                await self.bot.on_connected()
                self._set_state("online")
                self._changed()
                backoff = 5
                await self.client.run_until_disconnected()
            except _Reconfigure:
                pass
            except FATAL_AUTH_ERRORS as exc:
                self.bot.fatal_error = exc
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any failure: retry, never die
                self._set_state("offline", repr(exc)[:300])
                log.warning("Telegram bilan aloqa uzildi: %r", exc)
            finally:
                await self.bot.shutdown()
                with contextlib.suppress(Exception):
                    await self.client.disconnect()

            if self.bot.fatal_error is not None:
                log.critical("Sessiya bekor qilindi: %r", self.bot.fatal_error)
                self._set_state("revoked", repr(self.bot.fatal_error))
                self._forget_session()
                self.bot.fatal_error = None
                continue  # back to "logged_out": log in again from the panel
            if self._reconfigure:
                self._reconfigure = False
                continue
            if self.state == "logged_out":
                continue
            self._set_state("offline", self.last_error)
            log.info("%s soniyadan keyin qayta ulanamiz...", backoff)
            await self._sleep_or_wake(backoff)
            backoff = min(backoff * 2, 300)

    async def reconfigure(self) -> None:
        """Rebuild the client (after API ID/hash/proxy/device changes)."""
        self._reconfigure = True
        if self.client is not None:
            with contextlib.suppress(Exception):
                await self.client.disconnect()
        self.wake()

    # --- login from the panel -------------------------------------------

    def _require_client(self):
        if self.client is None or not self.client.is_connected():
            raise RuntimeError(f"Telegram'ga hali ulanmagan ({STATES.get(self.state, self.state)}). Biroz kutib qayta urinib ko'ring.")
        return self.client

    async def send_code(self, phone: str) -> None:
        client = self._require_client()
        phone = phone.strip().replace(" ", "")
        result = await client.send_code_request(phone)
        self._phone, self._phone_code_hash = phone, result.phone_code_hash
        self.s.update({"phone": phone})
        self._set_state("code_sent")

    async def sign_in_code(self, code: str) -> str:
        client = self._require_client()
        if not self._phone_code_hash:
            raise RuntimeError("Avval kod so'rang.")
        try:
            await client.sign_in(self._phone, code.strip(), phone_code_hash=self._phone_code_hash)
        except errors.SessionPasswordNeededError:
            self._set_state("password_needed")
            return "password_needed"
        return await self._logged_in()

    async def sign_in_password(self, password: str) -> str:
        client = self._require_client()
        await client.sign_in(password=password)
        return await self._logged_in()

    async def _logged_in(self) -> str:
        me = await self.client.get_me()
        save_string_backup(self.client, self.s)
        self.client.session.save()
        self._phone_code_hash = ""
        log.info("Panel orqali kirildi: %s", sender_label(me))
        self._changed()
        if self.backup is not None:
            asyncio.ensure_future(self.backup.backup_now())
        self.wake()
        return "ok"

    async def logout(self) -> None:
        """Terminate this session on Telegram's side and forget it locally."""
        if self.client is not None and self.client.is_connected():
            with contextlib.suppress(Exception):
                await self.client.log_out()
        self._forget_session()
        self._set_state("logged_out")
        if self.backup is not None:
            await self.backup.backup_now()
        await self.reconfigure()

    # --- information for the panel --------------------------------------

    async def authorizations(self) -> list[dict]:
        client = self._require_client()
        result = await client(functions.account.GetAuthorizationsRequest())
        out = []
        for a in result.authorizations:
            out.append({
                "hash": str(a.hash),
                "current": bool(a.current),
                "device": a.device_model,
                "platform": f"{a.platform} {a.system_version}".strip(),
                "app": f"{a.app_name} {a.app_version}".strip(),
                "ip": a.ip,
                "country": a.country,
                "created": a.date_created.timestamp() if a.date_created else 0,
                "active": a.date_active.timestamp() if a.date_active else 0,
            })
        out.sort(key=lambda a: (not a["current"], -a["active"]))
        return out

    async def terminate_authorization(self, auth_hash: str) -> None:
        client = self._require_client()
        await client(functions.account.ResetAuthorizationRequest(hash=int(auth_hash)))

    def status(self) -> dict:
        me = self.bot.me
        dc = None
        if self.client is not None:
            with contextlib.suppress(Exception):
                dc = self.client.session.dc_id
        return {
            "state": self.state,
            "state_text": STATES.get(self.state, self.state),
            "state_since": self.state_since,
            "last_error": self.last_error,
            "connected_at": self.bot.connected_at,
            "last_update_at": self.bot.last_update_at,
            "session_file": self.s.session_path.exists(),
            "session_backup": self.s.string_session_path.exists(),
            "dc": dc,
            "device": self.s.device_model,
            "phone_hint": _mask_phone(self.s.phone),
            "account": None if me is None else {
                "id": me.id,
                "name": sender_label(me),
                "first_name": me.first_name or "",
                "last_name": me.last_name or "",
                "username": me.username or "",
                "phone": _mask_phone(me.phone or ""),
                "premium": bool(getattr(me, "premium", False)),
            },
        }


class _Reconfigure(Exception):
    pass


def _mask_phone(phone: str) -> str:
    digits = phone.strip()
    return digits[:4] + "•••••" + digits[-2:] if len(digits) > 7 else digits
