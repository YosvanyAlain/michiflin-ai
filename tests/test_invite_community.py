"""Invitation-only community admission, safety and resource fairness (synthetic data)."""
from __future__ import annotations

import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from michiflin import access, admin, app, chat, community, config, filekit, filekit_archive, identity_guard, miniapp, queue, state

OWNER = "owner@example.invalid"
BACKUP = "backup@example.invalid"
ALICE = "alice@example.invalid"
BOB = "bob@example.invalid"
FINGERPRINT = "A" * 40


class Rpc:
    def __init__(self, address=ALICE):
        self.contact = SimpleNamespace(address=address, is_verified=True, verifier_id=1,
                                       is_key_contact=True, e2ee_avail=True)
        self.info = SimpleNamespace(chat_type="Single", is_encrypted=True, is_self_talk=False,
                                    is_contact_request=False)
        self.members = [15]
        self.fingerprint = FINGERPRINT
        self.accepted = False

    def get_contact(self, *_args):
        return self.contact

    def create_chat_by_contact_id(self, *_args):
        return 21

    def get_basic_chat_info(self, *_args):
        return self.info

    def get_chat_contacts(self, *_args):
        return self.members

    def accept_chat(self, *_args):
        self.accepted = True
        self.info.is_contact_request = False


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    access.reset()
    for key, value in {
        "OWNER_ADDRESS": OWNER, "OWNER_BACKUP_ADDRESS": BACKUP, "ADMIN_ADDRESSES": {OWNER, BACKUP},
        "OWNER_ONLY": False, "MAINTENANCE_MODE": False, "INVITE_AUTO_ACCESS": True,
        "EMAIL_KEY_RECOVERY": True, "BOT_EPOCH_STRICT": False,
        "ALLOWED_SENDERS": {"legacy@example.invalid"},
    }.items():
        monkeypatch.setattr(config, key, value)
    for key, value in {
        "invitees": {}, "invite_policy": {}, "apps": {}, "app_seen": {}, "jobs": {},
        "settings": {}, "prefs": {}, "people": {}, "flags": {}, "kill_switch": {}, "sent": [],
        "access": {"allowed": ["legacy@example.invalid"], "blocked": [], "attempts": {}, "alerts": True},
    }.items():
        monkeypatch.setitem(state.data, key, value)
    monkeypatch.setattr(state, "save", lambda: None)
    monkeypatch.setattr(community, "_checkpoint", lambda: None)
    monkeypatch.setattr(identity_guard, "contact_fingerprint", lambda rpc, *_: rpc.fingerprint)
    monkeypatch.setattr(miniapp, "_answers", {})
    community._pending.clear()
    miniapp._dispatch_pending.clear()
    queue._prepare_turns.clear()
    queue._delivery_turns.clear()
    queue._delivery_job_turns.clear()
    queue._claim_generations.clear()
    queue._delivery_generations.clear()
    chat.set_sender("")
    yield
    community._pending.clear()
    miniapp._dispatch_pending.clear()
    queue._prepare_turns.clear()
    queue._delivery_turns.clear()
    queue._delivery_job_turns.clear()
    queue._claim_generations.clear()
    queue._delivery_generations.clear()
    chat.set_sender("")
    access.reset()


def test_invite_grants_key_bound_person_not_legacy_users_or_admin():
    rpc = Rpc()
    assert not admin.is_allowed(ALICE)
    assert not admin.is_allowed("legacy@example.invalid")
    assert admin.is_allowed(OWNER) and admin.is_allowed(BACKUP)
    assert community.admit_from_securejoin(rpc, 1, 15)
    assert admin.is_allowed(ALICE) and not admin.is_admin(ALICE)
    assert identity_guard.authorized_chat(rpc, 1, 21, ALICE, contact_id=15)
    stamp = access.session_stamp(ALICE)
    assert stamp and access.bound_allowed({"addr": ALICE, "chat": 21, "access_session": stamp})
    assert not access.bound_allowed({"addr": ALICE, "chat": 21, "access_session": "legacy"})
    assert not identity_guard.authorized_chat(rpc, 1, 21, BOB)
    assert not admin.is_allowed("legacy@example.invalid")
    old = copy.deepcopy(state.data["invitees"])
    assert community.admit_from_securejoin(rpc, 1, 15)
    assert state.data["invitees"] == old  # retry of the same event is idempotent


@pytest.mark.parametrize("field,value", [
    ("is_verified", False), ("verifier_id", 9), ("is_key_contact", False), ("e2ee_avail", False),
])
def test_only_directly_verified_contacts_are_admitted(field, value):
    rpc = Rpc()
    setattr(rpc.contact, field, value)
    assert not community.admit_from_securejoin(rpc, 1, 15)
    assert not admin.is_allowed(ALICE)


