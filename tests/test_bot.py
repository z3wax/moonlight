import asyncio
import contextlib
import time
from types import SimpleNamespace as NS

import pytest

from autoreply.bot import AutoReplyBot

ME = NS(id=1, first_name="Owner", last_name=None, username="owner", is_self=True)
VALI = NS(id=100, first_name="Vali", last_name=None, username="vali", bot=False,
          contact=True, scam=False, fake=False, support=False, is_self=False)


def message(id, text, out=False, sender=VALI):
    async def edit(new_text):
        message_obj.edited = new_text

    async def delete(revoke=False):
        message_obj.deleted = True

    message_obj = NS(id=id, raw_text=text, out=out, media=None, action=None,
                     fwd_from=None, sender=None if out else sender,
                     edit=edit, delete=delete, edited=None, deleted=False)
    return message_obj


class FakeClient:
    def __init__(self):
        self.chats = {}  # chat_id -> list of messages, oldest first
        self.sent = []
        self.next_id = 1000
        self.session = NS(save=lambda: None)

    def add_incoming(self, chat_id, text, sender=VALI):
        self.next_id += 1
        m = message(self.next_id, text, sender=sender)
        self.chats.setdefault(chat_id, []).append(m)
        return m

    async def get_messages(self, chat_id, limit=20):
        return list(reversed(self.chats.get(chat_id, [])[-limit:]))

    async def send_message(self, chat_id, text, link_preview=False, reply_to=None):
        self.next_id += 1
        m = message(self.next_id, text, out=True)
        if chat_id != "me":
            self.chats.setdefault(chat_id, []).append(m)
        self.sent.append((chat_id, text))
        return m

    async def send_read_acknowledge(self, chat_id, msg=None):
        return True

    def action(self, chat_id, action):
        @contextlib.asynccontextmanager
        async def cm():
            yield
        return cm()

    async def get_entity(self, chat_id):
        return VALI if chat_id == VALI.id else NS(id=chat_id, title="Group")

    async def disconnect(self):
        pass


class FakeLLM:
    def __init__(self, reply="Salom! Ali hozir band."):
        self.reply = reply
        self.calls = []

    async def chat(self, messages, model=None, **kw):
        self.calls.append((messages, model))
        return self.reply


def event_for(client, chat_id, msg, private=True, mentioned=False, sender=VALI):
    async def get_sender():
        return sender
    return NS(chat_id=chat_id, message=msg, is_private=private, is_channel=False,
              is_group=not private, mentioned=mentioned, get_sender=get_sender)


@pytest.fixture
def bot(settings, store):
    client = FakeClient()
    llm = FakeLLM()
    b = AutoReplyBot(client, settings, store, llm)
    b.me = ME
    b.OWN_MESSAGE_GRACE = 0.05
    return b


async def settle(bot):
    await asyncio.sleep(0.01)
    for _ in range(50):
        tasks = [t for t in bot._pending.values() if not t.done()]
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


def test_replies_to_private_message(bot):
    async def scenario():
        m = bot.client.add_incoming(VALI.id, "salom, qayerdasan?")
        await bot.on_incoming(event_for(bot.client, VALI.id, m))
        await settle(bot)

    asyncio.run(scenario())
    assert bot.client.sent == [(VALI.id, "Salom! Ali hozir band.")]
    messages, model = bot.llm.calls[0]
    assert messages[0]["role"] == "system" and "Ali" in messages[0]["content"]
    assert messages[-1] == {"role": "user", "content": "salom, qayerdasan?"}
    

def test_debounce_combines_quick_messages(bot):
    async def scenario():
        for text in ("salom", "ishlar qalay", "javob ber"):
            m = bot.client.add_incoming(VALI.id, text)
            await bot.on_incoming(event_for(bot.client, VALI.id, m))
        await settle(bot)

    asyncio.run(scenario())
    assert len(bot.llm.calls) == 1
    assert bot.llm.calls[0][0][-1]["content"] == "salom\nishlar qalay\njavob ber"
    assert len(bot.client.sent) == 1


def test_owner_takeover_silences_chat(bot):
    async def scenario():
        m = bot.client.add_incoming(VALI.id, "salom")
        await bot.on_incoming(event_for(bot.client, VALI.id, m))
        own = await FakeClient.send_message(bot.client, VALI.id, "men shu yerdaman")
        bot.client.sent.clear()
        await bot.on_outgoing(event_for(bot.client, VALI.id, own))
        await settle(bot)
        m2 = bot.client.add_incoming(VALI.id, "zo'r")
        await bot.on_incoming(event_for(bot.client, VALI.id, m2))
        await settle(bot)

    asyncio.run(scenario())
    assert bot.client.sent == []
    assert bot.store.owner_active_until(VALI.id) > time.time()


def test_bot_own_message_is_not_takeover(bot):
    async def scenario():
        m = bot.client.add_incoming(VALI.id, "salom")
        await bot.on_incoming(event_for(bot.client, VALI.id, m))
        await settle(bot)
        sent = bot.client.chats[VALI.id][-1]
        await bot.on_outgoing(event_for(bot.client, VALI.id, sent))

    asyncio.run(scenario())
    assert bot.store.owner_active_until(VALI.id) == 0


