"""Wires everything together: database, settings, Telegram, AI, panel, backups."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
from dataclasses import dataclass, field

import uvicorn

from .bot import AutoReplyBot
from .config import Bootstrap, Settings
from .llm import OpenRouterClient
from .logging_setup import setup_logging
from .owner import OwnerDesk
from .persist import StateBackup
from .service import TelegramService
from .session import SessionLock
from .storage import Storage
from .web.auth import Auth
from .web.server import create_app

log = logging.getLogger(__name__)


@dataclass
class AppContext:
    boot: Bootstrap
    settings: Settings
    store: Storage
    llm: OpenRouterClient
    bot: AutoReplyBot
    owner: OwnerDesk
    tg: TelegramService
    auth: Auth
    backup: StateBackup
    log_buffer: object
    started_at: float = field(default_factory=time.time)


async def _ticker(owner: OwnerDesk) -> None:
    """Reports, channel posts and owner-account resolution."""
    while True:
        await asyncio.sleep(30)
        try:
            await owner.tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Rejalashtirilgan vazifada xato")


class _Server(uvicorn.Server):
    """uvicorn inside our own event loop: we handle SIGINT/SIGTERM ourselves."""

    def install_signal_handlers(self) -> None:  # uvicorn < 0.29
        pass

    @contextlib.contextmanager
    def capture_signals(self):  # uvicorn >= 0.29
        yield


KEEP_AWAKE_INTERVAL = 6 * 3600


async def _keep_awake(settings: Settings, boot: Bootstrap) -> None:
    """Free HF Spaces sleep after 48h without HTTP traffic; ping our own URL.

    ``SPACE_HOST`` is set by Hugging Face. A private Space only answers with
    a token, so the request carries ``HF_TOKEN``.
    """
    host = os.getenv("SPACE_HOST", "").strip()
    if not host:
        return  # not running on Hugging Face
    import httpx

    url = f"https://{host}/healthz"
    headers = {"Authorization": f"Bearer {boot.hf_token}"} if boot.hf_token else {}
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            await asyncio.sleep(KEEP_AWAKE_INTERVAL)
            if not settings.keep_awake:
                continue
            try:
                response = await client.get(url, headers=headers)
                log.info("Uyg'oq saqlash: %s -> %s", host, response.status_code)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Uyg'oq saqlash so'rovi bajarilmadi: %s", exc)


async def run(boot: Bootstrap) -> int:
    log_buffer = setup_logging(boot.log_level, boot.data_dir)

    lock = SessionLock(boot.sessions_dir / f"{boot.session_name}.lock")
    lock.acquire()

    backup = StateBackup(boot, None)
    await asyncio.to_thread(backup.restore)  # before the database is opened

    store = Storage(boot.data_dir / "state.db")
    store.on_change = backup.mark_dirty
    backup.store = store
    settings = Settings(boot, store)
    backup.s = settings

    llm = OpenRouterClient(settings)
    owner = OwnerDesk(settings, store, llm)
    bot = AutoReplyBot(None, settings, store, llm, owner=owner)
    owner.bot = bot
    tg = TelegramService(settings, store, bot, backup)
    auth = Auth(store, boot)
    ctx = AppContext(boot, settings, store, llm, bot, owner, tg, auth, backup, log_buffer)

    server = _Server(uvicorn.Config(
        create_app(ctx), host="0.0.0.0", port=boot.port, lifespan="off",
        access_log=False, log_level="warning", proxy_headers=False,
    ))
    web_task = asyncio.create_task(server.serve(), name="web")
    log.info("Admin panel (FastAPI): http://0.0.0.0:%s", boot.port)
    if not backup.enabled:
        log.warning("Off-site zaxira o'chiq (HF_TOKEN, STATE_REPO, SECRET_KEY). "
                    "Hugging Face'da restartdan keyin sessiya yo'qolishi mumkin!")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)  # not available on Windows

    tasks = [
        web_task,
        asyncio.create_task(tg.run_forever(), name="telegram"),
        asyncio.create_task(_ticker(owner), name="ticker"),
    ]
    if os.getenv("SPACE_HOST"):
        tasks.append(asyncio.create_task(_keep_awake(settings, boot), name="keep-awake"))
    if backup.enabled:
        tasks.append(asyncio.create_task(backup.run(), name="backup"))
    try:
        stopper = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait([stopper, *tasks], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task is not stopper:
                log.critical("Asosiy vazifa kutilmaganda to'xtadi: %s", task.get_name(),
                             exc_info=task.exception())
    finally:
        log.info("To'xtatilmoqda...")
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(web_task), timeout=5)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bot.shutdown()
        if tg.client is not None:
            with contextlib.suppress(Exception):
                tg.client.session.save()
                await tg.client.disconnect()
        with contextlib.suppress(Exception):
            await backup.backup_now()
        await llm.close()
        store.close()
        lock.release()
        log.info("Bot to'xtadi.")
    return 0
