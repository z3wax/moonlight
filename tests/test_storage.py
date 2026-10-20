import time

from autoreply.storage import Storage


def test_settings_roundtrip(tmp_path):
    store = Storage(tmp_path / "state.db")
    assert store.get_bool("away", True) is True
    store.set_bool("away", False)
    assert store.get_bool("away", True) is False
    store.set("model", "x/y")
    assert store.get("model") == "x/y"
    store.set("model", None)
    assert store.get("model") is None
    store.close()

    # survives reopening (persistence across restarts)
    store = Storage(tmp_path / "state.db")
    assert store.get_bool("away", True) is False


def test_chat_state_and_rate_limit():
    store = Storage(":memory:")
    assert not store.is_chat_disabled(5)
    store.set_chat_disabled(5, True)
    assert store.is_chat_disabled(5)
    assert store.disabled_chats() == [5]
    store.set_owner_active_until(5, 123.0)
    assert store.owner_active_until(5) == 123.0
    store.set_reset_after_msg_id(5, 77)
    assert store.reset_after_msg_id(5) == 77

    now = time.time()
    store.record_reply(5, now - 7200)
    store.record_reply(5, now)
    assert store.replies_since(5, now - 3600) == 1
    store.prune_replies(now - 3600)
    assert store.replies_since(5, 0) == 1


def test_usable_from_another_thread(tmp_path):
    import threading

    store = Storage(tmp_path / "state.db")
    store.set("k", "v")
    seen = []
    t = threading.Thread(target=lambda: (seen.append(store.get("k")), store.backup_to(tmp_path / "copy.db")))
    t.start()
    t.join()
    assert seen == ["v"] and (tmp_path / "copy.db").exists()
