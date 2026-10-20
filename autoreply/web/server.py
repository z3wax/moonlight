"""Admin panel: two pages (login, dashboard) + a JSON API."""

from __future__ import annotations

import hmac
import logging
import os
import platform
import resource
import shutil
import time
from pathlib import Path

from aiohttp import web

from ..config import FIELD_MAP, GROUPS, panel_values, schema_for_panel
from ..llm import LLMError
from .auth import Auth

log = logging.getLogger(__name__)

PANEL = Path(__file__).resolve().parents[2] / "panel"  # shared with the Vercel deploy
COOKIE = "tgar_sid"
PUBLIC = {"/", "/login", "/api/login", "/api/ping", "/healthz"}
CTX = web.AppKey("ctx", object)
SESSION = web.RequestKey("session", object) if hasattr(web, "RequestKey") else "session"

CSP = (
    "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; frame-ancestors 'self' https://huggingface.co; base-uri 'none'; "
    "form-action 'self'"
)


def from_trusted_proxy(request: web.Request) -> bool:
    secret = request.app[CTX].boot.proxy_secret
    given = request.headers.get("X-Proxy-Secret", "")
    return bool(secret) and hmac.compare_digest(given, secret)


def client_ip(request: web.Request) -> str:
    if from_trusted_proxy(request) and request.headers.get("X-Client-IP"):
        return request.headers["X-Client-IP"].strip()
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote or "?"


def is_https(request: web.Request) -> bool:
    return request.secure or request.headers.get("X-Forwarded-Proto", "") == "https"


@web.middleware
async def security_headers(request: web.Request, handler):
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        response = exc
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.path.startswith("/api/") or request.path in {"/admin", "/login"}:
        response.headers["Cache-Control"] = "no-store"
    if is_https(request):
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    if isinstance(response, web.HTTPException):
        raise response
    return response


@web.middleware
async def require_auth(request: web.Request, handler):
    # When the panel is served through the Vercel proxy, the API only answers
    # requests that carry the shared proxy secret.
    if (request.path.startswith("/api/") and request.app[CTX].boot.proxy_secret
            and not from_trusted_proxy(request) and not request.app[CTX].boot.allow_direct):
        return web.json_response({"error": "proxy"}, status=403)
    if request.path in PUBLIC or request.path.startswith("/assets/"):
        return await handler(request)
    auth: Auth = request.app[CTX].auth
    session = auth.get(request.cookies.get(COOKIE))
    if session is None:
        if request.path.startswith("/api/"):
            return web.json_response({"error": "auth"}, status=401)
        raise web.HTTPFound("/")
    if request.method not in {"GET", "HEAD"}:
        # CSRF: a per-session secret in a custom header, which a cross-site
        # page can neither read nor send (no CORS is allowed).
        token = request.headers.get("X-CSRF-Token", "")
        if not hmac.compare_digest(token, session.csrf):
            return web.json_response({"error": "csrf"}, status=403)
    request[SESSION] = session
    return await handler(request)


def ok(data=None, **kw):
    return web.json_response({"ok": True, **(data or {}), **kw})


def fail(message: str, status: int = 400):
    return web.json_response({"ok": False, "error": message}, status=status)


async def body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        raise web.HTTPBadRequest(text="JSON kerak")
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="JSON obyekt kerak")
    return data


# --- pages -------------------------------------------------------------------

async def index(request):
    session = request.app[CTX].auth.get(request.cookies.get(COOKIE))
    if session:
        raise web.HTTPFound("/admin")
    return web.FileResponse(PANEL / "index.html")


async def login_page(request):
    return web.FileResponse(PANEL / "index.html")


async def admin_page(request):
    return web.FileResponse(PANEL / "admin.html")


async def healthz(request):
    return web.json_response({"ok": True})


# --- auth --------------------------------------------------------------------

async def api_login(request):
    ctx = request.app[CTX]
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
        samesite="None" if secure else "Strict", max_age=12 * 3600,
    )
    return response


async def api_logout(request):
    ctx = request.app[CTX]
    ctx.auth.logout(request.cookies.get(COOKIE))
    response = ok()
    response.del_cookie(COOKIE, path="/")
    return response


async def api_me(request):
    ctx = request.app[CTX]
    return ok(username=ctx.auth.username, csrf=request[SESSION].csrf)


