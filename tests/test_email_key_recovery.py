"""Email-based recovery tests: synthetic identities and no real accounts or addresses."""
from __future__ import annotations

import copy
from contextlib import nullcontext
from types import SimpleNamespace
from unittest import mock

import pytest

from michiflin import access, admin, app, chat, config, identity_guard, miniapp, queue, state
from tools import cutover_identity_once as cutover

OWNER = "owner@example.invalid"
BACKUP = "backup@example.invalid"
GUEST = "guest@example.invalid"
STRANGER = "stranger@example.invalid"
OLD = "A" * 40
NEW = "B" * 40
THIRD = "C" * 40


class SyntheticRpc:
    def __init__(self):
        self.contacts = {
            15: SimpleNamespace(address=OWNER, fingerprint=OLD, is_key_contact=True, e2ee_avail=True,
                                is_verified=False, verifier_id=None),
            16: SimpleNamespace(address=OWNER, fingerprint=NEW, is_key_contact=True, e2ee_avail=True,
                                is_verified=False, verifier_id=None),
            17: SimpleNamespace(address=BACKUP, fingerprint=THIRD, is_key_contact=True, e2ee_avail=True,
                                is_verified=False, verifier_id=None),
            18: SimpleNamespace(address=STRANGER, fingerprint=NEW, is_key_contact=True, e2ee_avail=True,
                                is_verified=False, verifier_id=None),
            19: SimpleNamespace(address=GUEST, fingerprint=THIRD, is_key_contact=True, e2ee_avail=True,
                                is_verified=False, verifier_id=None),
        }
        self.members = {21: [15], 22: [16], 23: [17], 24: [18], 25: [19]}
        self.info = {key: SimpleNamespace(chat_type="Single", is_encrypted=True, is_contact_request=False,
                                          is_self_talk=False) for key in self.members}
        self.messages = {}
        self.chat_messages = {}

    def get_basic_chat_info(self, _account, chat_id):
        return self.info[chat_id]

    def get_chat_contacts(self, _account, chat_id):
        return self.members[chat_id]

    def get_contact(self, _account, contact_id):
        return self.contacts[contact_id]

    def get_chatlist_entries(self, _account, flags, _query, _contact):
        return list(self.members) if flags == 0 else []

    def get_contacts(self, _account, _flags, query):
        return [SimpleNamespace(id=ident, address=contact.address) for ident, contact in self.contacts.items()
                if contact.address == query]

    def get_chat_id_by_contact_id(self, _account, contact_id):
        return next((chat_id for chat_id, members in self.members.items() if members == [contact_id]), 0)

    def get_message_ids(self, _account, chat_id, _info_only, _daymarker):
        return self.chat_messages.get(chat_id, [])

    def get_message(self, _account, ident):
        return self.messages[ident]

    def accept_chat(self, _account, chat_id):
        self.info[chat_id].is_contact_request = False


def msg(chat_id, contact, ident=40, timestamp=100):
    return SimpleNamespace(id=ident, chat_id=chat_id, from_id=contact, show_padlock=True,
                           is_info=False, received_timestamp=timestamp, text="synthetic")


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(config, "OWNER_ADDRESS", OWNER)
    monkeypatch.setattr(config, "OWNER_BACKUP_ADDRESS", BACKUP)
    monkeypatch.setattr(config, "OWNER_ONLY", True)
    monkeypatch.setattr(config, "MAINTENANCE_MODE", False)
    monkeypatch.setattr(config, "ADMIN_ADDRESSES", {OWNER, BACKUP})
    monkeypatch.setattr(config, "ALLOWED_SENDERS", {OWNER, GUEST})  # historical guest stays closed
    monkeypatch.setattr(config, "EMAIL_KEY_RECOVERY", True)
    monkeypatch.setattr(config, "BOT_VERIFIED_CONTACTS_ONLY", True)
    monkeypatch.setattr(config, "BOT_EPOCH_STRICT", True)
    monkeypatch.setattr(config, "OWNER_PGP_FINGERPRINT", OLD)
    monkeypatch.delenv("STATE_BACKUP_REQUIRED", raising=False)
    monkeypatch.setattr(state, "data", copy.deepcopy(state.data))
    state.data["email_key_bindings"] = {}
    state.data["access"] = {"allowed": [], "blocked": [], "attempts": {}, "alerts": True}
    monkeypatch.setattr(state, "save", lambda: None)
    monkeypatch.setattr(identity_guard, "contact_fingerprint", lambda rpc, _account, contact: rpc.contacts[contact].fingerprint)
    access.reset()
    yield SyntheticRpc()
    access.reset()


