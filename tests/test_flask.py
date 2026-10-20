import asyncio
import json
import threading
import time
from types import SimpleNamespace as NS

import pytest

from autoreply.bridge import STATUS_KEY, Bridge
from autoreply.config import Settings
from autoreply.storage import Storage
from autoreply.web.flask_app import create_app


@pytest.fixture
def app_and_store(boot):
    boot.data_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(boot)
    app.testing = True
    store = Storage(boot.data_dir / "state.db")  # what worker.py would open
    Settings(boot, store).update({"api_hash": "abcdef", "openrouter_api_keys": ["sk-or-test-key-1234567890"]})
    return app, store


def login(c, password="test-password-123", **kw):
    return c.post("/api/login", json={"username": "abu", "password": password}, **kw)


def csrf_headers(c):
    return {"X-CSRF-Token": c.get("/api/me").get_json()["csrf"]}


class FakeWorker:
    """Plays worker.py: heartbeat + answers queued commands."""

    def __init__(self, store, answers):
        self.store, self.answers, self.seen = store, answers, []
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self.thread.join()

    def _run(self):
        while not self._stop.is_set():
            self.store.set(STATUS_KEY, json.dumps({
                "ts": time.time(),
                "telegram": {"state": "online", "state_text": "Ishlayapti", "account": None},
                "llm": {"last_model": "m", "last_latency": 0, "last_error": "", "requests": 1, "keys": []},
                "activity": {"replies_session": 3, "pending": 0, "owner_id": None},
                "server": {"uptime": 1, "python": "3", "memory_mb": 1, "load": None,
                           "backup": {"enabled": False}},
            }), notify=False)
            command = self.store.claim_command()
            if command:
                self.seen.append(command)
                self.store.finish_command(command["id"], self.answers.get(command["kind"], {"ok": True}))
            time.sleep(0.02)


def test_auth_csrf_proxy_and_settings(app_and_store, boot):
    app, store = app_and_store
    c = app.test_client()
    r = c.get("/admin")
    assert r.status_code == 302 and r.headers["Location"].endswith("/")
    assert c.get("/api/status").status_code == 401
    assert login(c, "bad").status_code == 401
    r = login(c)
    assert r.status_code == 200 and "HttpOnly" in r.headers["Set-Cookie"]
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert c.post("/api/settings", json={"values": {"ai_enabled": False}}).status_code == 403
    h = csrf_headers(c)
    r = c.post("/api/settings", json={"values": {"ai_enabled": False, "post_time": "9:05"}}, headers=h)
    assert r.get_json()["ok"]
    worker_view = Settings(boot, store)
    assert worker_view.ai_enabled is False and worker_view.post_time == "09:05"
    assert worker_view.version  # bumped, so worker.py reloads
    assert c.post("/api/settings", json={"values": {"post_time": "99:99"}}, headers=h).status_code == 400
    assert c.post("/api/settings", data="x", headers={**h, "Content-Type": "application/json"}).status_code == 400
    assert c.get("/assets/admin.js").status_code == 200


def test_proxy_secret_and_lockout(app_and_store, boot):
    boot.proxy_secret = "p"
    app, _ = app_and_store
    c = app.test_client()
    assert login(c).status_code == 403
    h = {"X-Proxy-Secret": "p", "X-Client-IP": "7.7.7.7"}
    for _ in range(5):
        login(c, "bad", headers=h)
    assert login(c, headers=h).status_code == 429
    assert c.get("/healthz").get_json()["ok"]


def test_worker_offline_is_reported(app_and_store):
    app, _ = app_and_store
    c = app.test_client()
    login(c)
    h = csrf_headers(c)
    status = c.get("/api/status").get_json()
    assert status["telegram"]["state"] == "worker_offline"
    assert status["ai"]["keys"] == [] and status["server"]["worker"] is False
    r = c.post("/api/telegram/send_code", json={"phone": "+998901234567"}, headers=h)
    assert r.status_code == 400 and "worker.py" in r.get_json()["error"]


def test_commands_go_through_worker(app_and_store):
    app, store = app_and_store
    c = app.test_client()
    login(c)
    h = csrf_headers(c)
    answers = {"send_code": {"ok": True, "state": "code_sent"},
               "sessions": {"ok": False, "error": "FLOOD"}}
    with FakeWorker(store, answers) as worker:
        time.sleep(0.1)
        status = c.get("/api/status").get_json()
        assert status["telegram"]["state"] == "online" and status["activity"]["replies_session"] == 3
        r = c.post("/api/telegram/send_code", json={"phone": "+998901234567"}, headers=h)
        assert r.get_json() == {"ok": True, "state": "code_sent"}
        r = c.get("/api/telegram/sessions")
        assert r.status_code == 400 and r.get_json()["error"] == "FLOOD"
    assert worker.seen[0]["payload"] == {"phone": "+998901234567"}


def test_bridge_executes_and_reloads_settings(boot):
    store = Storage(":memory:")
    settings = Settings(boot, store)
    calls = []

    async def set_name(first, last):
        calls.append((first, last))
        return "ok"

    async def reconfigure():
        calls.append("reconfigure")

    tg = NS(state="online", status=lambda: {"state": "online"}, reconfigure=reconfigure)
    llm = NS(last_model="", last_latency=0.0, last_error="", total_requests=0, key_status=lambda: [])
    ctx = NS(settings=settings, store=store, tg=tg, llm=llm, backup=None, started_at=time.time(),
             bot=NS(replies_sent=0, _pending={}), owner=NS(owner_id=None, set_name=set_name))
    bridge = Bridge(ctx)

    async def scenario():
        cid = store.enqueue_command("name", {"first_name": "Ali", "last_name": "V"})
        bad = store.enqueue_command("nope")
        task = asyncio.create_task(bridge.run())
        # the web app (another process) saves a restart-needing setting
        Settings(boot, store).update({"api_id": 4242})
        for _ in range(60):
            await asyncio.sleep(0.05)
            if store.command_result(bad) and "reconfigure" in calls:
                break
        task.cancel()
        return store.command_result(cid), store.command_result(bad)

    good, bad = asyncio.run(scenario())
    assert good == {"ok": True, "message": "ok"} and ("Ali", "V") in calls
    assert bad["ok"] is False and "Noma'lum" in bad["error"]
    assert settings.api_id == 4242 and "reconfigure" in calls
    assert json.loads(store.get(STATUS_KEY))["telegram"] == {"state": "online"}