def test_ignores_service_bots_and_away_off(bot):
    service = NS(**{**VALI.__dict__, "id": 777000})
    robot = NS(**{**VALI.__dict__, "id": 555, "bot": True})

    async def scenario():
        for sender in (service, robot):
            m = bot.client.add_incoming(sender.id, "code 12345", sender=sender)
            await bot.on_incoming(event_for(bot.client, sender.id, m, sender=sender))
        bot.s.update({"ai_enabled": False})
        m = bot.client.add_incoming(VALI.id, "salom")
        await bot.on_incoming(event_for(bot.client, VALI.id, m))
        await settle(bot)

    asyncio.run(scenario())
    assert bot.llm.calls == []
    assert bot.client.sent == []


def test_groups_require_mention(bot, monkeypatch):
    bot.s.reply_in_groups = True

    async def scenario():
        m = bot.client.add_incoming(-50, "hammaga salom")
        await bot.on_incoming(event_for(bot.client, -50, m, private=False))
        await settle(bot)
        assert bot.client.sent == []
        m = bot.client.add_incoming(-50, "@owner sen-chi?")
        await bot.on_incoming(event_for(bot.client, -50, m, private=False, mentioned=True))
        await settle(bot)

    asyncio.run(scenario())
    assert len(bot.client.sent) == 1
    assert bot.llm.calls[0][0][-1]["content"].startswith("Vali (@vali): ")


def test_commands_in_saved_messages(bot):
    async def scenario():
        cmd = message(1, ".ai off", out=True)
        await bot.on_outgoing(event_for(bot.client, ME.id, cmd))
        assert "O'CHIRILDI" in cmd.edited
        assert bot.away is False

        cmd = message(2, ".ai model openai/gpt-4o", out=True)
        await bot.on_outgoing(event_for(bot.client, ME.id, cmd))
        assert bot.model == "openai/gpt-4o"

        cmd = message(3, ".ai on", out=True)
        await bot.on_outgoing(event_for(bot.client, ME.id, cmd))
        assert bot.away is True

    asyncio.run(scenario())


def test_command_in_chat_is_deleted_and_disables_chat(bot):
    async def scenario():
        cmd = message(5, ".ai stop", out=True)
        await bot.on_outgoing(event_for(bot.client, VALI.id, cmd))
        assert cmd.deleted
        assert bot.store.is_chat_disabled(VALI.id)
        m = bot.client.add_incoming(VALI.id, "salom")
        await bot.on_incoming(event_for(bot.client, VALI.id, m))
        await settle(bot)

    asyncio.run(scenario())
    assert [c for c, _ in bot.client.sent] == ["me"]


def test_away_notice_sent_once(bot):
    bot.s.away_notice_enabled = True

    async def scenario():
        for text in ("salom", "yana"):
            m = bot.client.add_incoming(VALI.id, text)
            await bot.on_incoming(event_for(bot.client, VALI.id, m))
            await settle(bot)

    asyncio.run(scenario())
    texts = [t for _, t in bot.client.sent]
    assert texts.count(bot.s.away_notice) == 1
    assert len(texts) == 3


class FakeOwner:
    def __init__(self, owner_id=777):
        self.owner_id = owner_id
        self.commands = []
        self.conversations = []

    def is_owner(self, sender):
        return getattr(sender, "id", None) == self.owner_id

    async def handle_command(self, event):
        self.commands.append(event.message.raw_text)

    async def on_conversation(self, chat, new_messages, text, images=None, urgent=None):
        self.conversations.append((chat.id, text, urgent))

    async def send(self, text, file=None):
        return True


def test_debt_message_gets_fixed_reply_and_owner_alert(bot):
    bot.owner = FakeOwner()

    async def scenario():
        m = bot.client.add_incoming(VALI.id, "qarzingizni qachon qaytarasiz?")
        await bot.on_incoming(event_for(bot.client, VALI.id, m))
        await settle(bot)
        await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert bot.llm.calls == []  # no LLM improvisation on debt topics
    assert "AI yordamchi" in bot.client.sent[0][1]
    assert bot.owner.conversations == [(VALI.id, "qarzingizni qachon qaytarasiz?", "debt")]


def test_second_account_messages_are_commands_not_chats(bot):
    bot.owner = FakeOwner(owner_id=VALI.id)

    async def scenario():
        m = bot.client.add_incoming(VALI.id, "/status")
        await bot.on_incoming(event_for(bot.client, VALI.id, m))
        await settle(bot)

    asyncio.run(scenario())
    assert bot.owner.commands == ["/status"]
    assert bot.client.sent == [] and bot.llm.calls == []


def test_summary_sent_after_reply(bot):
    bot.owner = FakeOwner()

    async def scenario():
        m = bot.client.add_incoming(VALI.id, "sizdan sayt buyurtma qilmoqchiman")
        await bot.on_incoming(event_for(bot.client, VALI.id, m))
        await settle(bot)
        await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert bot.owner.conversations == [(VALI.id, "sizdan sayt buyurtma qilmoqchiman", None)]


class FailingLLM(FakeLLM):
    async def chat(self, messages, model=None, **kw):
        from autoreply.llm import LLMError
        raise LLMError("hammasi limitda")


def test_fallback_reply_when_ai_is_down(bot):
    bot.llm = FailingLLM()

    async def scenario():
        m = bot.client.add_incoming(VALI.id, "salom")
        await bot.on_incoming(event_for(bot.client, VALI.id, m))
        await settle(bot)

    asyncio.run(scenario())
    texts = [t for c, t in bot.client.sent if c == VALI.id]
    assert texts == ["Hozir javob bera olmayapman. Ali qaytgach o'zi yozadi 🙏"]
