import dataclasses
import sys
import types

from autoreply.config import Settings
from autoreply.persist import StateBackup
from autoreply.storage import Storage


def test_encrypted_backup_roundtrip(boot, tmp_path, monkeypatch):
    boot = dataclasses.replace(boot, hf_token="hf_x", state_repo="me/state")
    boot.sessions_dir.mkdir(parents=True)
    (boot.sessions_dir / "account.string").write_text("1SESSIONSTRING")
    store = Storage(boot.data_dir / "state.db")
    Settings(boot, store).update({"owner_name": "Abu", "ai_enabled": False})

    backup = StateBackup(boot, None)
    backup.store = store
    blob = backup._pack()
    assert b"SESSIONSTRING" not in blob and b"Abu" not in blob  # encrypted
    archive = tmp_path / "state.bin"
    archive.write_bytes(blob)

    # fresh machine: nothing on disk, archive comes from the hub
    fresh = dataclasses.replace(boot, data_dir=tmp_path / "new", sessions_dir=tmp_path / "new" / "s")
    fake_hub = types.SimpleNamespace(hf_hub_download=lambda *a, **k: str(archive))
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hub)
    assert StateBackup(fresh, None).restore()
    assert (fresh.sessions_dir / "account.string").read_text() == "1SESSIONSTRING"
    restored = Settings(fresh, Storage(fresh.data_dir / "state.db"))
    assert restored.owner_name == "Abu" and restored.ai_enabled is False

    # wrong key cannot open it
    other = dataclasses.replace(fresh, secret_key="different", data_dir=tmp_path / "x",
                                sessions_dir=tmp_path / "x" / "s")
    assert not StateBackup(other, None).restore()
