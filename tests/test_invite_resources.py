"""Concurrent admission and disk safeguards use only synthetic data."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from michiflin import access, chat, config, files, queue, state


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OWNER_ADDRESS", "owner@example.invalid")
    monkeypatch.setattr(config, "OWNER_BACKUP_ADDRESS", "")
    monkeypatch.setattr(config, "OWNER_ONLY", False)
    monkeypatch.setattr(config, "INVITE_AUTO_ACCESS", False)
    monkeypatch.setattr(config, "MAINTENANCE_MODE", False)
    monkeypatch.setattr(config, "JOB_ROOT", tmp_path)
    monkeypatch.setattr(config, "MIN_FREE_DISK_BYTES", 0)
    monkeypatch.setattr(config, "MAX_QUEUE_BYTES", 1000)
    monkeypatch.setattr(config, "MAX_FILE_BYTES", 1000)
    monkeypatch.setattr(queue, "free_disk", lambda: 100_000_000)
    monkeypatch.setattr(queue, "queue_bytes", lambda: 0)
    original = state.setting
    monkeypatch.setattr(state, "setting", lambda key: 5 if key == "user_jobs" else original(key))
    monkeypatch.setattr(state, "save", lambda: None)
    monkeypatch.setitem(state.data, "jobs", {})
    queue._pending_admissions.clear()
    access.reset()
    yield
    queue._pending_admissions.clear()
    chat.set_sender("")
    access.reset()


def test_atomic_person_reservations_under_concurrent_admission():
    barrier = threading.Barrier(12)
    def enter(_):
        chat.set_sender("guest@example.invalid")
        barrier.wait(timeout=5)
        return queue._reserve_admission(1, "en")
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(enter, range(12)))
    tickets = [ticket for ticket, _ in results if ticket]
    assert len(tickets) == 5 and len(set(tickets)) == 5
    assert len(queue._pending_admissions) == 5
    for ticket in tickets:
        queue._release_admission(ticket)
    chat.set_sender("guest@example.invalid")
    ticket, error = queue._reserve_admission(1, "en")
    assert ticket and not error
    queue._release_admission(ticket)


def test_queue_capacity_is_reserved_across_people(monkeypatch):
    monkeypatch.setattr(config, "MAX_QUEUE_BYTES", 10)
    barrier = threading.Barrier(2)
    def enter(index):
        chat.set_sender(f"guest{index}@example.invalid")
        barrier.wait(timeout=5)
        return queue._reserve_admission(6, "en")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(enter, range(2)))
    tickets = [ticket for ticket, _ in results if ticket]
    assert len(tickets) == 1
    assert any("full" in error for ticket, error in results if not ticket)
    queue._release_admission(tickets[0])


def test_unknown_length_download_stops_before_the_disk_reserve(monkeypatch, tmp_path):
    class Response:
        status_code = 200
        headers = {}
        def iter_content(self, chunk_size):
            yield b"first"
            yield b"second"
        def close(self):
            pass
    monkeypatch.setattr(files.net, "fetch", lambda *_args, **_kwargs: Response())
    frees = iter([100, 9])
    monkeypatch.setattr(files.shutil, "disk_usage", lambda _path: SimpleNamespace(free=next(frees)))
    target = tmp_path / "partial"
    with pytest.raises(ValueError, match="disk_full"):
        files.download("https://example.invalid/f", target, 0, False, max_bytes=1000, min_free_bytes=5)
    assert target.read_bytes() == b"first"  # no second block written; partial is retained
