from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

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
    bot = NS(replies_sent=0, _pending={}, me=None,
             set_ai_enabled=lambda on: settings.update({"ai_enabled": on}))
    return NS(boot=boot, settings=settings, store=store, llm=llm, tg=FakeTG(), bot=bot, owner=owner,
              auth=Auth(store, boot), backup=None, log_buffer=[], started_at=0)


def client_for(ctx):
    return TestClient(create_app(ctx))


def login(c, password="test-password-123", **kw):
    return c.post("/api/login", json={"username": "abu", "password": password}, **kw)


def test_panel_auth_csrf_and_settings(settings, store, boot):
    ctx = make_ctx(settings, store, boot)
    with client_for(ctx) as c:
        r = c.get("/admin", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/"
        assert c.get("/api/status").status_code == 401

        assert login(c, "nope").status_code == 401
        r = login(c)
        assert r.status_code == 200
        assert "httponly" in r.headers["set-cookie"].lower()
        assert "default-src 'self'" in r.headers["content-security-policy"]

        csrf = c.get("/api/me").json()["csrf"]

        # POST without the CSRF header is refused
        r = c.post("/api/settings", json={"values": {"ai_enabled": False}})
        assert r.status_code == 403

        h = {"X-CSRF-Token": csrf}
        r = c.post("/api/settings", json={"values": {"ai_enabled": False, "api_hash": ""}}, headers=h)
        assert r.json()["ok"] and settings.ai_enabled is False
        assert settings.api_hash == "abcdef"  # empty secret keeps old value

        r = c.post("/api/settings", json={"values": {"api_id": 555}}, headers=h)
        assert r.json()["restart"] == ["api_id"] and ctx.tg.reconfigured == 1

        r = c.post("/api/settings", json={"values": {"post_time": "99:99"}}, headers=h)
        assert r.status_code == 400

        r = c.post("/api/settings", content=b"not json", headers=h)
        assert r.status_code == 400 and r.json()["error"] == "JSON kerak"

        data = c.get("/api/settings").json()
        assert "sk-or-test" not in str(data["values"])

        r = c.post("/api/keys/delete", json={"index": 0}, headers=h)
        assert r.json()["ok"] and settings.openrouter_api_keys == []

        assert c.get("/assets/admin.js").status_code == 200
        r = c.get("/admin")
        assert r.status_code == 200 and "ai-master" in r.text

        # no public API docs
        assert c.get("/docs").status_code in (302, 404)
        assert c.get("/openapi.json").status_code in (302, 404)


def test_login_lockout(settings, store, boot):
    ctx = make_ctx(settings, store, boot)
    with client_for(ctx) as c:
        for _ in range(5):
            login(c, "x")
        assert login(c).status_code == 429


def test_proxy_secret_required_and_client_ip(settings, store, boot):
    boot.proxy_secret = "p-secret"
    ctx = make_ctx(settings, store, boot)
    with client_for(ctx) as c:
        assert login(c).status_code == 403  # direct access without the proxy secret
        h = {"X-Proxy-Secret": "p-secret", "X-Client-IP": "9.9.9.9"}
        for _ in range(5):
            login(c, "bad", headers=h)
        assert ctx.auth.locked_for("9.9.9.9") > 0  # lockout keyed on the real client
        # a spoofed X-Client-IP without the secret is ignored (request refused)
        assert c.get("/api/me", headers={"X-Client-IP": "1.2.3.4"}).status_code == 403
        assert c.get("/healthz").json() == {"ok": True}


def test_ping_is_public_but_still_needs_proxy_secret(settings, store, boot):
    boot.proxy_secret = "p-secret"
    ctx = make_ctx(settings, store, boot)
    with client_for(ctx) as c:
        assert c.get("/api/ping").status_code == 403
        r = c.get("/api/ping", headers={"X-Proxy-Secret": "p-secret"})
        assert r.status_code == 200 and r.json()["ok"]


def test_photo_upload_validates_type(settings, store, boot):
    ctx = make_ctx(settings, store, boot)
    with client_for(ctx) as c:
        login(c)
        h = {"X-CSRF-Token": c.get("/api/me").json()["csrf"]}
        r = c.post("/api/account/photo", files={"photo": ("a.txt", b"hello", "text/plain")}, headers=h)
        assert r.status_code == 400 and r.json()["error"] == "Faqat JPG yoki PNG"
        assert c.get("/api/account/photo").status_code == 404


def test_unicode_admin_login_returns_http_responses(settings, store, boot):
    boot.admin_username = "Юсуф"
    ctx = make_ctx(settings, store, boot)
    with client_for(ctx) as c:
        assert c.post("/api/login", json={"username": "Бошқа", "password": "test-password-123"}).status_code == 401
        response = c.post("/api/login", json={"username": "ЮСУФ", "password": "test-password-123"})
        assert response.status_code == 200
        assert c.get("/api/me").json()["username"] == "Юсуф"
