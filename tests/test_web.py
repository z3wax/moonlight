import asyncio
from types import SimpleNamespace as NS

from aiohttp.test_utils import TestClient, TestServer

from autoreply.web.auth import Auth
from autoreply.web.server import create_app


class FakeTG:
    state = "logged_out"
    client = None

    def __init__(self):
        self.reconfigured = 0

    def status(self):
        return {"state": self.state, "state_text": "x", "account": None}

    async def reconfigure(self):
        self.reconfigured += 1


def make_ctx(settings, store, boot):
    llm = NS(key_status=lambda: [], last_model="", last_latency=0, last_error="", total_requests=0)
    owner = NS(owner_id=None)

    async def resolve():
        return None
    owner.resolve = resolve
    bot = NS(replies_sent=0, _pending={}, set_ai_enabled=lambda on: settings.update({"ai_enabled": on}))
    return NS(boot=boot, settings=settings, store=store, llm=llm, tg=FakeTG(), bot=bot, owner=owner,
              auth=Auth(store, boot), backup=None, log_buffer=[], started_at=0)


def run(coro):
    return asyncio.run(coro)


def test_panel_auth_csrf_and_settings(settings, store, boot):
    ctx = make_ctx(settings, store, boot)

    async def scenario():
        async with TestClient(TestServer(create_app(ctx))) as c:
            r = await c.get("/admin", allow_redirects=False)
            assert r.status == 302 and r.headers["Location"] == "/"
            assert (await c.get("/api/status")).status == 401

            r = await c.post("/api/login", json={"username": "abu", "password": "nope"})
            assert r.status == 401
            r = await c.post("/api/login", json={"username": "abu", "password": "test-password-123"})
            assert r.status == 200
            assert "HttpOnly" in r.headers["Set-Cookie"]
            assert "default-src 'self'" in r.headers["Content-Security-Policy"]

            me = await (await c.get("/api/me")).json()
            csrf = me["csrf"]

            # POST without the CSRF header is refused
            r = await c.post("/api/settings", json={"values": {"ai_enabled": False}})
            assert r.status == 403

            h = {"X-CSRF-Token": csrf}
            r = await c.post("/api/settings", json={"values": {"ai_enabled": False, "api_hash": ""}}, headers=h)
            data = await r.json()
            assert data["ok"] and settings.ai_enabled is False
            assert settings.api_hash == "abcdef"  # empty secret keeps old value

            r = await c.post("/api/settings", json={"values": {"api_id": 555}}, headers=h)
            assert (await r.json())["restart"] == ["api_id"] and ctx.tg.reconfigured == 1

            r = await c.post("/api/settings", json={"values": {"post_time": "99:99"}}, headers=h)
            assert r.status == 400

            data = await (await c.get("/api/settings")).json()
            assert "sk-or-test" not in str(data["values"])

            r = await c.post("/api/keys/delete", json={"index": 0}, headers=h)
            assert (await r.json())["ok"] and settings.openrouter_api_keys == []

            r = await c.get("/assets/admin.js")
            assert r.status == 200
            r = await c.get("/admin")
            assert r.status == 200 and "ai-master" in await r.text()

    run(scenario())


def test_login_lockout(settings, store, boot):
    ctx = make_ctx(settings, store, boot)

    async def scenario():
        async with TestClient(TestServer(create_app(ctx))) as c:
            for _ in range(5):
                await c.post("/api/login", json={"username": "abu", "password": "x"})
            r = await c.post("/api/login", json={"username": "abu", "password": "test-password-123"})
            assert r.status == 429

    run(scenario())


def test_proxy_secret_required_and_client_ip(settings, store, boot):
    boot.proxy_secret = "p-secret"
    ctx = make_ctx(settings, store, boot)

    async def scenario():
        async with TestClient(TestServer(create_app(ctx))) as c:
            r = await c.post("/api/login", json={"username": "abu", "password": "test-password-123"})
            assert r.status == 403  # direct access without the proxy secret
            h = {"X-Proxy-Secret": "p-secret", "X-Client-IP": "9.9.9.9"}
            for _ in range(5):
                await c.post("/api/login", json={"username": "abu", "password": "bad"}, headers=h)
            assert ctx.auth.locked_for("9.9.9.9") > 0  # lockout keyed on the real client
            # a spoofed X-Client-IP without the secret is ignored (request refused)
            r = await c.get("/api/me", headers={"X-Client-IP": "1.2.3.4"})
            assert r.status == 403
            assert (await c.get("/healthz")).status == 200

    run(scenario())


def test_ping_is_public_but_still_needs_proxy_secret(settings, store, boot):
    boot.proxy_secret = "p-secret"
    ctx = make_ctx(settings, store, boot)

    async def scenario():
        async with TestClient(TestServer(create_app(ctx))) as c:
            assert (await c.get("/api/ping")).status == 403
            r = await c.get("/api/ping", headers={"X-Proxy-Secret": "p-secret"})
            assert r.status == 200 and (await r.json())["ok"]

    run(scenario())