def accepted(rpc, chat_id, address, contact, incoming=True):
    return identity_guard.authorized_chat(rpc, 1, chat_id, address, contact_id=contact,
                                          message=msg(chat_id, contact) if incoming else None)


def test_only_explicit_owner_and_backup_are_admins_in_private_mode(enabled):
    assert admin.is_admin(OWNER) and admin.is_admin(BACKUP)
    assert admin.is_allowed(OWNER) and admin.is_allowed(BACKUP)
    assert not admin.is_admin(GUEST) and not admin.is_allowed(GUEST)
    assert not admin.is_allowed(STRANGER)
    assert not access.open_one_guest(BACKUP, 60, access.snapshot()["nonce"])
    assert not admin.revoke(BACKUP, block=True)
    snapshot = admin.snapshot()
    assert {item["address"] for item in snapshot["users"]} == {OWNER, BACKUP}
    assert snapshot["email_key_recovery"] is True


def test_current_allowed_key_rotates_and_prior_key_is_not_reactivated(enabled):
    rpc = enabled
    assert accepted(rpc, 21, OWNER, 15)
    assert accepted(rpc, 21, OWNER, 15, incoming=False)
    assert accepted(rpc, 22, OWNER, 16)
    assert not accepted(rpc, 21, OWNER, 15, incoming=False)
    assert not accepted(rpc, 21, OWNER, 15)  # old message cannot rotate it back
    assert accepted(rpc, 22, OWNER, 16, incoming=False)
    assert identity_guard.current_email_chat(OWNER) == 22
    assert identity_guard._key_digest(OLD) in state.data["email_key_bindings"][OWNER]["retired"]
    assert accepted(rpc, 23, BACKUP, 17)
    assert accepted(rpc, 23, BACKUP, 17, incoming=False)


def test_first_reinstalled_owner_key_retires_the_original_pin(enabled):
    rpc = enabled
    assert not accepted(rpc, 22, OWNER, 16, incoming=False)  # passive rechecks never learn
    assert accepted(rpc, 22, OWNER, 16)
    assert not accepted(rpc, 21, OWNER, 15)
    assert identity_guard._key_digest(OLD) in state.data["email_key_bindings"][OWNER]["retired"]


def test_email_matching_does_not_allow_foreign_or_historically_allowed_guests(enabled):
    rpc = enabled
    assert not accepted(rpc, 24, STRANGER, 18)
    assert not accepted(rpc, 25, GUEST, 19)
    assert STRANGER not in state.data["email_key_bindings"]
    assert GUEST not in state.data["email_key_bindings"]
    mode = access.snapshot()
    assert access.open_one_guest(GUEST, 60, mode["nonce"]) is not None
    assert admin.is_allowed(GUEST)
    assert accepted(rpc, 25, GUEST, 19)  # explicitly granted guest may also replace their key
    access.set_owner_only(True)
    assert not admin.is_allowed(GUEST)
    assert not accepted(rpc, 25, GUEST, 19)


@pytest.mark.parametrize("change", ["plaintext", "group", "unencrypted_chat", "wrong_sender", "multi_member", "no_key"])
def test_email_mode_still_requires_single_encrypted_correctly_addressed_chat(enabled, change):
    rpc, incoming = enabled, msg(22, 16)
    if change == "plaintext":
        incoming.show_padlock = False
    elif change == "group":
        rpc.info[22].chat_type = "Group"
    elif change == "unencrypted_chat":
        rpc.info[22].is_encrypted = False
    elif change == "wrong_sender":
        incoming.from_id = 15
    elif change == "multi_member":
        rpc.members[22] = [15, 16]
    else:
        rpc.contacts[16].e2ee_avail = False
    assert not identity_guard.authorized_chat(rpc, 1, 22, OWNER, contact_id=16, message=incoming)
    assert not state.data["email_key_bindings"]


