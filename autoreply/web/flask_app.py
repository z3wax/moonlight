"""Admin panel backend for WSGI hosts (Flask), e.g. PythonAnywhere "Web".

Same pages, API, cookies, proxy secret and CSRF rules as the FastAPI server.
The Telegram bot itself runs in ``worker.py`` (a separate always-on process):
everything that needs the live Telegram connection is sent to it through the
shared SQLite database (see ``autoreply/bridge.py``).
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import shutil
import time
from collections import deque
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, request, send_from_directory

from ..bridge import STATUS_INTERVAL, STATUS_KEY
from ..config import FIELD_MAP, GROUPS, Bootstrap, Settings, panel_values, schema_for_panel
from ..storage import Storage
from .auth import Auth

log = logging.getLogger(__name__)

PANEL = Path(__file__).resolve().parents[2] / "panel"
COOKIE = "tgar_sid"
PUBLIC = {"/", "/login", "/api/login", "/api/ping", "/healthz"}
WORKER_TIMEOUT = 25.0
WORKER_STALE = STATUS_INTERVAL * 10

CSP = (
    "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


def ok(data=None, **kw):
    return jsonify({"ok": True, **(data or {}), **kw})


def fail(message: str, status: int = 400):
    response = jsonify({"ok": False, "error": message})
    response.status_code = status
    return response


def create_app(boot: Bootstrap | None = None) -> Flask:
    boot = boot or Bootstrap.from_env()
    store = Storage(boot.data_dir / "state.db")
    settings = Settings(boot, store)
    auth = Auth(store, boot)

    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

    # --- helpers ---------------------------------------------------------

    def from_trusted_proxy() -> bool:
        given = request.headers.get("X-Proxy-Secret", "")
        return bool(boot.proxy_secret) and hmac.compare_digest(given, boot.proxy_secret)

    def client_ip() -> str:
        if from_trusted_proxy() and request.headers.get("X-Client-IP"):
            return request.headers["X-Client-IP"].strip()
        for header in ("X-Real-IP", "X-Forwarded-For"):
            if request.headers.get(header):
                return request.headers[header].split(",")[0].strip()
        return request.remote_addr or "?"

    def is_https() -> bool:
        return request.is_secure or request.headers.get("X-Forwarded-Proto", "") == "https"

    def body() -> dict:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise BadRequest("JSON kerak")
        return data

    def worker_status() -> dict | None:
        raw = store.get(STATUS_KEY)
        if not raw:
            return None
        status = json.loads(raw)
        if time.time() - status.get("ts", 0) > WORKER_STALE:
            return None
        return status

    def call_worker(kind: str, payload: dict | None = None, timeout: float = WORKER_TIMEOUT):
        """Queue a command for worker.py and wait for its answer."""
        if worker_status() is None:
            return None, "Bot jarayoni (worker.py) ishlamayapti. Always-on task'ni tekshiring."
        command_id = store.enqueue_command(kind, payload)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = store.command_result(command_id)
            if result is not None:
                if not result.pop("ok", False):
                    return None, result.get("error") or "Xatolik"
                return result, None
            time.sleep(0.25)
        store.abandon_command(command_id)
        return None, "Bot jarayoni javob bermadi (vaqt tugadi)"

    def via_worker(kind: str, payload: dict | None = None, **extra):
        result, error = call_worker(kind, payload, **extra)
        return fail(error) if error else ok(result)

    class BadRequest(Exception):
        pass

    @app.errorhandler(BadRequest)
    def _bad_request(exc):
        return fail(str(exc))

    @app.errorhandler(413)
    def _too_large(_exc):
        return fail("So'rov juda katta", 413)

    # --- guard + headers -------------------------------------------------

    @app.before_request
    def guard():
        path = request.path
        if (path.startswith("/api/") and boot.proxy_secret
                and not from_trusted_proxy() and not boot.allow_direct):
            return jsonify({"error": "proxy"}), 403
        settings.reload()  # the worker or another WSGI process may have saved changes
        if path in PUBLIC or path.startswith("/assets/"):
            return None
        session = auth.get(request.cookies.get(COOKIE))
        if session is None:
            if path.startswith("/api/"):
                return jsonify({"error": "auth"}), 401
            return redirect("/")
        if request.method not in {"GET", "HEAD"}:
            token = request.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(token, session.csrf):
                return jsonify({"error": "csrf"}), 403
        request.environ["autoreply.session"] = session
        return None

    @app.after_request
    def security_headers(response: Response):
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.path.startswith("/api/") or request.path in {"/admin", "/login"}:
            response.headers["Cache-Control"] = "no-store"
        if is_https():
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    # --- pages -----------------------------------------------------------

    @app.get("/")
    def index():
        if auth.get(request.cookies.get(COOKIE)):
            return redirect("/admin")
        return send_from_directory(PANEL, "index.html")

    @app.get("/login")
    def login_page():
        return send_from_directory(PANEL, "index.html")

    @app.get("/admin")
    def admin_page():
        return send_from_directory(PANEL, "admin.html")

    @app.get("/assets/<path:name>")
    def assets(name):
        return send_from_directory(PANEL / "assets", name)

    @app.get("/healthz")
    @app.get("/api/ping")
    def healthz():
        return jsonify({"ok": True, "worker": worker_status() is not None})

    # --- auth ------------------------------------------------------------

    @app.post("/api/login")
    def api_login():
        ip = client_ip()
        locked = auth.locked_for(ip)
        if locked:
            return fail(f"Juda ko'p urinish. {locked // 60 + 1} daqiqadan keyin qayta urinib ko'ring.", 429)
        data = body()
        session = auth.login(str(data.get("username", "")), str(data.get("password", "")), ip)
        if session is None:
            locked = auth.locked_for(ip)
            msg = "Login yoki parol noto'g'ri"
            if locked:
                msg = f"Juda ko'p urinish. {locked // 60 + 1} daqiqaga bloklandi."
            return fail(msg, 401)
        response = ok()
        secure = is_https()
        response.set_cookie(COOKIE, session.token, httponly=True, secure=secure, path="/",
                            samesite="None" if secure else "Strict", max_age=12 * 3600)
        return response

    @app.post("/api/logout")
    def api_logout():
        auth.logout(request.cookies.get(COOKIE))
        response = ok()
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/api/me")
    def api_me():
        return ok(username=auth.username, csrf=request.environ["autoreply.session"].csrf)

    @app.post("/api/password")
    def api_password():
        data = body()
        try:
            auth.change_password(str(data.get("current", "")), str(data.get("new", "")),
                                 str(data.get("username", "")).strip() or None)
        except ValueError as exc:
            return fail(str(exc))
        response = ok()
        response.delete_cookie(COOKIE, path="/")
        return response

    # --- status ----------------------------------------------------------

    @app.get("/api/status")
    def api_status():
        live = worker_status()
        day_ago = time.time() - 86400
        disk = shutil.disk_usage(boot.data_dir)
        if live is None:
            telegram = {"state": "worker_offline",
                        "state_text": "Bot jarayoni ishlamayapti (worker.py)",
                        "state_since": 0, "last_error": "", "connected_at": 0,
                        "last_update_at": 0, "session_file": False, "session_backup": False,
                        "dc": None, "device": settings.device_model, "phone_hint": "",
                        "account": None}
            llm = {"last_model": "", "last_latency": 0, "last_error": "", "requests": 0, "keys": []}
            activity = {"replies_session": 0, "pending": 0, "owner_id": None}
            server = {"uptime": 0, "python": "", "memory_mb": 0, "load": None,
                      "backup": {"enabled": False}}
        else:
            telegram, llm = live["telegram"], live["llm"]
            activity, server = live["activity"], live["server"]
        return ok(
            telegram=telegram,
            ai={"enabled": settings.ai_enabled, "model": settings.openrouter_model, **llm},
            activity={**activity,
                      "events_24h": store.count_events_since(day_ago),
                      "debt_24h": store.count_events_since(day_ago, "debt")},
            server={**server, "disk_free_gb": round(disk.free / 1e9, 2), "space": "",
                    "worker": live is not None},
        )

    @app.get("/api/logs")
    def api_logs():
        path = boot.data_dir / "bot.log"
        lines: deque = deque(maxlen=300)
        if path.exists():
            with path.open(encoding="utf-8", errors="replace") as fh:
                lines.extend(line.rstrip("\n") for line in fh)
        return ok(lines=list(lines))

    # --- settings --------------------------------------------------------

    @app.get("/api/settings")
    def api_settings():
        return ok(groups=GROUPS, schema=schema_for_panel(), values=panel_values(settings))

    @app.post("/api/settings")
    def api_settings_save():
        changes = body().get("values") or {}
        if not isinstance(changes, dict):
            return fail("values kerak")
        clean = {}
        for key, value in changes.items():
            field = FIELD_MAP.get(key)
            if field is None:
                return fail(f"Noma'lum sozlama: {key}")
            if field.type == "secret_list":
                continue
            if field.type == "secret" and value in (None, ""):
                continue
            clean[key] = value
        try:
            restart = settings.update(clean)  # the worker picks it up within a second
        except ValueError as exc:
            return fail(str(exc))
        return ok(restart=restart, values=panel_values(settings))

    @app.post("/api/ai/toggle")
    def api_toggle_ai():
        settings.update({"ai_enabled": bool(body().get("enabled"))})
        return ok(enabled=settings.ai_enabled)

    def key_list_response():
        live = worker_status()
        if live is not None:
            return live["llm"]["keys"]
        from ..llm import mask_key

        return [{"key": mask_key(k), "status": "ok", "cooldown_seconds": 0, "last_error": "",
                 "last_ok": 0, "successes": 0, "failures": 0}
                for k in settings.openrouter_api_keys]

    @app.post("/api/keys/add")
    def api_keys_add():
        key = str(body().get("key", "")).strip()
        if not key.startswith("sk-") or len(key) < 20:
            return fail("Kalit noto'g'ri ko'rinishda (sk-or-... bo'lishi kerak)")
        keys = list(settings.openrouter_api_keys)
        if key in keys:
            return fail("Bu kalit allaqachon bor")
        result, error = call_worker("key_check", {"key": key})
        check = result["check"] if result else {"ok": False, "error": error}
        keys.append(key)
        settings.update({"openrouter_api_keys": keys})
        return ok(check=check, keys=key_list_response())

    @app.post("/api/keys/delete")
    def api_keys_delete():
        index = int(body().get("index", -1))
        keys = list(settings.openrouter_api_keys)
        if not 0 <= index < len(keys):
            return fail("Bunday kalit yo'q")
        keys.pop(index)
        settings.update({"openrouter_api_keys": keys})
        return ok(keys=key_list_response())

    @app.post("/api/keys/move")
    def api_keys_move():
        data = body()
        index, direction = int(data.get("index", -1)), int(data.get("direction", 0))
        keys = list(settings.openrouter_api_keys)
        target = index + direction
        if not (0 <= index < len(keys) and 0 <= target < len(keys)):
            return fail("Siljitib bo'lmaydi")
        keys[index], keys[target] = keys[target], keys[index]
        settings.update({"openrouter_api_keys": keys})
        return ok(keys=key_list_response())

    @app.get("/api/models")
    def api_models():
        return via_worker("models")

    @app.post("/api/ai/test")
    def api_ai_test():
        return via_worker("ai_test", {"text": body().get("text")}, timeout=120)

    # --- telegram (through the worker) ----------------------------------

    @app.post("/api/telegram/send_code")
    def api_tg_send_code():
        phone = str(body().get("phone", "")).strip()
        if not phone.startswith("+") or len(phone) < 8:
            return fail("Telefon raqamni +998... ko'rinishida kiriting")
        return via_worker("send_code", {"phone": phone})

    @app.post("/api/telegram/code")
    def api_tg_code():
        return via_worker("code", {"code": str(body().get("code", ""))})

    @app.post("/api/telegram/password")
    def api_tg_password():
        return via_worker("password", {"password": str(body().get("password", ""))})

    @app.post("/api/telegram/logout")
    def api_tg_logout():
        return via_worker("logout")

    @app.post("/api/telegram/reconnect")
    def api_tg_reconnect():
        return via_worker("reconnect")

    @app.get("/api/telegram/sessions")
    def api_tg_sessions():
        return via_worker("sessions")

    @app.post("/api/telegram/terminate")
    def api_tg_terminate():
        return via_worker("terminate", {"hash": str(body().get("hash", ""))})

    @app.post("/api/account/name")
    def api_account_name():
        data = body()
        return via_worker("name", {"first_name": str(data.get("first_name", "")),
                                   "last_name": str(data.get("last_name", ""))})

    @app.get("/api/account/photo")
    def api_account_photo_get():
        result, error = call_worker("photo_get", timeout=15)
        if error or not result.get("data"):
            return Response(status=404)
        return Response(base64.b64decode(result["data"]), mimetype="image/jpeg",
                        headers={"Cache-Control": "private, max-age=60"})

    @app.post("/api/account/photo")
    def api_account_photo_set():
        upload = request.files.get("photo")
        if upload is None:
            return fail("Rasm tanlang")
        data = upload.read()
        if len(data) > 8 * 1024 * 1024:
            return fail("Rasm 8 MB dan katta")
        if not (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"):
            return fail("Faqat JPG yoki PNG")
        return via_worker("photo_set", {"data": base64.b64encode(data).decode()})

    @app.post("/api/owner/report")
    def api_report_now():
        result, error = call_worker("report")
        if error:
            return fail(error)
        return ok() if result.get("sent") else fail("Ikkinchi akkaunt sozlanmagan yoki topilmadi")

    @app.post("/api/owner/post")
    def api_post_now():
        result, error = call_worker("post", timeout=90)
        if error or not result.get("sent"):
            return fail(error or "E'lon yuborilmadi: kanal, mavzular va admin huquqini tekshiring")
        return ok()

    @app.post("/api/backup")
    def api_backup_now():
        return via_worker("backup", timeout=90)

    return app
