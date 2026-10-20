import os

from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession, StringSession

from autoreply.session import parse_proxy, prepare_session_file


def make_string():
    s = StringSession()
    s.set_dc(2, "149.154.167.51", 443)
    s.auth_key = AuthKey(os.urandom(256))
    return s.save(), s.auth_key.key


def test_restore_from_string_backup(settings):
    string, key = make_string()
    settings.sessions_dir.mkdir(parents=True)
    settings.string_session_path.write_text(string)
    assert not settings.session_path.exists()

    prepare_session_file(settings)

    assert settings.session_path.exists()
    restored = SQLiteSession(str(settings.sessions_dir / settings.session_name))
    assert restored.auth_key.key == key
    assert restored.dc_id == 2
    restored.close()


def test_env_string_seeds_file(settings):
    string, key = make_string()
    settings.boot.string_session = string
    prepare_session_file(settings)
    restored = SQLiteSession(str(settings.sessions_dir / settings.session_name))
    assert restored.auth_key.key == key
    restored.close()


def test_existing_file_is_not_overwritten(settings):
    string_a, key_a = make_string()
    settings.boot.string_session = string_a
    prepare_session_file(settings)
    string_b, _ = make_string()
    settings.boot.string_session = string_b
    prepare_session_file(settings)
    restored = SQLiteSession(str(settings.sessions_dir / settings.session_name))
    assert restored.auth_key.key == key_a
    restored.close()


def test_parse_proxy():
    assert parse_proxy("") is None
    proxy = parse_proxy("socks5://u:p%40ss@127.0.0.1:9050")
    assert proxy == {
        "proxy_type": "socks5", "addr": "127.0.0.1", "port": 9050,
        "rdns": True, "username": "u", "password": "p@ss",
    }