def test_passive_or_retired_chat_cannot_receive_queue_or_app_updates(enabled, monkeypatch):
    rpc = enabled
    assert accepted(rpc, 21, OWNER, 15)
    old_entry = {"addr": OWNER, "chat": 21, "epoch": "test"}
    monkeypatch.setattr(config, "BOT_EPOCH", "test")
    monkeypatch.setattr(chat, "ready", lambda: True)
    monkeypatch.setattr(chat, "rpc", lambda: rpc)
    monkeypatch.setattr(chat, "account", lambda: 1)
    assert miniapp._verified_binding(old_entry)
    assert queue._verified_target(old_entry)
    assert accepted(rpc, 22, OWNER, 16)
    assert not miniapp._verified_binding(old_entry)
    assert not queue._verified_target(old_entry)


def test_required_encrypted_checkpoint_failure_does_not_admit_new_key(enabled, monkeypatch):
    import state_backup
    rpc = enabled
    monkeypatch.setenv("STATE_BACKUP_REQUIRED", "1")
    checkpoint = mock.Mock(side_effect=RuntimeError("synthetic checkpoint failure"))
    monkeypatch.setattr(state_backup, "save", checkpoint)
    with pytest.raises(identity_guard.BindingCheckpointError):
        accepted(rpc, 22, OWNER, 16)
    assert OWNER not in state.data["email_key_bindings"]
    checkpoint.assert_called_once_with(rotate=False)


def test_failed_checkpoint_leaves_incoming_message_unseen_for_retry(enabled, monkeypatch):
    import state_backup
    rpc = enabled
    monkeypatch.setenv("STATE_BACKUP_REQUIRED", "1")
    monkeypatch.setattr(state_backup, "save", mock.Mock(side_effect=RuntimeError("synthetic failure")))
    done = mock.Mock()
    monkeypatch.setattr(app, "_done", done)
    incoming = msg(22, 16, ident=456)
    app._taken.discard(incoming.id)
    app._accept(SimpleNamespace(rpc=rpc), 1, incoming)
    done.assert_not_called()
    assert incoming.id not in app._taken
    assert OWNER not in state.data["email_key_bindings"]


def test_backup_admin_can_manage_controls_but_maintenance_still_stops_guest_work(enabled, monkeypatch):
    assert access.service_allowed(BACKUP) and admin.is_admin(BACKUP)
    monkeypatch.setattr(config, "MAINTENANCE_MODE", True)
    assert access.service_allowed(BACKUP)  # admin controls, not ordinary work
    assert not access.service_allowed(GUEST)
    assert not access.set_owner_only(False, 60, access.snapshot()["nonce"])
    state.data["maintenance_notice"] = {"active": True, "generation": 1}
    assert state.maintenance_active()
    with pytest.raises(state.WorkPaused):
        with state.guard_work(BACKUP):
            pass


def test_candidate_inspection_does_not_mutate_or_authorize_delivery(enabled, monkeypatch):
    rpc = enabled
    state.data.pop("email_key_bindings", None)
    save = mock.Mock()
    monkeypatch.setattr(state, "save", save)
    msg = SimpleNamespace(id=32, chat_id=22, from_id=16, show_padlock=True)
    assert identity_guard.authorized_chat(rpc, 1, 22, OWNER, contact_id=16, message=msg, _inspect_recovery=True)
    assert "email_key_bindings" not in state.data
    save.assert_not_called()
    assert not identity_guard.authorized_chat(rpc, 1, 22, OWNER, contact_id=16)
    assert not identity_guard.authorized_chat(rpc, 1, 22, OWNER, contact_id=16, _inspect_recovery=True)


