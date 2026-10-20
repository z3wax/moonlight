import pytest

from autoreply.config import Settings, panel_values
from autoreply.owner import parse_intent
from autoreply.bot import is_debt_topic
from autoreply.web.auth import Auth, hash_password, verify_password


def test_settings_persist_and_validate(boot, store):
    s = Settings(boot, store)
    assert s.ai_enabled is True
    s.update({"ai_enabled": False, "post_time": "9:05", "whitelist": "a, b\nc"})
    again = Settings(boot, store)
    assert again.ai_enabled is False
    assert again.post_time == "09:05"
    assert again.whitelist == ["a", "b", "c"]
    with pytest.raises(ValueError):
        s.update({"post_time": "25:00"})
    with pytest.raises(ValueError):
        s.update({"history_limit": 0})
    with pytest.raises(ValueError):
        s.update({"nope": 1})
    assert s.update({"api_id": 99}) == ["api_id"]


def test_secrets_are_masked(settings):
    settings.update({"api_hash": "0123456789abcdef"})
    values = panel_values(settings)
    assert values["api_hash"] == {"set": True, "masked": "••••••cdef"}
    assert all("test-key" not in v for v in values["openrouter_api_keys"])


def test_password_hash():
    h = hash_password("example-long-pass")
    assert verify_password("example-long-pass", h)
    assert not verify_password("wrong", h)
    assert "example" not in h


def test_auth_lockout_and_sessions(boot, store):
    auth = Auth(store, boot)
    assert auth.login("abu", "test-password-123", "1.1.1.1") is not None
    for _ in range(5):
        assert auth.login("abu", "bad", "2.2.2.2") is None
    assert auth.locked_for("2.2.2.2") > 0
    # correct password is refused while locked
    assert auth.login("abu", "test-password-123", "2.2.2.2") is None
    session = auth.login("ABU", "test-password-123", "3.3.3.3")
    assert auth.get(session.token) is session
    auth.change_password("test-password-123", "new-password-456")
    assert auth.get(session.token) is None  # everyone logged out
    with pytest.raises(ValueError):
        auth.change_password("new-password-456", "short")


def test_parse_intent():
    assert parse_intent('Mana: {"action": "set_name", "args": {"first_name": "Ali"}}') == {
        "action": "set_name", "args": {"first_name": "Ali"}}
    assert parse_intent("bilmadim")["action"] == "none"


def test_debt_detection():
    assert is_debt_topic("Qarzingizni qachon qaytarasiz?")
    assert is_debt_topic("Когда вернешь долг?")
    assert not is_debt_topic("Salom, ishlar qalay?")
