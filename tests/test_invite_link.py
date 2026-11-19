"""The owner can read back the bot's own invitation link; nobody else can."""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from michiflin import chat, invite  # noqa: E402

LINK = "https://i.delta.chat/#AABBCC&a=bot%40example.org&n=Bot&i=xyz&s=sss"


class _Rpc:
    def __init__(self, value=LINK):
        self.value, self.calls = value, []

    def get_chat_securejoin_qr_code(self, accid, chat_id):
        self.calls.append((accid, chat_id))
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def _bind(rpc):
    invite.forget()
    chat.bind(types.SimpleNamespace(rpc=rpc), 1)
    return rpc


def _unbind():
    invite.forget()
    chat.bind(None, 0)


def test_link_is_account_wide_and_cached():
    rpc = _bind(_Rpc())
    try:
        assert invite.link() == LINK
        assert invite.link() == LINK
        # Asked once thanks to the cache, and for the account, not a group.
        assert rpc.calls == [(1, None)]
        assert invite.link(refresh=True) == LINK
        assert len(rpc.calls) == 2
    finally:
        _unbind()


def test_offline_account_yields_empty_string_not_an_error():
    _unbind()
    assert invite.available() is False
    assert invite.link() == ""


def test_rpc_failure_keeps_the_previous_value_and_never_raises():
    rpc = _bind(_Rpc())
    try:
        assert invite.link() == LINK
        rpc.value = RuntimeError("account offline")
        # A key-derived link stays valid, so a failed refresh must not blank it.
        assert invite.link(refresh=True) == LINK
    finally:
        _unbind()


def test_failure_with_no_cached_value_is_empty():
    _bind(_Rpc(RuntimeError("boom")))
    try:
        assert invite.link() == ""
    finally:
        _unbind()


def test_command_and_operation_are_owner_only():
    from michiflin import commands

    assert "invitacion" in commands.COMMANDS
    # Registered as owner-only, so the single authorization point rejects
    # everyone else before the handler ever runs.
    assert "invitacion" in commands.ADMIN_ONLY
