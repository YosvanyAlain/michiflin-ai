"""Intake and bridge safeguards; fake accounts and files only."""
import hashlib
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from michiflin import access, admin, app, chat, config, filekit, filekit_transfer, miniapp, progress, state


class FileKitIntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="filekit-intake-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "attachment.txt"
        self.path.write_bytes(b"contenido")
        self.address = "owner@example.invalid"
        # _process deliberately sets the current sender and queue locale; keep
        # those thread/global contexts from leaking into later test modules.
        self.addCleanup(chat.set_sender, chat.sender())
        self.addCleanup(app.queue.set_locale, app.queue._locale)
        patches = [mock.patch.object(config, "JOB_ROOT", self.root / "jobs"), mock.patch.object(config, "MIN_FREE_DISK_BYTES", 0),
                   mock.patch.object(access, "session_stamp", return_value="test-session"),
                   mock.patch.object(state, "work_interrupted", return_value=False),
                   mock.patch.object(state, "check_work_allowed"), mock.patch.object(state, "get_kill_switch", return_value=False),
                   mock.patch.object(state, "guard_work", side_effect=lambda *_a: nullcontext()),
                   mock.patch.object(chat, "recipient_guard", side_effect=lambda *_a: nullcontext()),
                   mock.patch.object(chat, "recipient_allowed", return_value=True),
                   mock.patch.object(admin, "is_admin", return_value=True), mock.patch.object(admin, "is_allowed", return_value=True),
                   mock.patch.object(progress, "begin"), mock.patch.object(progress, "end"),
                   mock.patch.object(chat, "text"), mock.patch.object(chat, "delete"), mock.patch.object(app.agent, "handle_attachment")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        filekit_transfer._pending.clear()
        self.addCleanup(filekit_transfer._pending.clear)
        self.msg = SimpleNamespace(id=10, chat_id=11, from_id=42, file=str(self.path), file_mime="text/plain")

    def test_only_explicit_marked_attachment_enters_kit_and_is_never_deleted(self):
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        draft = filekit_transfer.prepare_import(self.address, "", "copia.txt", 9, digest)
        app._process(self.msg, draft["caption"], "es", self.address)
        self.assertEqual(filekit.Workspace(self.address).content("/copia.txt")[1].read_bytes(), b"contenido")
        self.assertEqual(self.path.read_bytes(), b"contenido")
        chat.delete.assert_not_called()
        app.agent.handle_attachment.assert_not_called()
        self.assertIn("original", chat.text.call_args.args[1])
        # Existing attachment handling stays unchanged for non-marker captions.
        app._process(self.msg, "texto ordinario", "es", self.address)
        app.agent.handle_attachment.assert_called_once()
        chat.delete.assert_called_once_with([10])

    def test_invalid_marker_is_not_sent_to_agent_or_deleted(self):
        app._process(self.msg, filekit_transfer.MARKER + "not-a-valid-token", "en", self.address)
        chat.delete.assert_not_called()
        app.agent.handle_attachment.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b"contenido")
        self.assertEqual(filekit.Workspace(self.address).view()["entries"], [])

    def test_old_session_and_maintenance_block_filekit_before_mutation(self):
        entry = {"chat": 11, "addr": self.address, "build": miniapp.app_build()}
        request = {"id": "stale1", "op": "filekit", "args": {"action": "mkdir", "folder": "", "name": "no"},
                   "lang": "en", "session": "old-session"}
        with mock.patch.object(miniapp, "_admit", return_value="new"), mock.patch.object(access, "bound_allowed", return_value=True), \
             mock.patch.object(miniapp, "respond") as response, mock.patch.object(miniapp._pool, "submit") as submit:
            miniapp._route(1, entry, request)
            submit.assert_not_called()
            self.assertEqual(response.call_args.kwargs["code"], "session_changed")
        with mock.patch.object(access, "bound_allowed", return_value=True), mock.patch.object(miniapp, "_verified_binding", return_value=True), \
             mock.patch.object(config, "service_access_allowed", return_value=True), mock.patch.object(state, "maintenance_active", return_value=True), \
             mock.patch.object(miniapp, "respond") as response, mock.patch.object(miniapp, "op_filekit") as operation:
            miniapp.handle(1, entry, request)
            operation.assert_not_called()
            self.assertIn("maintenance", response.call_args.args[3].lower())
        self.assertEqual(filekit.Workspace(self.address).view()["entries"], [])


if __name__ == "__main__":
    unittest.main()
