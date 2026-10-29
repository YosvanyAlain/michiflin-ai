"""Strict owner-key pinning: synthetic keys and RPCs only; no production account."""
from __future__ import annotations

import base64
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from michiflin import app, config, identity_guard, miniapp, state

pytestmark = pytest.mark.skipif(not shutil.which("gpg"), reason="GnuPG required for fingerprint verification")


@pytest.fixture(scope="module")
def synthetic_keys(tmp_path_factory):
    home = tmp_path_factory.mktemp("gpg-synthetic")
    home.chmod(0o700)
    command = ["gpg", "--no-options", "--homedir", str(home), "--batch", "--pinentry-mode", "loopback", "--passphrase", ""]

    def make(label):
        subprocess.run(command + ["--quick-generate-key", label, "ed25519", "sign", "0"],
                       capture_output=True, check=True, timeout=20)
        listing = subprocess.run(command + ["--with-colons", "--list-keys", label],
                                 capture_output=True, check=True, timeout=20).stdout.decode()
        fingerprint = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))
        key = subprocess.run(command + ["--export", fingerprint], capture_output=True, check=True, timeout=20).stdout
        return fingerprint, key

    first_fingerprint, first_key = make("Synthetic Owner <owner@example.invalid>")
    # A malicious-looking name containing the trusted fingerprint must not help.
    second_fingerprint, second_key = make(f"{first_fingerprint} <attacker@example.invalid>")
    return (first_fingerprint, first_key), (second_fingerprint, second_key)


def card(key: bytes) -> str:
    return "BEGIN:VCARD\nVERSION:4.0\nFN:Example\nKEY:" + base64.b64encode(key).decode() + "\nEND:VCARD\n"


class FakeRpc:
    def __init__(self, key: bytes):
        self.info = SimpleNamespace(chat_type="Single", is_encrypted=True, is_contact_request=False, is_self_talk=False)
        self.contact = SimpleNamespace(address="owner@example.invalid", is_key_contact=True, e2ee_avail=True,
                                       is_verified=True, verifier_id=1)
        self.members = [15]
        self.card = card(key)

    def get_basic_chat_info(self, _account, _chat_id):
        return self.info

    def get_chat_contacts(self, _account, _chat_id):
        return self.members

    def get_contact(self, _account, _contact_id):
        return self.contact

    def make_vcard(self, _account, _contacts):
        return self.card

    def create_chat_by_contact_id(self, _account, _contact_id):
        return 21


def message():
    return SimpleNamespace(id=44, chat_id=21, from_id=15, show_padlock=True, text="hello", is_info=False, file=None)


@pytest.fixture
def strict_owner(monkeypatch, synthetic_keys):
    (fingerprint, key), _ = synthetic_keys
    monkeypatch.setattr(config, "BOT_VERIFIED_CONTACTS_ONLY", True)
    monkeypatch.setattr(config, "OWNER_ADDRESS", "owner@example.invalid")
    monkeypatch.setattr(config, "OWNER_PGP_FINGERPRINT", fingerprint)
    return FakeRpc(key)


def test_legacy_mode_is_not_changed(monkeypatch):
    monkeypatch.setattr(config, "BOT_VERIFIED_CONTACTS_ONLY", False)
    assert identity_guard.authorized_chat(None, 1, 2, "")
    assert identity_guard.authorized_entry(None, 1, {})


def test_verified_signed_owner_and_fingerprint_pass(strict_owner, synthetic_keys):
    rpc = strict_owner
    assert identity_guard.contact_fingerprint(rpc, 1, 15) == synthetic_keys[0][0]
    assert identity_guard.authorized_chat(rpc, 1, 21, "owner@example.invalid", contact_id=15, message=message())
    assert identity_guard.authorized_entry(rpc, 1, {"chat": 21, "addr": "owner@example.invalid"})


def test_delta_chat_vcard_data_uri_with_escaped_comma(strict_owner, synthetic_keys):
    fingerprint, key = synthetic_keys[0]
    encoded = base64.b64encode(key).decode()
    strict_owner.card = f"BEGIN:VCARD\nVERSION:4.0\nKEY:data:application/pgp-keys;base64\\,{encoded}\nEND:VCARD\n"
    assert identity_guard.contact_fingerprint(strict_owner, 1, 15) == fingerprint
    assert identity_guard.authorized_chat(strict_owner, 1, 21, "owner@example.invalid", message=message())


def test_wrong_fingerprint_cannot_impersonate_owner(strict_owner, synthetic_keys):
    rpc = strict_owner
    rpc.card = card(synthetic_keys[1][1])  # attacker key has the real fingerprint in its displayed UID
    assert identity_guard.contact_fingerprint(rpc, 1, 15) == synthetic_keys[1][0]
    assert not identity_guard.authorized_chat(rpc, 1, 21, "owner@example.invalid", contact_id=15, message=message())


@pytest.mark.parametrize("attribute,value", [
    ("is_key_contact", False), ("e2ee_avail", False), ("is_verified", False), ("verifier_id", None),
    ("verifier_id", 42), ("address", "other@example.invalid"),
])
def test_unverified_or_wrong_contact_is_denied(strict_owner, attribute, value):
    setattr(strict_owner.contact, attribute, value)
    assert not identity_guard.authorized_chat(strict_owner, 1, 21, "owner@example.invalid", message=message())


