"""Bot worker side of the Flask/WSGI deployment.

On hosts where the web app runs as WSGI (e.g. PythonAnywhere "Web") the
Telegram bot lives in a separate, always-running process. The two share the
SQLite database:

- the web app queues commands (send login code, change name, ...) in the
  ``commands`` table, the worker executes them and stores the result;
- the worker publishes its live status every few seconds;
- settings saved by the web app bump ``config_version`` and the worker
  reloads them.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import platform
import resource
import time

from .config import FIELD_MAP
from .llm import LLMError

log = logging.getLogger(__name__)

STATUS_KEY = "worker_status"
STATUS_INTERVAL = 3.0
POLL_INTERVAL = 0.5


class Bridge:
    def __init__(self, ctx):
        self.ctx = ctx
        self._version = ctx.settings.version

    async def run(self) -> None:
        await asyncio.gather(self._commands(), self._status(), self._watch_settings())

    # --- status ----------------------------------------------------------

    def snapshot(self) -> dict:
        c = self.ctx
        return {
            "ts": time.time(),
            "telegram": c.tg.status(),
            "llm": {
                "last_model": c.llm.last_model,
                "last_latency": round(c.llm.last_latency, 2),
                "last_error": c.llm.last_error,
                "requests": c.llm.total_requests,
                "keys": c.llm.key_status(),
            },
            "activity": {
                "replies_session": c.bot.replies_sent,
                "pending": len(c.bot._pending),
                "owner_id": c.owner.owner_id,
            },
            "server": {
                "uptime": time.time() - c.started_at,
                "python": platform.python_version(),
                "memory_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
                "load": os.getloadavg()[0] if hasattr(os, "getloadavg") else None,
                "backup": c.backup.status() if c.backup else {"enabled": False},
            },
        }

    async def _status(self) -> None:
        while True:
            try:
                # notify=False: a heartbeat is not a reason to re-upload backups
                self.ctx.store.set(STATUS_KEY, json.dumps(self.snapshot(), default=str), notify=False)
            except Exception:  # noqa: BLE001
                log.exception("Holatni yozib bo'lmadi")
            await asyncio.sleep(STATUS_INTERVAL)

    # --- settings saved by the web app -----------------------------------

    async def _watch_settings(self) -> None:
        c = self.ctx
        while True:
            await asyncio.sleep(1)
            version = c.settings.version
            if version == self._version:
                continue
            self._version = version
            before = c.settings.as_dict()
            c.settings.reload()
            after = c.settings.as_dict()
            changed = [k for k in after if before.get(k) != after.get(k)]
            if not changed:
                continue
            log.info("Sozlamalar yangilandi: %s", ", ".join(changed))
            if "ai_enabled" in changed and not after["ai_enabled"]:
                for chat_id in list(c.bot._pending):
                    c.bot._cancel_pending(chat_id)
            if "owner_account" in changed:
                await c.owner.resolve()
            if any(FIELD_MAP[k].restart for k in changed if k in FIELD_MAP):
                await c.tg.reconfigure()

    # --- commands --------------------------------------------------------

    async def _commands(self) -> None:
        store = self.ctx.store
        last_prune = 0.0
        while True:
            command = store.claim_command()
            if command is None:
                if time.time() - last_prune > 3600:
                    store.prune_commands(time.time() - 86400)
                    last_prune = time.time()
                await asyncio.sleep(POLL_INTERVAL)
                continue
            try:
                result = await self.execute(command["kind"], command["payload"])
                result = {"ok": True, **result}
            except Exception as exc:  # noqa: BLE001 - report every failure to the panel
                log.warning("Buyruq bajarilmadi (%s): %r", command["kind"], exc)
                text = str(exc) or exc.__class__.__name__
                if "FRESH_RESET" in text:
                    text = ("Telegram yangi (24 soatdan yosh) sessiyadan boshqalarni "
                            "o'chirishga ruxsat bermaydi.")
                result = {"ok": False, "error": text}
            store.finish_command(command["id"], result)

    async def execute(self, kind: str, p: dict) -> dict:
        c = self.ctx
        if kind == "send_code":
            await c.tg.send_code(str(p.get("phone", "")))
            return {"state": c.tg.state}
        if kind == "code":
            return {"result": await c.tg.sign_in_code(str(p.get("code", "")))}
        if kind == "password":
            return {"result": await c.tg.sign_in_password(str(p.get("password", "")))}
        if kind == "logout":
            await c.tg.logout()
            return {}
        if kind == "reconnect":
            await c.tg.reconfigure()
            return {}
        if kind == "sessions":
            return {"sessions": await c.tg.authorizations()}
        if kind == "terminate":
            await c.tg.terminate_authorization(str(p.get("hash", "")))
            return {}
        if kind == "name":
            return {"message": await c.owner.set_name(str(p.get("first_name", "")),
                                                      str(p.get("last_name", "")))}
        if kind == "photo_set":
            data = base64.b64decode(p.get("data", ""))
            return {"message": await c.owner.set_photo(data=data)}
        if kind == "photo_get":
            if c.bot.me is None or c.tg.client is None:
                return {"data": ""}
            data = await c.tg.client.download_profile_photo("me", file=bytes)
            return {"data": base64.b64encode(data).decode() if data else ""}
        if kind == "report":
            return {"sent": bool(await c.owner.send_report(force=True))}
        if kind == "post":
            return {"sent": bool(await c.owner.post_now())}
        if kind == "backup":
            if not c.backup or not c.backup.enabled:
                raise RuntimeError("Zaxira sozlanmagan (HF_TOKEN, STATE_REPO, SECRET_KEY)")
            if not await c.backup.backup_now():
                raise RuntimeError(c.backup.last_error or "zaxira yuklanmadi")
            return {}
        if kind == "ai_test":
            started = time.monotonic()
            try:
                answer = await c.llm.chat([
                    {"role": "system", "content": c.settings.system_prompt_rendered},
                    {"role": "user", "content": str(p.get("text") or "Salom!")[:2000]},
                ])
            except LLMError as exc:
                raise RuntimeError(str(exc)) from exc
            return {"answer": answer, "model": c.llm.last_model,
                    "seconds": round(time.monotonic() - started, 2)}
        if kind == "models":
            return {"models": await c.llm.list_models()}
        if kind == "key_check":
            return {"check": await c.llm.check_key(str(p.get("key", "")))}
        raise ValueError(f"Noma'lum buyruq: {kind}")
