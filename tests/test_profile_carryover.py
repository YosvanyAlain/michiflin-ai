"""Profile-only migration never copies an old identity or advertises an old link."""
from contextlib import contextmanager
from pathlib import Path

import pytest

from tools import profile_carryover_once as carry


def test_merge_description_preserves_text_but_filters_old_addresses_and_invites():
    old = """Old useful description
old-bot@example.invalid
https://i.delta.chat/#x%26a%3Dold-bot%40example.invalid
https://github.com/YosvanyAlain/michiflin-ai"""
    current = "New useful description\n" + carry.profile.START_HINT
    merged = carry.merge_description(current, old, {"old-bot@example.invalid"})
    assert "Old useful description" in merged and "New useful description" in merged
    assert "old-bot@example.invalid" not in merged and "i.delta.chat" not in merged
    assert merged.count(carry.profile.REPOSITORY_URL) == 1 and "/app" in merged
    assert carry.merge_description(merged, old, {"old-bot@example.invalid"}) == merged


def test_profile_carryover_reads_only_offline_and_is_idempotent(tmp_path, monkeypatch):
    path = tmp_path / "active"
    path.mkdir()
    (path / "accounts").mkdir()
    class Storage:
        token = "synthetic"
        repo = "synthetic/private"
        password = "synthetic"
        release = {"id": 1}
        def download(self, asset):
            assert asset == 12
            return b"encrypted-synthetic"
    old = {"selfstatus": "Old useful description", "displayname": "Old name"}
    new = {"selfstatus": "New useful description"}
    class Rpc:
        def __init__(self, values): self.values = values
        def get_config(self, _acc, key): return self.values.get(key, "")
        def set_config(self, _acc, key, value): self.values[key] = value
    @contextmanager
    def account_rpc(folder: Path):
        yield Rpc(new if folder == path / "accounts" else old)
    monkeypatch.setattr(carry.cutover, "activate", lambda *_: {"legacy_asset_id": 12})
    monkeypatch.setattr(carry.state_backup, "_assets", lambda *_: [{"id": 12, "name": "michiflin-legacy-identity-synthetic"}])
    monkeypatch.setattr(carry.state_backup, "decrypt_backup", lambda *_: b"plaintext-synthetic")
    monkeypatch.setattr(carry.state_backup, "_verify_plaintext", lambda *_: None)
    monkeypatch.setattr(carry.state_backup, "_install_restored", lambda _raw, dest: dest.mkdir())
    monkeypatch.setattr(carry.relay, "identity", lambda folder: {"fingerprint": "NEW" if folder == path else "OLD",
                                                                 "transports": {"new@example.invalid" if folder == path else "old@example.invalid"}})
    monkeypatch.setattr(carry.relay, "account_rpc", account_rpc)
    monkeypatch.setattr(carry.relay, "only_account", lambda _rpc: 1)
    assert carry.carryover(path, Storage(), "owner@example.invalid") == "verified"
    assert "Old useful description" in new["selfstatus"] and "New useful description" in new["selfstatus"]
    assert old == {"selfstatus": "Old useful description", "displayname": "Old name"}
    assert carry.carryover(path, Storage(), "owner@example.invalid") == "already_verified"
    assert (path / carry.MARKER).is_file()


def test_carryover_does_not_write_when_the_old_archive_is_unavailable(tmp_path, monkeypatch):
    class Storage:
        token = "synthetic"; repo = "synthetic/private"; release = {"id": 1}
    monkeypatch.setattr(carry.cutover, "activate", lambda *_: {"legacy_asset_id": 12})
    monkeypatch.setattr(carry.state_backup, "_assets", lambda *_: [])
    with pytest.raises(ValueError):
        carry.carryover(tmp_path, Storage(), "owner@example.invalid")
    assert not (tmp_path / carry.MARKER).exists()