@pytest.mark.parametrize("field,value", [("chat_type", "Group"), ("is_encrypted", False), ("is_self_talk", True)])
def test_groups_unencrypted_and_self_chats_fail_closed(field, value):
    rpc = Rpc()
    setattr(rpc.info, field, value)
    assert not community.admit_from_securejoin(rpc, 1, 15)
    assert not admin.is_allowed(ALICE)


def test_contact_request_accepted_only_after_verified_securejoin():
    rpc = Rpc()
    rpc.info.is_contact_request = True
    assert community.admit_from_securejoin(rpc, 1, 15)
    assert rpc.accepted and admin.is_allowed(ALICE)
    assert identity_guard.authorized_chat(rpc, 1, 21, ALICE)


def test_invitee_rekey_requires_a_new_securejoin_and_invalidates_old_instance():
    rpc = Rpc()
    assert community.admit_from_securejoin(rpc, 1, 15)
    old = access.session_stamp(ALICE)
    rpc.fingerprint = "B" * 40
    assert not identity_guard.authorized_chat(rpc, 1, 21, ALICE, allow_rotation=True)
    assert community.admit_from_securejoin(rpc, 1, 15)
    new = access.session_stamp(ALICE)
    assert new != old
    assert not access.bound_allowed({"addr": ALICE, "chat": 21, "access_session": old})
    assert access.bound_allowed({"addr": ALICE, "chat": 21, "access_session": new})
    assert identity_guard.authorized_chat(rpc, 1, 21, ALICE)


def test_failed_checkpoint_rolls_back_grant_and_retry_works(monkeypatch):
    rpc = Rpc()
    monkeypatch.setattr(community, "_checkpoint", lambda: (_ for _ in ()).throw(OSError("offline")))
    with pytest.raises(community.CheckpointError):
        community.admit_from_securejoin(rpc, 1, 15)
    assert ALICE not in state.data["invitees"] and not admin.is_allowed(ALICE)
    monkeypatch.setattr(community, "_checkpoint", lambda: None)
    assert community.admit_from_securejoin(rpc, 1, 15) and admin.is_allowed(ALICE)


def test_provisional_grant_is_invisible_and_emergency_close_wins(monkeypatch):
    rpc = Rpc()
    entered, release = threading.Event(), threading.Event()
    def checkpoint():
        entered.set()
        assert release.wait(3)
    monkeypatch.setattr(community, "_checkpoint", checkpoint)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(community.admit_from_securejoin, rpc, 1, 15)
        assert entered.wait(3)
        assert not admin.is_allowed(ALICE)
        access.close_immediately()
        release.set()
        assert future.result(timeout=3)
    assert not admin.is_allowed(ALICE)
    monkeypatch.setattr(community, "_checkpoint", lambda: None)
    assert access.set_owner_only(True)
    access.reset()
    access.restore_policy()
    assert access.snapshot()["owner_only"] and not admin.is_allowed(ALICE)
    nonce = access.snapshot()["nonce"]
    assert access.set_owner_only(False, -1, nonce)
    assert admin.is_allowed(ALICE)
    access.reset()
    access.restore_policy()
    assert admin.is_allowed(ALICE) and not access.snapshot()["owner_only"]


def test_maintenance_and_explicit_ban_override_invitation(monkeypatch):
    rpc = Rpc()
    monkeypatch.setattr(config, "MAINTENANCE_MODE", True)
    assert not community.admit_from_securejoin(rpc, 1, 15)
    assert admin.is_allowed(OWNER) and not admin.is_allowed(ALICE)
    monkeypatch.setattr(config, "MAINTENANCE_MODE", False)
    assert community.admit_from_securejoin(rpc, 1, 15)
    assert community.revoke(ALICE, block=True)
    assert not admin.is_allowed(ALICE) and not community.admit_from_securejoin(rpc, 1, 15)
    assert community.unblock(ALICE)
    assert not admin.is_allowed(ALICE)  # unblocking alone does not restore a key
    assert community.admit_from_securejoin(rpc, 1, 15) and admin.is_allowed(ALICE)


