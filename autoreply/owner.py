"""Second-account features: summaries, reports, remote control, channel posts.

The owner's second Telegram account is the "control room":
- who wrote and what about (short summaries, image analysis) arrives there,
- a periodic report arrives there,
- commands sent from there change the profile name/photo, schedule channel
  posts, toggle the AI and so on.

The owner account is pinned by its numeric ID the first time it is resolved,
so someone who later grabs the same @username can never control the bot.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from telethon import functions, types

from .formatting import sender_label
from .llm import LLMError

log = logging.getLogger(__name__)

NOTIFY_COOLDOWN = 600  # one instant summary per chat per 10 minutes

OWNER_HELP = """\
🛠 Boshqaruv buyruqlari:

/status — holat
/ai on | /ai off — avtojavobni yoqish/o'chirish
/name Ism Familiya — profil ismini o'zgartirish
/photo — rasm bilan birga yuboring: profil rasmi o'zgaradi
/report — hozir hisobot
/post — kanalga hozir e'lon
/channel @kanal — e'lon kanali
/time 22:30 — e'lon vaqti
/topics — keyingi qatorlarda mavzular
/posts on|off — kunlik e'lonlar
/reports on|off — muntazam hisobot

Oddiy so'z bilan ham yozish mumkin, masalan:
"ismimni Ali Valiyev qil" yoki "kanalimga har kuni 21:00 da texnologiya haqida e'lon ber".
"""

INTENT_PROMPT = """\
Sen buyruqni JSON ga aylantiruvchi yordamchisan. Faqat JSON qaytar, boshqa matn yo'q.
Ruxsat etilgan "action" qiymatlari:
- "set_name": {"first_name": str, "last_name": str}
- "set_photo": {}  (faqat foydalanuvchi profil rasmini o'zgartirishni so'rasa)
- "schedule_posts": {"channel": str|null, "time": "HH:MM"|null, "topics": [str]|null}
- "post_now": {}
- "report": {}
- "status": {}
- "ai_on": {}
- "ai_off": {}
- "none": {}  (boshqa har qanday narsa)
Format: {"action": "...", "args": {...}}
"""


def _tz(name: str):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return timezone(timedelta(hours=5))


def parse_intent(text: str) -> dict:
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return {"action": "none", "args": {}}
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return {"action": "none", "args": {}}
    if not isinstance(data, dict):
        return {"action": "none", "args": {}}
    args = data.get("args") if isinstance(data.get("args"), dict) else {}
    return {"action": str(data.get("action", "none")), "args": args}


class OwnerDesk:
    def __init__(self, settings, store, llm):
        self.s = settings
        self.store = store
        self.llm = llm
        self.bot = None  # set by the runner
        self._last_notify: dict[int, float] = {}

    @property
    def client(self):
        return self.bot.client if self.bot else None

    # --- identity --------------------------------------------------------

    @property
    def owner_id(self) -> int | None:
        if not self.s.owner_account:
            return None
        if self.store.get("owner_ref") != self.s.owner_account:
            return None
        value = self.store.get("owner_id")
        return int(value) if value else None

    def is_owner(self, sender) -> bool:
        owner_id = self.owner_id
        return owner_id is not None and getattr(sender, "id", None) == owner_id

    async def resolve(self) -> None:
        """Pin the configured account to its numeric ID (once per value)."""
        ref = self.s.owner_account
        if not ref or self.client is None or self.bot.me is None:
            return
        if self.store.get("owner_ref") == ref and self.store.get("owner_id"):
            return
        try:
            entity = await self.client.get_entity(int(ref) if ref.lstrip("-").isdigit() else ref)
        except Exception as exc:  # noqa: BLE001
            log.warning("Ikkinchi akkaunt topilmadi (%s): %s", ref, exc)
            return
        if not isinstance(entity, types.User) or entity.id == self.bot.me.id or entity.bot:
            log.warning("Ikkinchi akkaunt sifatida oddiy foydalanuvchi ko'rsating: %s", ref)
            return
        self.store.set("owner_id", str(entity.id))
        self.store.set("owner_ref", ref)
        log.info("Ikkinchi akkaunt: %s (id=%s)", sender_label(entity), entity.id)

    async def send(self, text: str, file=None) -> bool:
        owner_id = self.owner_id
        if owner_id is None or self.client is None:
            return False
        if file is not None:
            await self.client.send_file(owner_id, file, caption=text[:1000])
        else:
            await self.client.send_message(owner_id, text[:4000], link_preview=False)
        return True

    async def owner_online(self) -> bool:
        owner_id = self.owner_id
        if owner_id is None:
            return False
        try:
            users = await self.client(functions.users.GetUsersRequest(
                [await self.client.get_input_entity(owner_id)]
            ))
        except Exception:  # noqa: BLE001
            return False
        status = getattr(users[0], "status", None) if users else None
        if isinstance(status, types.UserStatusOnline):
            return True
        if isinstance(status, types.UserStatusOffline):
            return (datetime.now(timezone.utc) - status.was_online).total_seconds() < 120
        return False

    # --- summaries -------------------------------------------------------

    async def on_conversation(self, chat, new_messages, text: str, images=None, urgent=None):
        name = sender_label(chat)
        kind = urgent or "message"
        summary = ""
        try:
            notify = self.s.notify_enabled and self.owner_id is not None
            if notify and not urgent:
                recent = time.time() - self._last_notify.get(chat.id, 0) < NOTIFY_COOLDOWN
                notify = not recent and (not self.s.notify_only_when_online or await self.owner_online())
            if notify:
                self._last_notify[chat.id] = time.time()
                summary = await self.summarize(name, text, images if self.s.notify_images else None)
                header = "🚨 QARZ MAVZUSI — " if urgent == "debt" else "📩 "
                await self.send(f"{header}{name} yozdi:\n\n{summary}")
                if images and self.s.notify_images:
                    for m in new_messages:
                        if getattr(m, "photo", None):
                            await self.send(f"🖼 {name} yuborgan rasm", file=m.media)
        except Exception:  # noqa: BLE001
            log.exception("Ikkinchi akkauntga xabar yuborib bo'lmadi")
        self.store.add_event(chat.id, name, kind, summary or text)

    async def summarize(self, name: str, text: str, images=None) -> str:
        prompt = (
            "Egasi uchun quyidagi xabarni o'zbek tilida 1-2 jumlada qisqa tushuntir. "
            "Boshida toifani yoz: [Yordam so'rayapti], [Taklif], [Mijoz bo'lmoqchi], "
            "[Qarz], [Salomlashish] yoki [Boshqa]. Rasm bo'lsa, unda nima borligini "
            "qisqa tahlil qil."
        )
        content = f"Yozgan: {name}\nXabar:\n{text[:3000]}"
        user = {"role": "user", "content": content}
        if images:
            user["content"] = [{"type": "text", "text": content},
                               *({"type": "image_url", "image_url": {"url": u}} for u in images)]
        try:
            return await self.llm.chat([{"role": "system", "content": prompt}, user],
                                       max_tokens=250, temperature=0.3)
        except LLMError:
            return text[:400]

    # --- periodic jobs ---------------------------------------------------

    async def tick(self) -> None:
        if self.client is None or self.bot.me is None:
            return
        await self.resolve()
        await self._maybe_report()
        await self._maybe_post()

    async def _maybe_report(self) -> None:
        if not self.s.reports_enabled or self.owner_id is None:
            return
        last = float(self.store.get("last_report_at") or 0)
        if not last:
            self.store.set("last_report_at", str(time.time()))
            return
        if time.time() - last >= self.s.report_interval_hours * 3600:
            await self.send_report()

    async def send_report(self, force: bool = False) -> bool:
        since = float(self.store.get("last_report_at") or 0) or time.time() - 86400
        events = self.store.events_since(since)
        self.store.set("last_report_at", str(time.time()))
        hours = max(1, round((time.time() - since) / 3600))
        if not events:
            if not (force or self.s.report_when_empty):
                return False
            return await self.send(f"📊 Hisobot (oxirgi {hours} soat)\n\nHech kim yozmadi. Hammasi tinch ✅")
        people = {e["chat_id"] for e in events}
        debts = sum(1 for e in events if e["kind"] == "debt")
        lines = [f"- {e['name']}: {e['text'][:200]}" for e in events[-60:]]
        body = "\n".join(lines)
        try:
            body = await self.llm.chat([
                {"role": "system", "content":
                    "Egasi uchun o'zbek tilida qisqa hisobot tuz: har bir odam bo'yicha bitta "
                    "qator - kim, nima haqida, javob kerakmi. Muhimlarini birinchi yoz."},
                {"role": "user", "content": body},
            ], max_tokens=700, temperature=0.3)
        except LLMError:
            pass
        head = f"📊 Hisobot (oxirgi {hours} soat)\nOdamlar: {len(people)}, xabarlar: {len(events)}"
        if debts:
            head += f", qarz mavzusi: {debts} 🚨"
        return await self.send(f"{head}\n\n{body}")

    def _topics(self) -> list[str]:
        return [t.strip() for t in (self.s.post_topics or "").splitlines() if t.strip()]

    async def _maybe_post(self) -> None:
        if not (self.s.channel_posts_enabled and self.s.channel and self._topics()):
            return
        now = datetime.now(_tz(self.s.timezone))
        h, m = map(int, self.s.post_time.split(":"))
        due = now.replace(hour=h, minute=m, second=0, microsecond=0)
        today = now.strftime("%Y-%m-%d")
        if self.store.get("last_post_date") == today:
            return
        if not (due <= now <= due + timedelta(hours=2)):
            return
        attempts_key = f"post_attempts_{today}"
        attempts = int(self.store.get(attempts_key) or 0)
        if attempts >= 3:
            return
        self.store.set(attempts_key, str(attempts + 1))
        if await self.post_now():
            self.store.set("last_post_date", today)

    async def post_now(self) -> bool:
        topics = self._topics()
        if not (self.s.channel and topics):
            return False
        index = int(self.store.get("post_topic_idx") or 0) % len(topics)
        topic = topics[index]
        try:
            text = await self.llm.chat([
                {"role": "system", "content":
                    "Sen Telegram kanal uchun e'lon yozuvchisan. Faqat tayyor e'lon matnini "
                    f"qaytar, izohsiz. Uslub: {self.s.post_style}"},
                {"role": "user", "content": f"Mavzu: {topic}"},
            ], max_tokens=800)
            await self.client.send_message(self.s.channel, text, link_preview=False)
        except Exception as exc:  # noqa: BLE001
            log.error("Kanalga e'lon yuborilmadi: %s", exc)
            await self.send(f"⚠️ Kanalga e'lon yuborilmadi: {exc}")
            return False
        self.store.set("post_topic_idx", str(index + 1))
        log.info("Kanalga e'lon yuborildi: %s", topic)
        await self.send(f"📢 {self.s.channel} ga e'lon chiqdi (mavzu: {topic})")
        return True

    # --- remote control --------------------------------------------------

    async def handle_command(self, event) -> None:
        msg = event.message
        text = (msg.raw_text or "").strip()
        if not self.s.remote_control_enabled:
            await event.reply("🔒 Boshqaruv admin paneldan o'chirilgan.")
            return
        try:
            reply = await self._dispatch(msg, text)
        except Exception as exc:  # noqa: BLE001
            log.exception("Ikkinchi akkaunt buyrug'ida xato")
            reply = f"⚠️ Xato: {exc}"
        if reply:
            await event.reply(reply[:4000])

    async def _dispatch(self, msg, text: str) -> str:
        has_photo = bool(getattr(msg, "photo", None))
        if text.startswith("/"):
            head, _, rest = text.partition(" ")
            cmd = head[1:].split("@")[0].lower()
            rest = rest.strip()
            if cmd in {"help", "start"}:
                return OWNER_HELP
            if cmd == "status":
                return self.status_text()
            if cmd == "ai":
                return self._toggle("ai_enabled", rest, "Avtojavob")
            if cmd == "posts":
                return self._toggle("channel_posts_enabled", rest, "Kunlik e'lonlar")
            if cmd == "reports":
                return self._toggle("reports_enabled", rest, "Muntazam hisobot")
            if cmd == "name":
                first, _, last = rest.partition(" ")
                return await self.set_name(first, last)
            if cmd == "photo":
                return await self.set_photo(msg)
            if cmd == "report":
                await self.send_report(force=True)
                return ""
            if cmd == "post":
                return "✅" if await self.post_now() else "⚠️ Kanal yoki mavzular sozlanmagan."
            if cmd == "channel":
                self.s.update({"channel": rest})
                return f"Kanal: {rest}"
            if cmd == "time":
                self.s.update({"post_time": rest})
                return f"E'lon vaqti: {self.s.post_time}"
            if cmd == "topics":
                topics = text.split("\n", 1)[1] if "\n" in text else rest
                self.s.update({"post_topics": topics})
                return f"Mavzular saqlandi: {len(self._topics())} ta"
            return OWNER_HELP

        if not text and has_photo:
            return "Rasmni profilga qo'yish uchun izohiga /photo deb yozing."
        if not text:
            return ""
        try:
            raw = await self.llm.chat(
                [{"role": "system", "content": INTENT_PROMPT},
                 {"role": "user", "content": text + ("\n(rasm biriktirilgan)" if has_photo else "")}],
                max_tokens=300, temperature=0,
            )
        except LLMError as exc:
            return f"⚠️ AI ishlamadi: {exc}"
        intent = parse_intent(raw)
        action, args = intent["action"], intent["args"]
        if action == "set_name":
            return await self.set_name(str(args.get("first_name") or ""), str(args.get("last_name") or ""))
        if action == "set_photo":
            return await self.set_photo(msg) if has_photo else "Rasmni ham birga yuboring."
        if action == "schedule_posts":
            changes = {"channel_posts_enabled": True}
            if args.get("channel"):
                changes["channel"] = str(args["channel"])
            if args.get("time"):
                changes["post_time"] = str(args["time"])
            if isinstance(args.get("topics"), list) and args["topics"]:
                changes["post_topics"] = "\n".join(str(t) for t in args["topics"])
            self.s.update(changes)
            return (f"✅ Kunlik e'lon: {self.s.channel or '(kanal kiritilmagan)'} — "
                    f"har kuni {self.s.post_time}\nMavzular: {len(self._topics())} ta")
        if action == "post_now":
            return "✅" if await self.post_now() else "⚠️ Kanal yoki mavzular sozlanmagan."
        if action == "report":
            await self.send_report(force=True)
            return ""
        if action == "status":
            return self.status_text()
        if action in {"ai_on", "ai_off"}:
            self.bot.set_ai_enabled(action == "ai_on")
            return "✅ Avtojavob yoqildi" if action == "ai_on" else "⏸ Avtojavob o'chirildi"
        return "Tushunmadim. /help — buyruqlar ro'yxati."

    def _toggle(self, key: str, arg: str, label: str) -> str:
        if arg.lower() not in {"on", "off"}:
            return f"{label}: {'yoqilgan' if getattr(self.s, key) else 'o‘chirilgan'} (on/off)"
        if key == "ai_enabled":
            self.bot.set_ai_enabled(arg.lower() == "on")
        else:
            self.s.update({key: arg.lower() == "on"})
        return f"{label}: {'✅ yoqildi' if arg.lower() == 'on' else '⏸ o‘chirildi'}"

    async def set_name(self, first: str, last: str = "") -> str:
        first, last = first.strip()[:64], last.strip()[:64]
        if not first:
            return "Ism bo'sh bo'lmasin: /name Ism Familiya"
        await self.client(functions.account.UpdateProfileRequest(first_name=first, last_name=last))
        self.bot.me = await self.client.get_me()
        return f"✅ Ism o'zgartirildi: {first} {last}".strip()

    async def set_photo(self, msg=None, data: bytes | None = None) -> str:
        if data is None:
            if msg is None or not getattr(msg, "photo", None):
                return "Rasmni /photo izohi bilan yuboring."
            data = await self.client.download_media(msg, file=bytes)
        uploaded = await self.client.upload_file(data, file_name="avatar.jpg")
        await self.client(functions.photos.UploadProfilePhotoRequest(file=uploaded))
        return "✅ Profil rasmi o'zgartirildi"

    def status_text(self) -> str:
        since = time.time() - self.s.report_interval_hours * 3600
        return (
            f"🤖 Avtojavob: {'yoqilgan ✅' if self.s.ai_enabled else 'o‘chirilgan ⏸'}\n"
            f"Model: {self.llm.last_model or self.s.openrouter_model}\n"
            f"API kalitlar: {len(self.s.openrouter_api_keys)}\n"
            f"Oxirgi {self.s.report_interval_hours:g} soatda yozganlar: "
            f"{self.store.count_events_since(since)}\n"
            f"Kanal e'lonlari: {'yoqilgan' if self.s.channel_posts_enabled else 'o‘chirilgan'}"
            f" ({self.s.channel or '-'} {self.s.post_time})"
        )