async def api_password(request):
    ctx = request.app[CTX]
    data = await body(request)
    try:
        ctx.auth.change_password(str(data.get("current", "")), str(data.get("new", "")),
                                 str(data.get("username", "")).strip() or None)
    except ValueError as exc:
        return fail(str(exc))
    response = ok()
    response.del_cookie(COOKIE, path="/")
    return response


# --- status ------------------------------------------------------------------

def _memory_mb() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(usage / 1024, 1)  # Linux: KiB


async def api_status(request):
    ctx = request.app[CTX]
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
            "memory_mb": _memory_mb(),
            "load": load,
            "disk_free_gb": round(disk.free / 1e9, 2),
            "space": os.getenv("SPACE_ID", ""),
            "backup": ctx.backup.status() if ctx.backup else {"enabled": False},
        },
    )


async def api_logs(request):
    ctx = request.app[CTX]
    return ok(lines=list(ctx.log_buffer)[-300:])


# --- settings ----------------------------------------------------------------

async def api_settings(request):
    ctx = request.app[CTX]
    return ok(groups=GROUPS, schema=schema_for_panel(), values=panel_values(ctx.settings))


async def api_settings_save(request):
    ctx = request.app[CTX]
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


async def api_toggle_ai(request):
    ctx = request.app[CTX]
    data = await body(request)
    ctx.bot.set_ai_enabled(bool(data.get("enabled")))
    return ok(enabled=ctx.settings.ai_enabled)


async def api_keys_add(request):
    ctx = request.app[CTX]
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


async def api_keys_delete(request):
    ctx = request.app[CTX]
    data = await body(request)
    index = int(data.get("index", -1))
    keys = list(ctx.settings.openrouter_api_keys)
    if not 0 <= index < len(keys):
        return fail("Bunday kalit yo'q")
    keys.pop(index)
    ctx.settings.update({"openrouter_api_keys": keys})
    return ok(keys=ctx.llm.key_status())


async def api_keys_move(request):
    ctx = request.app[CTX]
    data = await body(request)
    index, direction = int(data.get("index", -1)), int(data.get("direction", 0))
    keys = list(ctx.settings.openrouter_api_keys)
    target = index + direction
    if not (0 <= index < len(keys) and 0 <= target < len(keys)):
        return fail("Siljitib bo'lmaydi")
    keys[index], keys[target] = keys[target], keys[index]
    ctx.settings.update({"openrouter_api_keys": keys})
    return ok(keys=ctx.llm.key_status())


async def api_models(request):
    ctx = request.app[CTX]
    try:
        models = await ctx.llm.list_models()
    except Exception as exc:  # noqa: BLE001
        return fail(f"Modellar ro'yxatini olib bo'lmadi: {exc}", 502)
    return ok(models=models)


async def api_ai_test(request):
    ctx = request.app[CTX]
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
    return ok(answer=answer, model=ctx.llm.last_model, seconds=round(time.monotonic() - started, 2))


# --- telegram ----------------------------------------------------------------

async def _tg_call(coro):
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001 - surface Telegram errors to the panel
        log.warning("Telegram amali bajarilmadi: %r", exc)
        return exc


async def api_tg_send_code(request):
    ctx = request.app[CTX]
    data = await body(request)
    phone = str(data.get("phone", "")).strip()
    if not phone.startswith("+") or len(phone) < 8:
        return fail("Telefon raqamni +998... ko'rinishida kiriting")
    result = await _tg_call(ctx.tg.send_code(phone))
    return fail(str(result)) if isinstance(result, Exception) else ok(state=ctx.tg.state)


async def api_tg_code(request):
    ctx = request.app[CTX]
    data = await body(request)
    result = await _tg_call(ctx.tg.sign_in_code(str(data.get("code", ""))))
    return fail(str(result)) if isinstance(result, Exception) else ok(result=result)


async def api_tg_password(request):
    ctx = request.app[CTX]
    data = await body(request)
    result = await _tg_call(ctx.tg.sign_in_password(str(data.get("password", ""))))
    return fail(str(result)) if isinstance(result, Exception) else ok(result=result)


async def api_tg_logout(request):
    ctx = request.app[CTX]
    await ctx.tg.logout()
    return ok()


async def api_tg_reconnect(request):
    ctx = request.app[CTX]
    await ctx.tg.reconfigure()
    return ok()


async def api_tg_sessions(request):
    ctx = request.app[CTX]
    result = await _tg_call(ctx.tg.authorizations())
    return fail(str(result)) if isinstance(result, Exception) else ok(sessions=result)


