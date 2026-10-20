"""Encrypted off-site backup of the bot state to a private Hugging Face dataset.

Hugging Face Spaces lose their disk on every restart (unless paid persistent
storage is enabled). To keep the Telegram session and every panel setting,
the database and the session backup are packed, encrypted with a key derived
from ``SECRET_KEY`` (Fernet / AES-128 + HMAC) and uploaded to a *private*
dataset repo. On start the archive is downloaded and restored.

Enabled when ``HF_TOKEN`` and ``STATE_REPO`` are set.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import tarfile
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

ARCHIVE_NAME = "state.bin"
MIN_INTERVAL = 120  # seconds between uploads


def fernet_for(secret: str):
    from cryptography.fernet import Fernet

    key = base64.urlsafe_b64encode(hashlib.sha256(("tg-autoreply:" + secret).encode()).digest())
    return Fernet(key)


class StateBackup:
    def __init__(self, boot, settings):
        self.boot = boot
        self.s = settings
        self.store = None
        self.enabled = bool(boot.hf_token and boot.state_repo and boot.secret_key)
        self.last_backup_at = 0.0
        self.last_error = ""
        self._dirty = False
        self._lock = asyncio.Lock()

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "repo": self.boot.state_repo if self.enabled else "",
            "last_backup_at": self.last_backup_at,
            "last_error": self.last_error,
            "pending": self._dirty,
        }

    # --- restore (sync, before the database is opened) -------------------

    def restore(self) -> bool:
        if not self.enabled:
            return False
        db = self.boot.data_dir / "state.db"
        if db.exists():
            return False  # local state wins (persistent storage)
        try:
            from huggingface_hub import hf_hub_download

            path = hf_hub_download(
                self.boot.state_repo, ARCHIVE_NAME, repo_type="dataset",
                token=self.boot.hf_token, force_download=True,
            )
        except Exception as exc:  # noqa: BLE001 - first run: nothing to restore
            log.info("Zaxira topilmadi (%s) - yangi holatdan boshlanadi", exc.__class__.__name__)
            return False
        try:
            raw = fernet_for(self.boot.secret_key).decrypt(Path(path).read_bytes())
        except Exception:  # noqa: BLE001
            log.error("Zaxirani ochib bo'lmadi: SECRET_KEY o'zgarganmi?")
            return False
        self.boot.data_dir.mkdir(parents=True, exist_ok=True)
        self.boot.sessions_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                name = Path(member.name).name  # no paths from the archive
                if member.name.startswith("data/") and name == "state.db":
                    target = db
                elif member.name.startswith("sessions/") and name.endswith(".string"):
                    target = self.boot.sessions_dir / name
                else:
                    continue
                target.write_bytes(tar.extractfile(member).read())
                target.chmod(0o600)
        log.info("Holat zaxiradan tiklandi (%s)", self.boot.state_repo)
        return True

    # --- backup ----------------------------------------------------------

    def mark_dirty(self) -> None:
        self._dirty = True

    def _pack(self) -> bytes:
        buf = io.BytesIO()
        with tempfile.TemporaryDirectory() as tmp:
            db_copy = Path(tmp) / "state.db"
            self.store.backup_to(db_copy)
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                tar.add(db_copy, arcname="data/state.db")
                for f in self.boot.sessions_dir.glob("*.string"):
                    tar.add(f, arcname=f"sessions/{f.name}")
        return fernet_for(self.boot.secret_key).encrypt(buf.getvalue())

    def _upload(self, blob: bytes) -> None:
        from huggingface_hub import HfApi

        api = HfApi(token=self.boot.hf_token)
        api.create_repo(self.boot.state_repo, repo_type="dataset", private=True, exist_ok=True)
        api.upload_file(
            path_or_fileobj=blob, path_in_repo=ARCHIVE_NAME, repo_id=self.boot.state_repo,
            repo_type="dataset", commit_message="state backup",
        )

    async def backup_now(self) -> bool:
        if not self.enabled or self.store is None:
            return False
        async with self._lock:
            self._dirty = False
            try:
                blob = await asyncio.to_thread(self._pack)
                await asyncio.to_thread(self._upload, blob)
            except Exception as exc:  # noqa: BLE001
                self._dirty = True
                self.last_error = str(exc)[:300]
                log.warning("Zaxira yuklanmadi: %s", exc)
                return False
            self.last_backup_at = time.time()
            self.last_error = ""
            return True

    async def run(self) -> None:
        """Upload whenever something changed, at most every MIN_INTERVAL seconds."""
        if not self.enabled:
            return
        while True:
            await asyncio.sleep(MIN_INTERVAL)
            if self._dirty or time.time() - self.last_backup_at > 6 * 3600:
                await self.backup_now()
