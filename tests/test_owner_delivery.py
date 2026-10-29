"""One-shot encrypted owner document delivery: offline safeguards."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("owner_delivery", ROOT / ".github" / "scripts" / "deliver_owner_documents.py")
owner_delivery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(owner_delivery)


class OwnerDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.zip = self.root / "source.zip"
        with zipfile.ZipFile(self.zip, "w") as out:
            out.writestr("Michiflin-AI/bot.py", "fixture")
        self.pdf = self.root / "manual.pdf"
        self.pdf.write_bytes(b"%PDF-1.4\nsynthetic test document\n%%EOF\n")
        self.owner = "owner@example.org"
        self.revision = "a" * 40
        self.rpc = mock.Mock()
        self.rpc.lookup_contact_id_by_addr.return_value = 7
        self.rpc.get_contact.return_value = SimpleNamespace(address=self.owner, is_verified=True, e2ee_avail=True)
        self.rpc.get_chat_id_by_contact_id.return_value = 10
        self.rpc.get_basic_chat_info.return_value = SimpleNamespace(chat_type="Single", is_encrypted=True)
        self.rpc.send_msg.side_effect = [101, 102]
        self.rpc.get_message.return_value = SimpleNamespace(state=26, show_padlock=True)
        self.backup = mock.Mock()

    def run_delivery(self, **overrides):
        values = dict(rpc=self.rpc, account=1, root=self.root, owner=self.owner, expected=self.owner,
                      revision=self.revision, package=self.zip, manual=self.pdf, backup=self.backup, timeout=1)
        values.update(overrides)
        return owner_delivery.deliver(**values)

    def test_uses_two_encrypted_messages_and_is_not_repeated(self):
        self.run_delivery()
        self.assertEqual(self.rpc.send_msg.call_count, 2)
        self.assertEqual(self.backup.call_count, 6)  # intent, handoff, relay per document
        messages = [call.args[2] for call in self.rpc.send_msg.call_args_list]
        self.assertEqual([x.filename for x in messages], ["Michiflin_codigo_fuente.zip", "Manual_Michiflin.pdf"])
        self.run_delivery()
        self.assertEqual(self.rpc.send_msg.call_count, 2)
        ledger = json.loads((self.root / owner_delivery.LEDGER_NAME).read_text())
        record = next(iter(ledger["deliveries"].values()))
        self.assertEqual([record["documents"][key]["state"] for key in ("source", "manual")], ["delivered", "delivered"])

    def test_recipient_mismatch_sends_nothing(self):
        with self.assertRaisesRegex(owner_delivery.DeliveryError, "recipient_mismatch"):
            self.run_delivery(expected="somebody-else@example.org")
        self.rpc.lookup_contact_id_by_addr.assert_not_called()
        self.rpc.send_msg.assert_not_called()
        self.backup.assert_not_called()

    def test_unverified_or_unencrypted_chat_sends_nothing(self):
        for kind in ("unverified", "encrypted", "group"):
            with self.subTest(kind=kind):
                self.rpc.get_contact.return_value.is_verified = kind != "unverified"
                info = self.rpc.get_basic_chat_info.return_value
                info.is_encrypted = kind != "encrypted"
                info.chat_type = "Group" if kind == "group" else "Single"
                with self.assertRaisesRegex(owner_delivery.DeliveryError, "owner_chat_not_verified"):
                    self.run_delivery()
        self.rpc.send_msg.assert_not_called()
        self.backup.assert_not_called()

    def test_backup_failure_happens_before_handoff_and_holds_retry(self):
        self.backup.side_effect = RuntimeError("private test")
        with self.assertRaisesRegex(owner_delivery.DeliveryError, "backup_failed"):
            self.run_delivery()
        self.rpc.send_msg.assert_not_called()
        self.backup.side_effect = None
        with self.assertRaisesRegex(owner_delivery.DeliveryError, "delivery_uncertain"):
            self.run_delivery()
        self.rpc.send_msg.assert_not_called()

    def test_uncertain_handoff_is_not_repeated(self):
        self.rpc.send_msg.side_effect = RuntimeError("private transport error")
        with self.assertRaisesRegex(owner_delivery.DeliveryError, "delivery_uncertain"):
            self.run_delivery()
        self.assertEqual(self.rpc.send_msg.call_count, 1)
        with self.assertRaisesRegex(owner_delivery.DeliveryError, "delivery_uncertain"):
            self.run_delivery()
        self.assertEqual(self.rpc.send_msg.call_count, 1)

    def test_pending_handoff_is_reconciled_without_another_send(self):
        self.rpc.get_message.return_value = SimpleNamespace(state=24, show_padlock=True)
        with self.assertRaisesRegex(owner_delivery.DeliveryError, "delivery_failed"):
            self.run_delivery()
        self.assertEqual(self.rpc.send_msg.call_count, 1)
        self.rpc.get_message.return_value = SimpleNamespace(state=26, show_padlock=True)
        self.run_delivery()
        self.assertEqual(self.rpc.send_msg.call_count, 2)

    def test_cannot_claim_success_if_message_not_encrypted(self):
        self.rpc.get_message.return_value = SimpleNamespace(state=26, show_padlock=False)
        with self.assertRaisesRegex(owner_delivery.DeliveryError, "encryption_not_confirmed"):
            self.run_delivery()
        self.assertEqual(self.rpc.send_msg.call_count, 1)

    def test_invalid_pdf_and_symlink_ledger_fail_closed(self):
        self.pdf.write_bytes(b"not a pdf")
        with self.assertRaisesRegex(owner_delivery.DeliveryError, "attachment_invalid"):
            self.run_delivery()
        self.assertFalse(self.rpc.send_msg.called)
        self.pdf.write_bytes(b"%PDF-1.4 synthetic")
        outside = self.root / "protected.txt"
        outside.write_text("untouched")
        (self.root / owner_delivery.LEDGER_NAME).symlink_to(outside)
        with self.assertRaisesRegex(owner_delivery.DeliveryError, "invalid_ledger"):
            self.run_delivery()
        self.assertEqual(outside.read_text(), "untouched")
        self.assertFalse(self.rpc.send_msg.called)

    def test_git_archive_contains_tracked_source_and_its_hashes_not_state(self):
        if not (ROOT / ".git").exists():
            self.skipTest("git checkout required")
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        result = self.root / "real-source.zip"
        result_sha = owner_delivery.source_zip(ROOT, revision, result)
        self.assertEqual(result_sha, hashlib.sha256(result.read_bytes()).hexdigest())
        with zipfile.ZipFile(result) as archive:
            names = archive.namelist()
            self.assertIn("Michiflin-AI/bot.py", names)
            self.assertIn("Michiflin-AI/michiflin/queue.py", names)
            self.assertNotIn("Michiflin-AI/.git/config", names)
            self.assertFalse(any("docs/media/" in name for name in names))
            self.assertFalse(any(".michiflin-state" in name or ".enc" in name for name in names))
            rows = archive.read("Michiflin-AI/SHA256SUMS.txt").decode().splitlines()
            for row in rows:
                digest, name = row.split("  ", 1)
                self.assertEqual(hashlib.sha256(archive.read("Michiflin-AI/" + name)).hexdigest(), digest)
        self.assertLess(result.stat().st_size, owner_delivery.MAX_ATTACHMENT)


if __name__ == "__main__":
    unittest.main()
