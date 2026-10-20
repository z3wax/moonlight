"""Admin panel backend (FastAPI): two pages (login, dashboard) + a JSON API."""

from __future__ import annotations

import hmac
import logging
import os
import platform
import resource
import shutil
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from ..config import FIELD_MAP, GROUPS, panel_values, schema_for_panel
from ..llm import LLMError

log = logging.getLogger(__name__)

PANEL = Path(__file__).resolve().parents[2] / "panel"  # shared with the Vercel deploy
COOKIE = "tgar_sid"
PUBLIC = {"/", "/login", "/api/login", "/api/ping", "/healthz"}
MAX_BODY = 10 * 1024 * 1024

CSP = (
    "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; frame-ancestors 'self' https://huggingface.co; base-uri 'none'; "
    "form-action 'self'"
)


class BadRequest(Exception):
    def __init__(self, message: str):
        self.message = message


def ctx_of(request: Request):
    return request.app.state.ctx


def from_trusted_proxy(request: Request) -> bool:
    secret = ctx_of(request).boot.proxy_secret
    given = request.headers.get("X-Proxy-Secret", "")
    return bool(secret) and hmac.compare_digest(given, secret)


def client_ip(request: Request) -> str:
    if from_trusted_proxy(request) and request.headers.get("X-Client-IP"):
        return request.headers["X-Client-IP"].strip()
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "?"


def is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("X-Forwarded-Proto", "") == "https"


def ok(data=None, **kw) -> JSONResponse:
    return JSONResponse({"ok": True, **(data or {}), **kw})


