"""Auto-reply logic: decide when to answer, ask OpenRouter, send the reply."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from collections import defaultdict, deque
from datetime import datetime

from telethon import TelegramClient, errors, events, functions

from .config import Settings
from .formatting import (
    build_history,
    is_command,
    matches_any,
    sender_label,
    split_text,
    typing_delay,
)
from .llm import LLMError, OpenRouterClient
from .session import FATAL_AUTH_ERRORS, save_string_backup
from .storage import Storage

log = logging.getLogger(__name__)

TELEGRAM_SERVICE_ID = 777000  # login codes come from here - never forward them to an LLM
MAX_IMAGE_BYTES = 4 * 1024 * 1024

DEBT_RE = re.compile(
    r"qarz|qarzdor|долг|задолж|верн\w*\s+деньг|pul(?:im|ni)\s+qaytar|\bdebt\b|owe\s+me",
    re.IGNORECASE,
)

HELP_TEXT = """\
🤖 Avtojavob buyruqlari ({p} ...):

{p} on — avtojavobni yoqish
{p} off — avtojavobni o'chirish
{p} status — holat
{p} stop — shu chatda botni o'chirish
{p} start — shu chatda botni qayta yoqish
{p} reset — shu chat tarixini AI uchun tozalash
{p} model [nom] — modelni ko'rish/o'zgartirish
{p} prompt [matn|clear] — qo'shimcha ko'rsatma
{p} test <matn> — OpenRouter'ni sinash
{p} help — shu yordam
"""


def is_debt_topic(text: str) -> bool:
    return bool(DEBT_RE.search(text or ""))


class AutoReplyBot:
    # How long to wait before deciding an outgoing message was typed by the owner.
    OWN_MESSAGE_GRACE = 2.0

    def __init__(
        self,
        client: TelegramClient | None,
        settings: Settings,
        store: Storage,
        llm: OpenRouterClient,
        owner=None,
    ):
        self.client = client
        self.s = settings
        self.store = store
        self.llm = llm
        self.owner = owner  # OwnerDesk (second account features), optional
        self.me = None
        self.fatal_error: Exception | None = None
        self.connected_at = 0.0
        self.last_update_at = 0.0
        self.replies_sent = 0
        self._pending: dict[int, asyncio.Task] = {}
        self._generating: set[int] = set()
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._sent_ids: dict[int, deque] = defaultdict(lambda: deque(maxlen=100))
        self._sending: set[int] = set()
        self._background: set[asyncio.Task] = set()
        self._watchdog: asyncio.Task | None = None
        self._rate_limited_logged: dict[int, float] = {}
        self._last_error_notice = 0.0

    # --- state -----------------------------------------------------------

    @property
    def away(self) -> bool:
        return bool(self.s.ai_enabled)

    @property
    def model(self) -> str:
        return self.s.openrouter_model

    @property
    def extra_prompt(self) -> str:
        return self.store.get("extra_prompt") or ""

    @property
    def online(self) -> bool:
        return self.me is not None and self.client is not None and self.client.is_connected()

    # --- lifecycle -------------------------------------------------------

    def attach(self, client: TelegramClient) -> None:
        self.client = client
        self.me = None
        self.fatal_error = None
        self.register()

    def register(self) -> None:
        self.client.add_event_handler(self.on_incoming, events.NewMessage(incoming=True))
        self.client.add_event_handler(self.on_outgoing, events.NewMessage(outgoing=True))

    async def on_connected(self) -> None:
        self.me = await self.client.get_me()
        self.connected_at = time.time()
        save_string_backup(self.client, self.s)
        log.info(
            "Ulandi: %s (id=%s). Avtojavob: %s, model: %s",
            sender_label(self.me), self.me.id,
            "YOQILGAN" if self.away else "O'CHIRILGAN", self.model,
        )
        if self._watchdog is not None:
            self._watchdog.cancel()  # never let an old one poke the new connection
        self._watchdog = asyncio.create_task(self._watchdog_loop())
        self._spawn(self.catch_up())

    async def shutdown(self) -> None:
        tasks = [*self._pending.values(), *self._background]
        if self._watchdog:
            tasks.append(self._watchdog)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._pending.clear()
        self.me = None

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    async def _fatal(self, exc: Exception) -> None:
        if self.fatal_error is None:
            self.fatal_error = exc
            log.critical("Sessiya yaroqsiz: %r", exc)
        await self.client.disconnect()

    async def _watchdog_loop(self) -> None:
        """Periodically pokes Telegram so a silently dead connection gets noticed.

        ``updates.GetState`` also tells the server we still want updates, which
        fixes the known "client is connected but stops receiving messages" case.
        """
        while self.client.is_connected():
            await asyncio.sleep(self.s.watchdog_interval)
            try:
                await asyncio.wait_for(
                    self.client(functions.updates.GetStateRequest()), timeout=60
                )
                self.store.prune_replies(time.time() - 2 * 3600)
                self.store.prune_events(time.time() - 14 * 86400)
                self.client.session.save()
            except FATAL_AUTH_ERRORS as exc:
                await self._fatal(exc)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Watchdog: aloqa javob bermadi (%r) - qayta ulanamiz", exc)
                await self.client.disconnect()
                return

    # --- filters ---------------------------------------------------------

    def _sender_allowed(self, sender, chat_id: int) -> bool:
        if sender is None:
            return False
        sender_id = getattr(sender, "id", None)
        if sender_id in (TELEGRAM_SERVICE_ID, getattr(self.me, "id", None)):
            return False
        if getattr(sender, "is_self", False) or getattr(sender, "support", False):
            return False
        if getattr(sender, "scam", False) or getattr(sender, "fake", False):
            return False
        if self.s.ignore_bots and getattr(sender, "bot", False):
            return False
        if self.s.only_contacts and not getattr(sender, "contact", False):
            return False
        if self.s.blacklist and matches_any(sender, chat_id, self.s.blacklist):
            return False
        if self.s.whitelist and not matches_any(sender, chat_id, self.s.whitelist):
            return False
        return True

    def _chat_allowed(self, chat_id: int) -> bool:
        if not self.away or self.store.is_chat_disabled(chat_id):
            return False
        if self.store.owner_active_until(chat_id) > time.time():
            return False
        sent = self.store.replies_since(chat_id, time.time() - 3600)
        if sent >= self.s.max_replies_per_hour:
            if time.time() - self._rate_limited_logged.get(chat_id, 0) > 600:
                self._rate_limited_logged[chat_id] = time.time()
                log.warning("Chat %s: soatlik limit (%s) tugadi, javob berilmaydi",
                            chat_id, self.s.max_replies_per_hour)
            return False
        return True

    # --- incoming --------------------------------------------------------

    async def on_incoming(self, event: events.NewMessage.Event) -> None:
        self.last_update_at = time.time()
        try:
            if self.me is None:
                return
            if event.is_private and self.owner is not None:
                sender = await event.get_sender()
                if self.owner.is_owner(sender):
                    await self.owner.handle_command(event)
                    return
            if not self.away:
                return
            # Groups only when enabled and the account is mentioned/replied to.
            if not event.is_private and not (self.s.reply_in_groups and event.mentioned):
                return
            sender = await event.get_sender()
            if not self._sender_allowed(sender, event.chat_id):
                return
            if not self._chat_allowed(event.chat_id):
                return
            self._schedule(event.chat_id)
        except FATAL_AUTH_ERRORS as exc:
            await self._fatal(exc)
        except Exception:  # noqa: BLE001
            log.exception("Kiruvchi xabarni qayta ishlashda xato")

    def _schedule(self, chat_id: int) -> None:
        """Debounce: several quick messages in a row get one combined answer."""
        task = self._pending.get(chat_id)
        if task and not task.done() and chat_id not in self._generating:
            task.cancel()
        self._pending[chat_id] = asyncio.create_task(self._respond_later(chat_id))

    def _cancel_pending(self, chat_id: int) -> None:
        task = self._pending.get(chat_id)
        if task and not task.done() and chat_id not in self._generating:
            task.cancel()

    async def _respond_later(self, chat_id: int) -> None:
        current = asyncio.current_task()
        try:
            await asyncio.sleep(self.s.debounce_seconds)
            async with self._locks[chat_id]:
                self._generating.add(chat_id)
                try:
                    await self._respond(chat_id)
                finally:
                    self._generating.discard(chat_id)
        except asyncio.CancelledError:
            pass
        except FATAL_AUTH_ERRORS as exc:
            await self._fatal(exc)
        except errors.FloodWaitError as exc:
            log.warning("Chat %s: FloodWait %s s - bu xabar o'tkazib yuborildi", chat_id, exc.seconds)
        except LLMError as exc:
            log.error("Chat %s: OpenRouter xatosi: %s", chat_id, exc)
            await self._send_fallback(chat_id)
            await self._notify_owner_error(f"⚠️ AI javob bera olmadi: {exc}")
        except Exception:  # noqa: BLE001
            log.exception("Chat %s: javob berishda xato", chat_id)
        finally:
            if self._pending.get(chat_id) is current:
                del self._pending[chat_id]

    async def _respond(self, chat_id: int) -> None:
        if not self._chat_allowed(chat_id):
            return

        is_private = chat_id > 0
        raw = await self.client.get_messages(chat_id, limit=self.s.history_limit)
        history = build_history(
            reversed(raw),
            after_id=self.store.reset_after_msg_id(chat_id),
            command_prefix=self.s.command_prefix,
            with_names=not is_private,
        )
        if not history or history[-1]["role"] != "user":
            return  # nothing new to answer (already replied, or owner answered)

        new_messages = []  # incoming messages since our last outgoing one
        for m in raw:
            if m.out:
                break
            new_messages.append(m)
        new_messages.reverse()

        if self.s.mark_read:
            await self.client.send_read_acknowledge(chat_id, raw[0])
        await asyncio.sleep(1.0)

        chat = await self.client.get_entity(chat_id)
        latest_text = history[-1]["content"]

        if is_private and self.s.debt_guard_enabled and is_debt_topic(latest_text):
            await self._send(chat_id, self.s.render(self.s.debt_reply))
            self.store.record_reply(chat_id)
            self.replies_sent += 1
            log.info("Chat %s: qarz mavzusi - tayyor javob yuborildi", chat_id)
            if self.owner is not None:
                self._spawn(self.owner.on_conversation(chat, new_messages, latest_text, urgent="debt"))
            return

        await self._maybe_send_notice(chat_id)

        images = await self._collect_images(new_messages) if self.s.vision_enabled else []
        if images:
            history[-1] = {"role": "user", "content": [
                {"type": "text", "text": latest_text},
                *({"type": "image_url", "image_url": {"url": url}} for url in images),
            ]}

        messages = [{"role": "system", "content": self._system_prompt(chat, is_private)}]
        messages += history

        if self.s.typing_enabled:
            async with self.client.action(chat_id, "typing"):
                reply = await self._generate(messages)
        else:
            reply = await self._generate(messages)

        # The owner may have jumped in, or turned the bot off, meanwhile.
        if not self._chat_allowed(chat_id):
            log.info("Chat %s: javob tayyor edi, lekin endi kerak emas", chat_id)
            return
        reply_to = None
        if not is_private:
            reply_to = next((m.id for m in raw if not m.out), None)
        await self._send(chat_id, reply, reply_to=reply_to)
        self.store.record_reply(chat_id)
        self.replies_sent += 1
        log.info("Chat %s (%s): javob yuborildi (%s belgi)",
                 chat_id, sender_label(chat), len(reply))
        if is_private and self.owner is not None:
            self._spawn(self.owner.on_conversation(chat, new_messages, latest_text, images=images))

    async def _send_fallback(self, chat_id: int) -> None:
        """No AI at all: send the fixed reply once, so nobody is left on read."""
        if not self.s.fallback_reply_enabled or not self.s.fallback_reply.strip():
            return
        if not self._chat_allowed(chat_id):
            return
        try:
            await self._send(chat_id, self.s.render(self.s.fallback_reply))
            self.store.record_reply(chat_id)
        except Exception:  # noqa: BLE001
            log.exception("Chat %s: tayyor javob yuborilmadi", chat_id)

    async def _generate(self, messages: list[dict]) -> str:
        started = time.monotonic()
        reply = await self.llm.chat(messages)
        if self.s.typing_enabled:
            remaining = typing_delay(
                reply, self.s.typing_chars_per_second, self.s.max_typing_seconds
            ) - (time.monotonic() - started)
            if remaining > 0:
                await asyncio.sleep(remaining)
        return reply

    async def _collect_images(self, messages) -> list[str]:
        """Data URLs of the photos in the newest incoming messages (max 2)."""
        urls = []
        for m in messages[-4:]:
            if not getattr(m, "photo", None):
                continue
            try:
                data = await self.client.download_media(m, file=bytes)
            except Exception:  # noqa: BLE001
                log.warning("Rasmni yuklab bo'lmadi", exc_info=True)
                continue
            if data and len(data) <= MAX_IMAGE_BYTES:
                urls.append("data:image/jpeg;base64," + base64.b64encode(data).decode())
            if len(urls) >= 2:
                break
        return urls

    def _system_prompt(self, chat, is_private: bool) -> str:
        now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M (%Z)")
        parts = [self.s.system_prompt_rendered.strip()]
        if self.extra_prompt:
            parts.append("Qo'shimcha ko'rsatmalar:\n" + self.extra_prompt)
        parts.append(
            "Agar xabarda [ovozli xabar], [video] kabi belgilar bo'lsa, sen ularning "
            "ichini ko'ra olmaysan - kerak bo'lsa buni ochiq ayt. Rasm biriktirilgan "
            "bo'lsa, uni ko'rib javob ber."
        )
        where = "shaxsiy chat" if is_private else "guruh"
        parts.append(f"Hozirgi vaqt: {now}. Suhbat: {where}, {sender_label(chat)}.")
        return "\n\n".join(parts)

    async def _maybe_send_notice(self, chat_id: int) -> None:
        if not self.s.away_notice_enabled or chat_id < 0:
            return
        cooldown = self.s.away_notice_cooldown_hours * 3600
        if time.time() - self.store.last_notice_at(chat_id) < cooldown:
            return
        await self._send(chat_id, self.s.render(self.s.away_notice))
        self.store.set_last_notice_at(chat_id, time.time())
        await asyncio.sleep(1.5)

    async def _send(self, chat_id: int, text: str, reply_to: int | None = None) -> None:
        self._sending.add(chat_id)
        try:
            for i, chunk in enumerate(split_text(text)):
                message = await self.client.send_message(
                    chat_id, chunk, link_preview=False, reply_to=reply_to if i == 0 else None
                )
                self._sent_ids[chat_id].append(message.id)
        finally:
            self._sending.discard(chat_id)

    async def _notify_owner_error(self, text: str) -> None:
        # At most one error notice per 30 minutes.
        if time.time() - self._last_error_notice < 1800:
            return
        self._last_error_notice = time.time()
        try:
            if self.owner is not None and await self.owner.send(text[:3500]):
                return
            await self.client.send_message("me", text[:3500])
        except Exception:  # noqa: BLE001
            log.exception("Xato haqida xabar yuborib bo'lmadi")

    # --- outgoing (owner) -----------------------------------------------

    async def on_outgoing(self, event: events.NewMessage.Event) -> None:
        try:
            if self.me is None:
                return
            text = event.message.raw_text or ""
            if is_command(text, self.s.command_prefix):
                await self._handle_command(event, text)
                return
            chat_id = event.chat_id
            if chat_id == self.me.id or self.s.owner_takeover_minutes <= 0:
                return
            # Let _send() record the id of a message the bot itself just sent.
            await asyncio.sleep(self.OWN_MESSAGE_GRACE)
            if event.message.id in self._sent_ids[chat_id] or chat_id in self._sending:
                return
            if self.owner is not None and self.owner.owner_id == chat_id:
                return  # the bot talking to the owner's second account
            until = time.time() + self.s.owner_takeover_minutes * 60
            self.store.set_owner_active_until(chat_id, until)
            self._cancel_pending(chat_id)
            log.info("Chat %s: siz o'zingiz yozdingiz - bot %s daqiqa jim turadi",
                     chat_id, int(self.s.owner_takeover_minutes))
        except FATAL_AUTH_ERRORS as exc:
            await self._fatal(exc)
        except Exception:  # noqa: BLE001
            log.exception("Chiquvchi xabarni qayta ishlashda xato")

    def set_ai_enabled(self, enabled: bool) -> None:
        self.s.update({"ai_enabled": enabled})
        if not enabled:
            for cid in list(self._pending):
                self._cancel_pending(cid)

    async def _handle_command(self, event: events.NewMessage.Event, text: str) -> None:
        parts = text.strip().split(maxsplit=2)
        sub = parts[1].lower() if len(parts) > 1 else "help"
        arg = parts[2].strip() if len(parts) > 2 else ""
        chat_id = event.chat_id
        in_saved = chat_id == self.me.id

        async def target_chat() -> int | None:
            if arg:
                entity = await self.client.get_entity(int(arg) if arg.lstrip("-").isdigit() else arg)
                return entity.id
            return None if in_saved else chat_id

        p = self.s.command_prefix
        if sub == "on":
            self.set_ai_enabled(True)
            result = "✅ Avtojavob YOQILDI"
        elif sub == "off":
            self.set_ai_enabled(False)
            result = "⏸ Avtojavob O'CHIRILDI"
        elif sub == "status":
            result = (
                f"🤖 Avtojavob: {'YOQILGAN ✅' if self.away else 'O‘CHIRILGAN ⏸'}\n"
                f"Model: {self.model}\n"
                f"O'chirilgan chatlar: {len(self.store.disabled_chats())}\n"
                f"Navbatdagi javoblar: {len(self._pending)}"
            )
        elif sub in {"stop", "pause"}:
            target = await target_chat()
            if target is None:
                result = f"Qaysi chat? Masalan: {p} stop @username"
            else:
                self.store.set_chat_disabled(target, True)
                self._cancel_pending(target)
                result = f"⏸ Chat {target} uchun bot o'chirildi"
        elif sub in {"start", "resume"}:
            target = await target_chat()
            if target is None:
                result = f"Qaysi chat? Masalan: {p} start @username"
            else:
                self.store.set_chat_disabled(target, False)
                self.store.set_owner_active_until(target, 0)
                result = f"▶️ Chat {target} uchun bot yoqildi"
        elif sub == "reset":
            if in_saved:
                result = "Bu buyruqni kerakli chatning o'zida yozing."
            else:
                self.store.set_reset_after_msg_id(chat_id, event.message.id)
                result = f"🧹 Chat {chat_id} tarixi AI uchun tozalandi"
        elif sub == "model":
            if arg:
                self.s.update({"openrouter_model": arg})
            result = f"Model: {self.model}"
        elif sub == "prompt":
            if not arg:
                result = "Qo'shimcha prompt:\n" + (self.extra_prompt or "(yo'q)")
            elif arg.lower() == "clear":
                self.store.set("extra_prompt", None)
                result = "Qo'shimcha prompt o'chirildi"
            else:
                self.store.set("extra_prompt", arg)
                result = "Qo'shimcha prompt saqlandi ✅"
        elif sub == "test":
            question = arg or "Salom! Qisqa qilib o'zingni tanishtir."
            try:
                answer = await self.llm.chat(
                    [{"role": "system", "content": self.s.system_prompt_rendered},
                     {"role": "user", "content": question}],
                )
                result = f"🧪 {self.llm.last_model}:\n\n{answer}"
            except LLMError as exc:
                result = f"⚠️ {exc}"
        else:
            result = HELP_TEXT.format(p=p)

        if in_saved:
            await event.message.edit(result[:4000])
        else:
            # Do not leave the command visible to the other person.
            await event.message.delete(revoke=True)
            await self.client.send_message("me", f"[chat {chat_id}] {result}"[:4000])

    # --- catch-up after downtime ----------------------------------------

    async def catch_up(self) -> None:
        """Answer private messages that arrived while the bot was offline."""
        if not self.s.catch_up_on_start or not self.away:
            return
        cutoff = time.time() - self.s.catch_up_max_age_hours * 3600
        scheduled = 0
        try:
            async for dialog in self.client.iter_dialogs(limit=200):
                if not dialog.is_user or dialog.unread_count <= 0:
                    continue
                message = dialog.message
                if message is None or message.out or message.date.timestamp() < cutoff:
                    continue
                if self.owner is not None and self.owner.is_owner(dialog.entity):
                    continue
                if not self._sender_allowed(dialog.entity, dialog.id):
                    continue
                if not self._chat_allowed(dialog.id) or dialog.id in self._pending:
                    continue
                self._schedule(dialog.id)
                scheduled += 1
                await asyncio.sleep(0.3)
        except FATAL_AUTH_ERRORS as exc:
            await self._fatal(exc)
            return
        except Exception:  # noqa: BLE001
            log.exception("O'qilmagan xabarlarni tekshirishda xato")
        if scheduled:
            log.info("Offline paytda kelgan %s ta chatga javob beriladi", scheduled)
