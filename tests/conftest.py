import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoreply.config import Bootstrap, Settings  # noqa: E402
from autoreply.storage import Storage  # noqa: E402


@pytest.fixture
def boot(tmp_path):
    return Bootstrap(
        admin_username="abu", admin_password="test-password-123", secret_key="s3cret",
        hf_token="", state_repo="", data_dir=tmp_path / "data",
        sessions_dir=tmp_path / "sessions", session_name="account", string_session="",
        port=0, log_level="INFO",
    )


@pytest.fixture
def store():
    return Storage(":memory:")


@pytest.fixture
def settings(boot, store):
    s = Settings(boot, store)
    s.update({
        "api_id": 12345, "api_hash": "abcdef", "openrouter_api_keys": ["sk-or-test-key-1234567890"],
        "owner_name": "Ali", "debounce_seconds": 0.05, "typing_enabled": False,
        "away_notice_enabled": False, "catch_up_on_start": False,
    })
    return s