def fail(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


async def body(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        raise BadRequest("JSON kerak")
    if not isinstance(data, dict):
        raise BadRequest("JSON obyekt kerak")
    return data


def _secure(response: Response, request: Request) -> Response:
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.path.startswith("/api/") or request.url.path in {"/admin", "/login"}:
        response.headers["Cache-Control"] = "no-store"
    if is_https(request):
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


async def guard(request: Request, call_next):
    """Proxy secret, admin session, CSRF and security headers for every request."""
    ctx = ctx_of(request)
    path = request.url.path

    def done(response: Response) -> Response:
        return _secure(response, request)

    if int(request.headers.get("content-length") or 0) > MAX_BODY:
        return done(fail("So'rov juda katta", 413))
    # When the panel is served through the Vercel proxy, the API only answers
    # requests that carry the shared proxy secret.
    if (path.startswith("/api/") and ctx.boot.proxy_secret
            and not from_trusted_proxy(request) and not ctx.boot.allow_direct):
        return done(JSONResponse({"error": "proxy"}, status_code=403))
    if path not in PUBLIC and not path.startswith("/assets/"):
        session = ctx.auth.get(request.cookies.get(COOKIE))
        if session is None:
            if path.startswith("/api/"):
                return done(JSONResponse({"error": "auth"}, status_code=401))
            return done(RedirectResponse("/", status_code=302))
        if request.method not in {"GET", "HEAD"}:
            # CSRF: a per-session secret in a custom header, which a cross-site
            # page can neither read nor send (no CORS is allowed).
            token = request.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(token, session.csrf):
                return done(JSONResponse({"error": "csrf"}, status_code=403))
        request.state.session = session
    return done(await call_next(request))


def create_app(ctx) -> FastAPI:
    # No /docs or /openapi.json: the API is private to the panel.
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx
    app.middleware("http")(guard)

    @app.exception_handler(BadRequest)
    async def _bad_request(request: Request, exc: BadRequest):
        return fail(exc.message)

    # --- pages ---------------------------------------------------------------

    @app.get("/")
    async def index(request: Request):
        if ctx.auth.get(request.cookies.get(COOKIE)):
            return RedirectResponse("/admin", status_code=302)
        return FileResponse(PANEL / "index.html")

    @app.get("/login")
    async def login_page():
        return FileResponse(PANEL / "index.html")

    @app.get("/admin")
    async def admin_page():
        return FileResponse(PANEL / "admin.html")

    @app.get("/healthz")
    @app.get("/api/ping")  # Vercel cron keep-alive (through the proxy)
    async def healthz():
        return {"ok": True}

    app.mount("/assets", StaticFiles(directory=PANEL / "assets"), name="assets")

    # --- auth ----------------------------------------------------------------

    @app.post("/api/login")
    async def api_login(request: Request):
        ip = client_ip(request)
        locked = ctx.auth.locked_for(ip)
        if locked:
            return fail(f"Juda ko'p urinish. {locked // 60 + 1} daqiqadan keyin qayta urinib ko'ring.", 429)
        data = await body(request)
        session = ctx.auth.login(str(data.get("username", "")), str(data.get("password", "")), ip)
        if session is None:
            locked = ctx.auth.locked_for(ip)
            msg = "Login yoki parol noto'g'ri"
            if locked:
                msg = f"Juda ko'p urinish. {locked // 60 + 1} daqiqaga bloklandi."
            return fail(msg, 401)
        response = ok()
        secure = is_https(request)
        response.set_cookie(
            COOKIE, session.token, httponly=True, secure=secure, path="/",
            samesite="none" if secure else "strict", max_age=12 * 3600,
        )
        return response

    @app.post("/api/logout")
    async def api_logout(request: Request):
        ctx.auth.logout(request.cookies.get(COOKIE))
        response = ok()
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/api/me")
    async def api_me(request: Request):
        return ok(username=ctx.auth.username, csrf=request.state.session.csrf)

    @app.post("/api/password")
    async def api_password(request: Request):
        data = await body(request)
        try:
            ctx.auth.change_password(str(data.get("current", "")), str(data.get("new", "")),
                                     str(data.get("username", "")).strip() or None)
        except ValueError as exc:
            return fail(str(exc))
        response = ok()
        response.delete_cookie(COOKIE, path="/")
        return response

    # --- status --------------------------------------------------------------

    @app.get("/api/status")
    async def api_status():
        s = ctx.settings
        disk = shutil.disk_usage(s.data_dir)
        day_ago = time.time() - 86400
        try:
            load = os.getloadavg()[0]
        except OSError:
            load = None
        return ok(
            telegram=ctx.tg.status(),
            ai={
                "enabled": s.ai_enabled,
                "model": s.openrouter_model,
                "last_model": ctx.llm.last_model,
                "last_latency": round(ctx.llm.last_latency, 2),
                "last_error": ctx.llm.last_error,
                "requests": ctx.llm.total_requests,
                "keys": ctx.llm.key_status(),
            },
            activity={
                "replies_session": ctx.bot.replies_sent,
                "events_24h": ctx.store.count_events_since(day_ago),
                "debt_24h": ctx.store.count_events_since(day_ago, "debt"),
                "pending": len(ctx.bot._pending),
                "owner_id": ctx.owner.owner_id,
            },
            server={
                "uptime": time.time() - ctx.started_at,
                "python": platform.python_version(),
                "memory_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
                "load": load,
                "disk_free_gb": round(disk.free / 1e9, 2),
                "space": os.getenv("SPACE_ID", ""),
                "backup": ctx.backup.status() if ctx.backup else {"enabled": False},
            },
        )

    @app.get("/api/logs")
    async def api_logs():
        return ok(lines=list(ctx.log_buffer)[-300:])

    # --- settings ------------------------------------------------------------

    @app.get("/api/settings")
    async def api_settings():
        return ok(groups=GROUPS, schema=schema_for_panel(), values=panel_values(ctx.settings))

    @app.post("/api/settings")
    async def api_settings_save(request: Request):
        data = await body(request)
        changes = data.get("values") or {}
        if not isinstance(changes, dict):
            return fail("values kerak")
        clean = {}
        for key, value in changes.items():
            field = FIELD_MAP.get(key)
            if field is None:
                return fail(f"Noma'lum sozlama: {key}")
            if field.type == "secret_list":
                continue  # keys are managed through /api/keys
            if field.type == "secret" and value in (None, ""):
                continue  # empty secret input = keep the current value
            clean[key] = value
        try:
            restart = ctx.settings.update(clean)
        except ValueError as exc:
            return fail(str(exc))
        if "owner_account" in clean:
            await ctx.owner.resolve()
        if restart:
            await ctx.tg.reconfigure()
        return ok(restart=restart, values=panel_values(ctx.settings))

    @app.post("/api/ai/toggle")
    async def api_toggle_ai(request: Request):
        data = await body(request)
        ctx.bot.set_ai_enabled(bool(data.get("enabled")))
        return ok(enabled=ctx.settings.ai_enabled)

    @app.post("/api/keys/add")
    async def api_keys_add(request: Request):
        data = await body(request)
        key = str(data.get("key", "")).strip()
        if not key.startswith("sk-") or len(key) < 20:
            return fail("Kalit noto'g'ri ko'rinishda (sk-or-... bo'lishi kerak)")
        keys = list(ctx.settings.openrouter_api_keys)
        if key in keys:
            return fail("Bu kalit allaqachon bor")
        check = await ctx.llm.check_key(key)
        keys.append(key)
        ctx.settings.update({"openrouter_api_keys": keys})
        return ok(check=check, keys=ctx.llm.key_status())

    @app.post("/api/keys/delete")
    async def api_keys_delete(request: Request):
        data = await body(request)
        index = int(data.get("index", -1))
        keys = list(ctx.settings.openrouter_api_keys)
        if not 0 <= index < len(keys):
            return fail("Bunday kalit yo'q")
        keys.pop(index)
        ctx.settings.update({"openrouter_api_keys": keys})
        return ok(keys=ctx.llm.key_status())

    @app.post("/api/keys/move")
    async def api_keys_move(request: Request):
        data = await body(request)
        index, direction = int(data.get("index", -1)), int(data.get("direction", 0))
        keys = list(ctx.settings.openrouter_api_keys)
        target = index + direction
        if not (0 <= index < len(keys) and 0 <= target < len(keys)):
            return fail("Siljitib bo'lmaydi")
        keys[index], keys[target] = keys[target], keys[index]
        ctx.settings.update({"openrouter_api_keys": keys})
        return ok(keys=ctx.llm.key_status())

    @app.get("/api/models")
    async def api_models():
        try:
            models = await ctx.llm.list_models()
        except Exception as exc:  # noqa: BLE001
            return fail(f"Modellar ro'yxatini olib bo'lmadi: {exc}", 502)
        return ok(models=models)

    @app.post("/api/ai/test")
    async def api_ai_test(request: Request):
        data = await body(request)
        question = str(data.get("text") or "Salom! O'zingni qisqa tanishtir.")[:2000]
        started = time.monotonic()
        try:
            answer = await ctx.llm.chat([
                {"role": "system", "content": ctx.settings.system_prompt_rendered},
                {"role": "user", "content": question},
            ])
        except LLMError as exc:
            return fail(str(exc))
        return ok(answer=answer, model=ctx.llm.last_model,
                  seconds=round(time.monotonic() - started, 2))

    # --- telegram ------------------------------------------------------------

    async def tg_call(coro):
        try:
            return await coro
        except Exception as exc:  # noqa: BLE001 - surface Telegram errors to the panel
            log.warning("Telegram amali bajarilmadi: %r", exc)
            return exc

    @app.post("/api/telegram/send_code")
    async def api_tg_send_code(request: Request):
        data = await body(request)
        phone = str(data.get("phone", "")).strip()
        if not phone.startswith("+") or len(phone) < 8:
            return fail("Telefon raqamni +998... ko'rinishida kiriting")
        result = await tg_call(ctx.tg.send_code(phone))
        return fail(str(result)) if isinstance(result, Exception) else ok(state=ctx.tg.state)

    @app.post("/api/telegram/code")
    async def api_tg_code(request: Request):
        data = await body(request)
        result = await tg_call(ctx.tg.sign_in_code(str(data.get("code", ""))))
        return fail(str(result)) if isinstance(result, Exception) else ok(result=result)

    @app.post("/api/telegram/password")
    async def api_tg_password(request: Request):
        data = await body(request)
        result = await tg_call(ctx.tg.sign_in_password(str(data.get("password", ""))))
        return fail(str(result)) if isinstance(result, Exception) else ok(result=result)

    @app.post("/api/telegram/logout")
    async def api_tg_logout():
        await ctx.tg.logout()
        return ok()

    @app.post("/api/telegram/reconnect")
    async def api_tg_reconnect():
        await ctx.tg.reconfigure()
        return ok()

    @app.get("/api/telegram/sessions")
    async def api_tg_sessions():
        result = await tg_call(ctx.tg.authorizations())
        return fail(str(result)) if isinstance(result, Exception) else ok(sessions=result)

    @app.post("/api/telegram/terminate")
    async def api_tg_terminate(request: Request):
        data = await body(request)
        result = await tg_call(ctx.tg.terminate_authorization(str(data.get("hash", ""))))
        if isinstance(result, Exception):
            text = str(result)
            if "FRESH_RESET" in text:
                text = "Telegram yangi (24 soatdan yosh) sessiyadan boshqalarni o'chirishga ruxsat bermaydi."
            return fail(text)
        return ok()

    @app.post("/api/account/name")
    async def api_account_name(request: Request):
        data = await body(request)
        result = await tg_call(ctx.owner.set_name(str(data.get("first_name", "")),
                                                  str(data.get("last_name", ""))))
        return fail(str(result)) if isinstance(result, Exception) else ok(message=result)

    @app.get("/api/account/photo")
    async def api_account_photo_get():
        if ctx.bot.me is None or ctx.tg.client is None:
            return Response(status_code=404)
        data = await tg_call(ctx.tg.client.download_profile_photo("me", file=bytes))
        if not data or isinstance(data, Exception):
            return Response(status_code=404)
        return Response(content=data, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=60"})

    @app.post("/api/account/photo")
    async def api_account_photo_set(request: Request):
        form = await request.form()
        upload = form.get("photo")
        if upload is None or not hasattr(upload, "read"):
            return fail("Rasm tanlang")
        data = await upload.read()
        if len(data) > 8 * 1024 * 1024:
            return fail("Rasm 8 MB dan katta")
        if not (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"):
            return fail("Faqat JPG yoki PNG")
        result = await tg_call(ctx.owner.set_photo(data=bytes(data)))
        return fail(str(result)) if isinstance(result, Exception) else ok(message=result)

    @app.post("/api/owner/report")
    async def api_report_now():
        result = await tg_call(ctx.owner.send_report(force=True))
        if isinstance(result, Exception):
            return fail(str(result))
        return ok() if result else fail("Ikkinchi akkaunt sozlanmagan yoki topilmadi")

    @app.post("/api/owner/post")
    async def api_post_now():
        result = await tg_call(ctx.owner.post_now())
        if isinstance(result, Exception) or not result:
            return fail("E'lon yuborilmadi: kanal, mavzular va admin huquqini tekshiring")
        return ok()

    @app.post("/api/backup")
    async def api_backup_now():
        if not ctx.backup or not ctx.backup.enabled:
            return fail("Zaxira sozlanmagan (HF_TOKEN, STATE_REPO, SECRET_KEY)")
        return ok() if await ctx.backup.backup_now() else fail(ctx.backup.last_error)

    return app
