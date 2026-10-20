from types import SimpleNamespace as NS

from autoreply.formatting import (
    build_history,
    describe_message,
    is_command,
    matches_any,
    split_text,
)


def msg(id, text="", out=False, **kw):
    base = dict(id=id, raw_text=text, out=out, media=None, action=None, fwd_from=None, sender=None)
    base.update(kw)
    return NS(**base)


def test_describe_media():
    m = msg(1, "qara", media=object(), photo=object())
    assert describe_message(m) == "[rasm] qara"
    v = msg(2, "", media=object(), voice=object())
    assert describe_message(v) == "[ovozli xabar]"
    s = msg(3, "", media=object(), sticker=object(), file=NS(emoji="😂"))
    assert describe_message(s) == "[stiker 😂]"


def test_build_history_merges_and_filters():
    messages = [
        msg(1, "bizning javob", out=True),  # leading assistant turn is dropped
        msg(2, "salom"),
        msg(3, "qalesan"),
        msg(4, ".ai status", out=True),  # command is ignored
        msg(5, "yaxshi, o'zingchi?", out=True),
        msg(6, "zo'r", action=None),
        msg(7, "", action=object()),  # service message
    ]
    history = build_history(messages)
    assert history == [
        {"role": "user", "content": "salom\nqalesan"},
        {"role": "assistant", "content": "yaxshi, o'zingchi?"},
        {"role": "user", "content": "zo'r"},
    ]
    assert build_history(messages, after_id=5) == [{"role": "user", "content": "zo'r"}]


def test_build_history_group_names():
    sender = NS(id=9, first_name="Vali", last_name=None, username="vali")
    history = build_history([msg(1, "hey", sender=sender)], with_names=True)
    assert history[0]["content"] == "Vali (@vali): hey"


def test_split_text():
    text = ("a" * 50 + "\n") * 200
    chunks = split_text(text, limit=500)
    assert all(len(c) <= 500 for c in chunks)
    assert "".join(c.replace("\n", "") for c in chunks) == text.replace("\n", "")
    assert split_text("qisqa") == ["qisqa"]


def test_is_command_and_matches():
    assert is_command(".ai on", ".ai")
    assert is_command(".AI status", ".ai")
    assert not is_command(".aim", ".ai")
    sender = NS(id=42, username="Some_User")
    assert matches_any(sender, 42, ["@some_user"])
    assert matches_any(sender, 42, ["42"])
    assert not matches_any(sender, 42, ["other"])