def test_bootstrap_uses_newest_encrypted_incoming_metadata_without_reading_text(enabled, monkeypatch):
    rpc = enabled
    # Inspect the newer contact before the older one, even when chat 22 is
    # absent from the ordinary chat list. No key may bind during the scan.
    rpc.contacts = {16: rpc.contacts[16], 15: rpc.contacts[15], 17: rpc.contacts[17]}
    saves = []
    monkeypatch.setattr(state, "save", lambda: saves.append(copy.deepcopy(state.data["email_key_bindings"])))
    class MetadataOnly:
        def __init__(self, ident, chat_id, contact, timestamp):
            self.id, self.chat_id, self.from_id = ident, chat_id, contact
            self.received_timestamp, self.show_padlock, self.is_info = timestamp, True, False
        @property
        def text(self):
            raise AssertionError("bootstrap must not inspect message text")
    rpc.messages = {31: MetadataOnly(31, 21, 15, 100), 32: MetadataOnly(32, 22, 16, 200)}
    rpc.chat_messages = {21: [31], 22: [32]}
    # The replacement chat is deliberately omitted from the normal chat list.
    monkeypatch.setattr(rpc, "get_chatlist_entries", lambda *_args: [21])
    monkeypatch.setattr(admin, "_set_blocked_contact", lambda *_args: True)
    monkeypatch.setattr(chat, "recipient_guard", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(chat, "recipient_allowed", lambda *_args: True)
    monkeypatch.setattr(app, "_welcome_locale", lambda *_args: "es")
    welcome = mock.Mock()
    monkeypatch.setattr(miniapp, "welcome", welcome)
    previous_sender = chat.sender()
    try:
        app._bootstrap_email_recovery(SimpleNamespace(rpc=rpc), 1)
    finally:
        chat.set_sender(previous_sender)
    assert identity_guard.current_email_chat(OWNER) == 22
    assert len(saves) == 1 and saves[0][OWNER]["chat"] == 22
    welcome.assert_called_once_with(22, "es")
    assert not accepted(rpc, 21, OWNER, 15)


def test_bootstrap_finishes_previously_bound_pending_contact_request(enabled, monkeypatch):
    rpc = enabled
    assert accepted(rpc, 22, OWNER, 16)
    rpc.info[22].is_contact_request = True
    monkeypatch.setattr(admin, "_set_blocked_contact", lambda *_args: True)
    monkeypatch.setattr(chat, "recipient_guard", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(chat, "recipient_allowed", lambda *_args: True)
    monkeypatch.setattr(app, "_welcome_locale", lambda *_args: "es")
    welcome = mock.Mock()
    monkeypatch.setattr(miniapp, "welcome", welcome)
    previous_sender = chat.sender()
    try:
        app._bootstrap_email_recovery(SimpleNamespace(rpc=rpc), 1)
    finally:
        chat.set_sender(previous_sender)
    assert not rpc.info[22].is_contact_request
    welcome.assert_called_once_with(22, "es")


def test_identity_cutover_never_uses_weakened_email_mode_for_new_identity(enabled):
    assert config.EMAIL_KEY_RECOVERY
    with cutover.strict_owner(OWNER, OLD):
        assert config.BOT_VERIFIED_CONTACTS_ONLY
        assert not config.EMAIL_KEY_RECOVERY
    assert config.EMAIL_KEY_RECOVERY


def test_sealed_boot_accepts_rotated_owner_only_when_bot_identity_still_matches(enabled, tmp_path, monkeypatch):
    import json
    marker = {"version": 1, "confirmed": True, "epoch": "a" * 48, "owner_address": OWNER,
              "owner_fingerprint": OLD, "bot_fingerprint": THIRD, "owner_chat": 21}
    (tmp_path / cutover.MARKER).write_text(json.dumps(marker), encoding="utf-8")
    storage = SimpleNamespace(sealed=lambda: True)
    monkeypatch.setattr(cutover.relay, "identity", lambda _p: {"transports": {"one"}, "fingerprint": THIRD})
    assert cutover.activate(tmp_path, storage, OWNER) == marker
    monkeypatch.setattr(cutover.relay, "identity", lambda _p: {"transports": {"one"}, "fingerprint": NEW})
    with pytest.raises(cutover.CutoverError, match="identity"):
        cutover.activate(tmp_path, storage, OWNER)
    monkeypatch.setattr(cutover.relay, "identity", lambda _p: {"transports": {"one"}, "fingerprint": THIRD})
    with pytest.raises(cutover.CutoverError, match="probe"):
        cutover.validate_candidate(tmp_path, {"verified": True, "owner_address": OWNER, "owner_fingerprint": OLD,
                                              "bot_fingerprint": THIRD, "chat_id": 21}, OWNER,
                                   require_probe=True, allow_owner_rotation=True)