def test_unknown_language_gets_one_bilingual_app_and_restart_does_not_duplicate(monkeypatch):
    assert community.admit_from_securejoin(Rpc(), 1, 15)
    chat.set_sender(ALICE)
    monkeypatch.setattr(miniapp, "_verified_binding", lambda _entry: True)
    monkeypatch.setattr(miniapp, "app_build", lambda: "synthetic-build")
    monkeypatch.setattr(miniapp, "build", lambda: "/synthetic/app.xdc")
    sends = []
    def send(chat_id, text, **kwargs):
        sends.append((chat_id, text, kwargs))
        return 100 + len(sends)
    monkeypatch.setattr(chat, "send", send)
    miniapp.welcome(21, "")
    assert len(sends) == 1
    assert "¡Hola!" in sends[0][1] and "Hi!" in sends[0][1]
    assert "Descarga" in sends[0][1] and "Download" in sends[0][1] and "/app" not in sends[0][1]
    assert sends[0][2]["viewtype"] == "Webxdc"
    # A legacy pending secondary welcome must never be sent after the upgrade.
    state.data["apps"]["101"]["welcome_secondary_pending"] = True
    miniapp.welcome(21, "")
    access.reset()
    access.restore_policy()
    miniapp.welcome(21, "en")
    assert len(sends) == 1 and len(state.data["apps"]) == 1


@pytest.mark.parametrize("language,expected,other", [("es", "¡Hola!", "Hi!"), ("en", "Hi!", "¡Hola!")])
def test_known_language_gets_one_localized_welcome(monkeypatch, language, expected, other):
    assert community.admit_from_securejoin(Rpc(), 1, 15)
    chat.set_sender(ALICE)
    monkeypatch.setattr(miniapp, "_verified_binding", lambda _entry: True)
    monkeypatch.setattr(miniapp, "app_build", lambda: "synthetic-build")
    monkeypatch.setattr(miniapp, "build", lambda: "/synthetic/app.xdc")
    sent = []
    monkeypatch.setattr(chat, "send", lambda _chat, text, **kwargs: sent.append((text, kwargs)) or 101)
    miniapp.welcome(21, language)
    assert len(sent) == 1 and expected in sent[0][0] and other not in sent[0][0]
    assert sent[0][1]["viewtype"] == "Webxdc" and "/app" not in sent[0][0]


def test_unknown_welcome_includes_both_maintenance_notices_in_one_message(monkeypatch):
    assert community.admit_from_securejoin(Rpc(), 1, 15)
    chat.set_sender(ALICE)
    monkeypatch.setattr(miniapp, "_verified_binding", lambda _entry: True)
    monkeypatch.setattr(miniapp, "app_build", lambda: "synthetic-build")
    monkeypatch.setattr(miniapp, "build", lambda: "/synthetic/app.xdc")
    monkeypatch.setattr(miniapp.maintenance, "text", lambda language: {"es": "Pausa de prueba", "en": "Test pause"}[language])
    sent = []
    monkeypatch.setattr(chat, "send", lambda _chat, text, **kwargs: sent.append((text, kwargs)) or 101)
    miniapp.welcome(21, "")
    assert len(sent) == 1 and "Pausa de prueba" in sent[0][0] and "Test pause" in sent[0][0]


def test_new_guest_language_is_not_guessed_from_the_owners_setting(monkeypatch):
    assert community.admit_from_securejoin(Rpc(), 1, 15)
    chat.set_sender(ALICE)
    monkeypatch.setattr(app, "_last_locale", {})
    state.data["settings"]["language"] = "es"  # shared setting is not the new guest's device language
    assert app._welcome_locale(21) == ""
    state.data["prefs"][ALICE] = {"language": "en"}
    assert app._welcome_locale(21) == "en"
    state.data["prefs"][ALICE]["language"] = "auto"
    app._last_locale[21] = "es"
    assert app._welcome_locale(21) == "es"
    app._last_locale.clear()
    assert app._welcome_locale(21) == ""
    state.data["apps"]["101"] = {"chat": 21, "addr": ALICE, "epoch": config.BOT_EPOCH,
                                "access_session": access.session_stamp(ALICE),
                                "locale": "en", "active": 100, "language_observed": True}
    assert app._welcome_locale(21) == "en"
    state.data["apps"]["101"]["language_observed"] = False
    assert app._welcome_locale(21) == ""  # the initial fallback alone is not evidence


def test_dispatch_per_person_limits_and_reserved_control_capacity():
    tickets = [miniapp._dispatch_acquire(ALICE, "ask")[0] for _ in range(miniapp._PERSON_LIMITS["normal"])]
    assert all(tickets) and not miniapp._dispatch_acquire(ALICE, "ask")[0]
    bob, bob_lane = miniapp._dispatch_acquire(BOB, "ask")
    owner, owner_lane = miniapp._dispatch_acquire(OWNER, "admin")
    owner_work, owner_work_lane = miniapp._dispatch_acquire(OWNER, "ask")
    pause, pause_lane = miniapp._dispatch_acquire(ALICE, "kill_switch")
    assert (bob_lane, owner_lane, owner_work_lane, pause_lane) == ("normal", "owner", "owner_work", "pause")
    assert all((bob, owner, owner_work, pause))
    for ticket in tickets + [bob, owner, owner_work, pause]:
        miniapp._dispatch_release(ticket)
    assert not miniapp._dispatch_pending


