"""Synthetic file-kit deliveries reuse the normal queue and mailbox ceilings."""
import copy
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from michiflin import access, chat, config, mail_profiles, queue, state, usage


class LocalDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="filekit-delivery-test-")
        self.addCleanup(self.tmp.cleanup)
        self.original_state = copy.deepcopy(state.data)
        self.addCleanup(self.restore_state)
        state.data["jobs"] = {}
        state.data["settings"] = {}
        state.data["flags"]["job_seq"] = 0
        patches = [
            mock.patch.object(config, "JOB_ROOT", Path(self.tmp.name) / "jobs"),
            mock.patch.object(state, "save"), mock.patch.object(state, "check_work_allowed"),
            mock.patch.object(state, "work_interrupted", return_value=False),
            mock.patch.object(queue, "_capacity_error", return_value=""),
            mock.patch.object(queue, "_status"), mock.patch.object(queue, "_status_done"),
            mock.patch.object(access, "job_allowed", return_value=True),
            mock.patch.object(usage, "note"),
            mock.patch.object(mail_profiles, "effective_settings", side_effect=lambda _address, values: dict(values)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(chat.set_sender, chat.sender())
        chat.set_sender("owner@example.invalid")

    def restore_state(self):
        with state.lock:
            state.data.clear()
            state.data.update(self.original_state)

    def test_splits_with_mailbox_ceiling_and_does_not_touch_original(self):
        original = b"a" * (2 * 1024 * 1024 + 31)
        with mock.patch.object(state, "get_nauta_strict", return_value=True):
            job, error = queue.enqueue_local(11, io.BytesIO(original), "clip.dat", len(original), "application/octet-stream", "es", part_mb=1)
        self.assertFalse(error)
        self.assertEqual(job["part_mb"], 1)
        self.assertTrue(job["send_requested"])
        queue._prepare(job)
        self.assertEqual(job["status"], "sending")
        self.assertEqual(job["sha"], hashlib.sha256(original).hexdigest())
        parts = [Path(path).read_bytes() for path in job["part_files"]]
        self.assertEqual(b"".join(parts), original)
        self.assertTrue(all(len(part) <= 1024 * 1024 for part in parts))
        self.assertEqual(original, b"a" * (2 * 1024 * 1024 + 31))

    def test_explicit_twenty_is_clamped_to_ten_and_empty_file_survives(self):
        with mock.patch.object(state, "get_nauta_strict", return_value=True):
            job, error = queue.enqueue_local(11, io.BytesIO(b""), "vacio.txt", 0, "text/plain", "es", part_mb=20)
        self.assertFalse(error)
        self.assertEqual(job["part_mb"], 10)
        queue._prepare(job)
        self.assertEqual(job["status"], "sending")
        self.assertEqual(Path(job["part_files"][0]).read_bytes(), b"")

    def test_exported_video_is_split_as_exact_bytes_without_remux(self):
        data = b"not-a-real-video" * 100000
        job, error = queue.enqueue_local(11, io.BytesIO(data), "clip.mp4", len(data), "video/mp4", "es", part_mb=1)
        self.assertFalse(error)
        job["part_format"] = "raw"
        with mock.patch.object(queue, "_segments") as segments:
            queue._prepare(job)
        segments.assert_not_called()
        self.assertEqual(b"".join(Path(path).read_bytes() for path in job["part_files"]), data)
        self.assertEqual(job["sha"], hashlib.sha256(data).hexdigest())

    def test_accounting_failure_after_commit_does_not_duplicate_or_erase_job(self):
        with mock.patch.object(usage, "note", side_effect=OSError("synthetic")):
            job, error = queue.enqueue_local(11, io.BytesIO(b"hello"), "a.txt", 5, "text/plain", "es")
        self.assertFalse(error)
        self.assertEqual(len(state.data["jobs"]), 1)
        self.assertEqual((queue.folder(job) / "src" / "download.bin").read_bytes(), b"hello")

    def test_missing_ephemeral_source_is_held_without_network_or_resend(self):
        data = b"copia temporal"
        job, error = queue.enqueue_local(11, io.BytesIO(data), "archivo.bin", len(data), "application/octet-stream", "es")
        self.assertFalse(error)
        (queue.folder(job) / "src" / "download.bin").unlink()
        with mock.patch.object(queue.files, "download") as download:
            queue._prepare(job)
        download.assert_not_called()
        self.assertTrue(job["hold"])
        self.assertEqual(job["error"], "source_preservation")
        self.assertEqual(job["status"], "queued")

    def test_partial_copy_does_not_create_a_job(self):
        with self.assertRaisesRegex(ValueError, "source_changed"):
            queue.enqueue_local(11, io.BytesIO(b"abc"), "a.txt", 10, "text/plain", "es")
        self.assertEqual(state.data["jobs"], {})
        self.assertEqual(list(config.JOB_ROOT.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