async def api_tg_terminate(request):
    ctx = request.app[CTX]
    data = await body(request)
    result = await _tg_call(ctx.tg.terminate_authorization(str(data.get("hash", ""))))
    if isinstance(result, Exception):
        text = str(result)
        if "FRESH_RESET" in text:
            text = "Telegram yangi (24 soatdan yosh) sessiyadan boshqalarni o'chirishga ruxsat bermaydi."
        return fail(text)
    return ok()


async def api_account_name(request):
    ctx = request.app[CTX]
    data = await body(request)
    result = await _tg_call(ctx.owner.set_name(str(data.get("first_name", "")),
                                               str(data.get("last_name", ""))))
    return fail(str(result)) if isinstance(result, Exception) else ok(message=result)


async def api_account_photo(request):
    ctx = request.app[CTX]
    if request.method == "GET":
        if ctx.bot.me is None:
            raise web.HTTPNotFound()
        data = await _tg_call(ctx.tg.client.download_profile_photo("me", file=bytes))
        if not data or isinstance(data, Exception):
            raise web.HTTPNotFound()
        return web.Response(body=data, content_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=60"})
    reader = await request.multipart()
    part = await reader.next()
    if part is None or part.name != "photo":
        return fail("Rasm tanlang")
    data = await part.read(decode=False)
    if len(data) > 8 * 1024 * 1024:
        return fail("Rasm 8 MB dan katta")
    if not (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"):
        return fail("Faqat JPG yoki PNG")
    result = await _tg_call(ctx.owner.set_photo(data=bytes(data)))
    return fail(str(result)) if isinstance(result, Exception) else ok(message=result)


async def api_report_now(request):
    ctx = request.app[CTX]
    result = await _tg_call(ctx.owner.send_report(force=True))
    if isinstance(result, Exception):
        return fail(str(result))
    return ok() if result else fail("Ikkinchi akkaunt sozlanmagan yoki topilmadi")


async def api_post_now(request):
    ctx = request.app[CTX]
    result = await _tg_call(ctx.owner.post_now())
    if isinstance(result, Exception) or not result:
        return fail("E'lon yuborilmadi: kanal, mavzular va admin huquqini tekshiring")
    return ok()


async def api_backup_now(request):
    ctx = request.app[CTX]
    if not ctx.backup or not ctx.backup.enabled:
        return fail("Zaxira sozlanmagan (HF_TOKEN, STATE_REPO, SECRET_KEY)")
    return ok() if await ctx.backup.backup_now() else fail(ctx.backup.last_error)


def create_app(ctx) -> web.Application:
    app = web.Application(middlewares=[security_headers, require_auth],
                          client_max_size=10 * 1024 * 1024)
    app[CTX] = ctx
    r = app.router
    r.add_get("/", index)
    r.add_get("/login", login_page)
    r.add_get("/admin", admin_page)
    r.add_get("/healthz", healthz)
    r.add_get("/api/ping", healthz)  # Vercel cron keep-alive (through the proxy)
    r.add_static("/assets/", PANEL / "assets", show_index=False)
    r.add_post("/api/login", api_login)
    r.add_post("/api/logout", api_logout)
    r.add_get("/api/me", api_me)
    r.add_post("/api/password", api_password)
    r.add_get("/api/status", api_status)
    r.add_get("/api/logs", api_logs)
    r.add_get("/api/settings", api_settings)
    r.add_post("/api/settings", api_settings_save)
    r.add_post("/api/ai/toggle", api_toggle_ai)
    r.add_post("/api/ai/test", api_ai_test)
    r.add_get("/api/models", api_models)
    r.add_post("/api/keys/add", api_keys_add)
    r.add_post("/api/keys/delete", api_keys_delete)
    r.add_post("/api/keys/move", api_keys_move)
    r.add_post("/api/telegram/send_code", api_tg_send_code)
    r.add_post("/api/telegram/code", api_tg_code)
    r.add_post("/api/telegram/password", api_tg_password)
    r.add_post("/api/telegram/logout", api_tg_logout)
    r.add_post("/api/telegram/reconnect", api_tg_reconnect)
    r.add_get("/api/telegram/sessions", api_tg_sessions)
    r.add_post("/api/telegram/terminate", api_tg_terminate)
    r.add_post("/api/account/name", api_account_name)
    r.add_get("/api/account/photo", api_account_photo)
    r.add_post("/api/account/photo", api_account_photo)
    r.add_post("/api/owner/report", api_report_now)
    r.add_post("/api/owner/post", api_post_now)
    r.add_post("/api/backup", api_backup_now)
    return app