def test_timed_out_worker_keeps_its_reservation_until_it_really_finishes(monkeypatch):
    assert community.admit_from_securejoin(Rpc(), 1, 15)
    stamp = access.session_stamp(ALICE)
    entry = {"chat": 21, "addr": ALICE, "epoch": config.BOT_EPOCH,
             "build": "synthetic-build", "access_session": stamp}
    state.data["apps"]["101"] = entry
    monkeypatch.setattr(miniapp, "_verified_binding", lambda _entry: True)
    monkeypatch.setattr(miniapp, "app_build", lambda: "synthetic-build")
    monkeypatch.setitem(miniapp.OP_LIMITS, "slow_test", 0.05)
    running, finish, replied = threading.Event(), threading.Event(), threading.Event()
    def run(_chat, _args, _locale):
        running.set()
        assert finish.wait(4)
        return {"ok": True}
    monkeypatch.setitem(miniapp.OPS, "slow_test", run)
    monkeypatch.setattr(miniapp, "respond", lambda *_args, **_kwargs: replied.set())
    miniapp._route(101, entry, {"id": "timeout123", "op": "slow_test", "lang": "en", "session": miniapp.BOOT_SESSION})
    try:
        assert running.wait(2) and replied.wait(2)
        assert len(miniapp._dispatch_pending) == 1 and miniapp.busy() >= 1
    finally:
        finish.set()
    deadline = time.monotonic() + 3
    while miniapp._dispatch_pending and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not miniapp._dispatch_pending


def test_replay_ledger_has_per_person_quota_and_admin_reserve(monkeypatch):
    monkeypatch.setattr(miniapp, "SEEN_LIMIT", 5)
    monkeypatch.setattr(miniapp, "SEEN_PER_PERSON", 3)
    monkeypatch.setattr(miniapp, "SEEN_ADMIN_RESERVE", 2)
    state.data["apps"].update({"101": {"addr": ALICE}, "102": {"addr": BOB}, "103": {"addr": OWNER}})
    for index in range(3):
        assert miniapp._admit(f"a{index}", 0, 101) == "new"
    assert miniapp._admit("a3", 0, 101) == "full"
    assert miniapp._admit("b0", 0, 102) == "new"
    assert miniapp._admit("b1", 0, 102) == "new"
    assert miniapp._admit("b2", 0, 102) == "full"
    assert miniapp._admit("o0", 0, 103) == "new"
    assert miniapp._admit("o1", 0, 103) == "new"
    assert miniapp._admit("o2", 0, 103) == "full"
    assert miniapp._admit("a0", 1, 102) == "replay"  # no cross-instance result leak


def test_fair_preparation_and_delivery_turns(monkeypatch):
    monkeypatch.setattr(queue, "_blocked", lambda _job: False)
    monkeypatch.setattr(queue, "in_window", lambda *_a, **_k: True)
    monkeypatch.setattr(access, "job_allowed", lambda _job: True)
    def make(i, owner, status):
        return {"id": str(i), "addr": owner, "chat": 20 + i, "status": status, "hold": False,
                "todo": [0, 1], "not_before": 0}
    jobs = [make(1, ALICE, "queued"), make(2, ALICE, "queued"), make(3, BOB, "queued")]
    state.data["jobs"] = {x["id"]: x for x in jobs}
    assert queue._next_queued()["addr"] == ALICE
    assert queue._next_queued()["addr"] == BOB
    for job in jobs:
        job["status"] = "sending"
    served = [queue._deliverable()["id"] for _ in range(4)]
    assert served == ["1", "3", "2", "3"]  # alternating people and each person's jobs


def test_archive_memory_reservations_account_for_concurrent_workers(monkeypatch):
    monkeypatch.setattr(filekit_archive, "_available_memory", lambda: 1024 * 1024 * 1024)
    entered, release = threading.Event(), threading.Event()
    def first():
        with filekit_archive.memory_reservation():
            entered.set()
            assert release.wait(3)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first)
        assert entered.wait(2)
        try:
            with pytest.raises(filekit.FileKitError, match="memory_unavailable"):
                with filekit_archive.memory_reservation():
                    pass
        finally:
            release.set()
        future.result(timeout=3)
    with filekit_archive.memory_reservation():
        assert filekit_archive.memory_cap() > 0
    assert filekit_archive._memory_reserved == 0