@pytest.mark.parametrize("attribute,value", [
    ("chat_type", "Group"), ("is_encrypted", False), ("is_contact_request", True), ("is_self_talk", True),
])
def test_wrong_chat_is_denied(strict_owner, attribute, value):
    setattr(strict_owner.info, attribute, value)
    assert not identity_guard.authorized_chat(strict_owner, 1, 21, "owner@example.invalid", message=message())


def test_plaintext_wrong_sender_and_missing_pin_fail_closed(strict_owner, monkeypatch):
    msg = message()
    msg.show_padlock = False
    assert not identity_guard.authorized_chat(strict_owner, 1, 21, "owner@example.invalid", message=msg)
    msg.show_padlock = True
    msg.from_id = 16
    assert not identity_guard.authorized_chat(strict_owner, 1, 21, "owner@example.invalid", message=msg)
    strict_owner.members = [15, 16]
    assert not identity_guard.authorized_chat(strict_owner, 1, 21, "owner@example.invalid")
    strict_owner.members = [15]
    monkeypatch.setattr(config, "OWNER_PGP_FINGERPRINT", "")
    assert not identity_guard.authorized_chat(strict_owner, 1, 21, "owner@example.invalid")


def test_malformed_or_multiple_keys_never_supply_a_pin(strict_owner, synthetic_keys):
    key1, key2 = synthetic_keys[0][1], synthetic_keys[1][1]
    for value in ("", card(key1).replace("KEY:", "NOTE:KEY:"), card(key1).replace("END:VCARD", "KEY:eA==\nEND:VCARD"), card(key1 + key2)):
        strict_owner.card = value
        assert not identity_guard.contact_fingerprint(strict_owner, 1, 15)
    assert not identity_guard.normalize_fingerprint("fingerprint: " + synthetic_keys[0][0])


def test_rpc_or_gpg_failure_fails_closed(strict_owner, monkeypatch):
    strict_owner.make_vcard = Mock(side_effect=RuntimeError("unavailable"))
    assert not identity_guard.authorized_chat(strict_owner, 1, 21, "owner@example.invalid")
    monkeypatch.setattr(identity_guard.subprocess, "run", Mock(side_effect=FileNotFoundError()))
    assert not identity_guard._fingerprint_from_key(b"uncached public key")


def test_strict_input_rejects_plaintext_before_dispatch(strict_owner, monkeypatch):
    bot = SimpleNamespace(rpc=strict_owner)
    monkeypatch.setattr(app.admin, "is_allowed", lambda _address: True)
    monkeypatch.setattr(app.admin, "is_admin", lambda _address: True)
    monkeypatch.setattr(app.admin, "seen", lambda _address: None)
    monkeypatch.setattr(config, "service_access_allowed", lambda _address: True)
    monkeypatch.setattr(state, "act_as", lambda _address: None)
    monkeypatch.setattr(state, "maintenance_active", lambda: False)
    monkeypatch.setattr(app, "_locale", lambda _text, _chat: "es")
    msg = message()
    msg.show_padlock = False
    assert app._screen(bot, 1, msg) is None
    msg.show_padlock = True
    assert app._screen(bot, 1, msg) is not None


def test_strict_welcome_and_miniapp_deny_wrong_key(strict_owner, synthetic_keys, monkeypatch):
    rpc = strict_owner
    rpc.card = card(synthetic_keys[1][1])
    monkeypatch.setattr(app.chat, "ready", lambda: True)
    monkeypatch.setattr(app.admin, "is_allowed", lambda _address: True)
    welcome = Mock()
    monkeypatch.setattr(miniapp, "welcome", welcome)
    app._welcome(SimpleNamespace(rpc=rpc), 1, 15)
    welcome.assert_not_called()
    monkeypatch.setattr(miniapp.chat, "rpc", lambda: rpc)
    monkeypatch.setattr(miniapp.chat, "account", lambda: 1)
    assert not miniapp._verified_binding({"chat": 21, "addr": "owner@example.invalid"})


def test_stale_epoch_never_receives_push(monkeypatch):
    monkeypatch.setattr(config, "BOT_VERIFIED_CONTACTS_ONLY", False)
    monkeypatch.setattr(config, "BOT_EPOCH_STRICT", True)
    monkeypatch.setattr(config, "BOT_EPOCH", "fresh")
    monkeypatch.setattr(state, "data", {"apps": {
        "50": {"chat": 21, "addr": "owner@example.invalid", "epoch": "fresh"},
        "99": {"chat": 21, "addr": "owner@example.invalid", "epoch": "old"},
    }})
    monkeypatch.setattr(config, "OWNER_ADDRESS", "owner@example.invalid")
    monkeypatch.setattr(miniapp.chat, "ready", lambda: True)
    monkeypatch.setattr(miniapp.admin, "is_allowed", lambda _address: True)
    send = Mock(return_value=True)
    monkeypatch.setattr(miniapp.chat, "send_update", send)
    assert miniapp.push(21, "notice", {})
    assert send.call_args.args[0] == 50
