"""Selective UnRAR tests with synthetic archives; no live account or user data."""
import io
import os
import socket
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from michiflin import config, filekit, filekit_rar_native as rar, state

FIXTURES = Path(__file__).parent / "fixtures" / "filekit"
REQUESTED = os.environ.get("MICHIFLIN_SANDBOX_TESTS") == "1"
ENABLED = REQUESTED and rar.available()


class NativeToolchainTests(unittest.TestCase):
    @unittest.skipUnless(REQUESTED, "set MICHIFLIN_SANDBOX_TESTS=1 to require the isolated toolchain")
    def test_requested_native_toolchain_is_present(self):
        self.assertTrue(rar.available(), "RAR sandbox tests requested without a compiled native reader")

    def test_parent_rejects_untrusted_listing_paths(self):
        for path in ("../escape", "sub/../../escape", "/absolute", "C:/root", "normal\\escape", "folder//file"):
            with self.subTest(path=path), self.assertRaises(filekit.FileKitError):
                rar._validated_listing({"entries": [{"path": path, "type": "file", "size": 1}]})
        with self.assertRaisesRegex(filekit.FileKitError, "duplicate_path"):
            rar._validated_listing({"entries": [{"path": "same", "type": "file", "size": 1},
                                               {"path": "same", "type": "file", "size": 1}]})


@unittest.skipUnless(ENABLED, "set MICHIFLIN_SANDBOX_TESTS=1 with the native UnRAR sandbox")
class NativeRarTests(unittest.TestCase):
    def setUp(self):
        # The repo is on the runner's real disk; /tmp can be a small tmpfs.
        self.temp = tempfile.TemporaryDirectory(prefix="rar-test-", dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        threshold = mock.patch.object(config, "MIN_FREE_DISK_BYTES", 0)
        threshold.start()
        self.addCleanup(threshold.stop)
        self.space = filekit.Workspace("owner@example.invalid", Path(self.temp.name))

    def add_fixture(self, fixture, alias=None):
        alias = alias or fixture
        with (FIXTURES / fixture).open("rb") as source:
            self.space.put("", [{"path": alias, "source": source}])
        return "/" + alias

    def listed(self, first, password="", paths=None):
        return rar.inspect(self.space, paths or [first], first, password)

    def extraction(self, first, listing, members, *, password="", paths=None, output="seleccion"):
        return rar.extract(self.space, paths or [first], first, password, members, listing["version"], folder="", name=output)

    def test_rar4_and_rar5_header_data_and_solid_encryption(self):
        for version in (4, 5):
            for variant in ("encrypted", "encrypted_filenames", "solid_encrypted", "solid_encrypted_filenames"):
                with self.subTest(version=version, variant=variant):
                    first = self.add_fixture(f"test_read_format_rar{version}_{variant}.rar")
                    listing = self.listed(first, "password")
                    self.assertEqual({item["path"] for item in listing["entries"]}, {"a.txt", "b.txt", "c.txt", "d.txt"})
                    self.assertEqual(listing["solid"], variant.startswith("solid"))
                    # Non-solid files can each use a different password; b.txt
                    # uses 'password'. For solids, d.txt exercises skip/seek.
                    target = "d.txt" if variant.startswith("solid") else "b.txt"
                    out = f"salida-{version}-{variant}"
                    self.extraction(first, listing, [target], password="password", output=out)
                    self.assertEqual(self.space.content(f"/{out}/{target}")[1].read_bytes(), f"This is from {target}".encode())
                    self.assertFalse(any(e["path"] == f"/{out}/a.txt" for e in self.space.view()["entries"]))

    def test_volume_names_with_spaces_and_unicode_are_not_lost(self):
        first = self.add_fixture("test_read_format_rar_encryption_data.rar", "  ñ archivo .rar")
        listing = self.listed(first, "12345678")
        self.extraction(first, listing, ["foo.txt"], password="12345678")
        self.assertEqual(self.space.content("/seleccion/foo.txt")[1].read_bytes(), b"data of foo.txt\n")

    def test_incorrect_password_collision_source_change_quota_and_undo(self):
        first = self.add_fixture("test_read_format_rar_encryption_data.rar", "ejemplo.rar")
        listing = self.listed(first, "12345678")
        before = self.space.view()
        with self.assertRaisesRegex(filekit.FileKitError, "wrong_password_or_corrupt"):
            self.extraction(first, listing, ["foo.txt"], password="incorrecta")
        self.assertEqual(self.space.view(), before)
        with mock.patch.object(config, "MAX_FILE_BYTES", 8):
            with self.assertRaisesRegex(filekit.FileKitError, "output_limit"):
                self.extraction(first, listing, ["foo.txt"], password="12345678")
        self.assertEqual(self.space.view(), before)
        self.extraction(first, listing, ["foo.txt"], password="12345678")
        self.assertEqual(self.space.content("/seleccion/foo.txt")[1].read_bytes(), b"data of foo.txt\n")
        with self.assertRaisesRegex(filekit.FileKitError, "exists"):
            self.extraction(first, listing, ["bar.txt"], password="12345678")
        self.assertTrue(self.space.travel(True))
        self.assertNotIn("/seleccion", [item["path"] for item in self.space.view()["entries"]])
        self.assertTrue(self.space.travel(False))
        self.space.write(first, io.BytesIO(b"archivo cambiado"))
        with self.assertRaisesRegex(filekit.FileKitError, "source_changed"):
            self.extraction(first, listing, ["foo.txt"], password="12345678", output="obsoleto")
        self.assertNotIn("/obsoleto", [item["path"] for item in self.space.view()["entries"]])

    def test_solid_multivolume_and_missing_volume(self):
        parts = [self.add_fixture(f"test_read_format_rar5_multiarchive_solid.part{i:02d}.rar") for i in range(1, 5)]
        listing = self.listed(parts[0], paths=parts)
        self.assertTrue(listing["multiVolume"])
        self.assertTrue(listing["solid"])
        chosen = [item["path"] for item in listing["entries"] if item["type"] == "file"][-1]
        self.extraction(parts[0], listing, [chosen], paths=parts)
        self.assertGreater(self.space.content("/seleccion/" + chosen)[0]["size"], 0)
        before = self.space.view()
        with self.assertRaises(filekit.FileKitError):
            partial = self.listed(parts[0], paths=parts[:3])
            self.extraction(parts[0], partial, [chosen], paths=parts[:3], output="incompleto")
        self.assertEqual(self.space.view(), before)

    def test_adversarial_paths_and_links_fail_closed(self):
        for fixture in ("test_read_format_rar5_symlink.rar", "test_read_format_rar5_hardlink.rar", "test_read_format_rar5_unicode.rar"):
            with self.subTest(fixture=fixture):
                first = self.add_fixture(fixture)
                before = self.space.view()
                with self.assertRaisesRegex(filekit.FileKitError, "unsafe_link"):
                    self.listed(first)
                self.assertEqual(self.space.view(), before)
        # Modify a benign RAR4 entry name in memory, recomputing its header CRC.
        data = bytearray((FIXTURES / "test_read_format_rar_encryption_data.rar").read_bytes())
        offset = data.index(b"foo.txt")
        data[offset:offset + 7] = b"../x.tx"
        start = 20
        end = start + struct.unpack_from("<H", data, start + 5)[0]
        struct.pack_into("<H", data, start, zlib.crc32(data[start + 2:end]) & 0xFFFF)
        self.space.put("", [{"path": "travesia.rar", "source": io.BytesIO(data)}])
        before = self.space.view()
        with self.assertRaisesRegex(filekit.FileKitError, "unsafe_path"):
            self.listed("/travesia.rar", "12345678")
        self.assertEqual(self.space.view(), before)

    def test_unavailable_isolation_and_maintenance_never_commit(self):
        first = self.add_fixture("test_read_format_rar_encryption_data.rar", "ejemplo.rar")
        before = self.space.view()
        with mock.patch.object(rar.filekit_archive, "available", return_value=False):
            with self.assertRaisesRegex(filekit.FileKitError, "engine_unavailable"):
                self.listed(first, "12345678")
        self.assertEqual(self.space.view(), before)
        listing = self.listed(first, "12345678")
        with mock.patch.object(state, "check_work_allowed", side_effect=InterruptedError("pause")):
            with self.assertRaises(InterruptedError):
                self.extraction(first, listing, ["foo.txt"], password="12345678")
        self.assertEqual(self.space.view(), before)

    def test_isolated_worker_no_network_host_environment_or_writable_input(self):
        first = self.add_fixture("test_read_format_rar_encryption_data.rar", "ejemplo.rar")
        synthetic_secret = "synthetic-password-never-in-process-args"
        marker = Path(self.temp.name) / "host-only-marker"
        marker.write_text("not visible", encoding="utf-8")
        fake = Path(self.temp.name) / "fake-worker.py"
        fake.write_text('''import json, os, socket, sys\np=json.load(sys.stdin)\nresult={"nonroot":os.getuid()!=0,"nohost":not os.path.exists(p["hostpath"]),"noenv":"MICHIFLIN_SYNTHETIC_HOST_SECRET" not in os.environ,"read":open("/input/ejemplo.rar","rb").read(4).hex()}\ntry:\n open("/input/ejemplo.rar","wb").write(b"oops");result["readonly"]=False\nexcept OSError:result["readonly"]=True\ns=socket.socket();s.settimeout(0.4)\ntry:s.connect(("127.0.0.1",p["port"]));result["nohostnet"]=False\nexcept OSError:result["nohostnet"]=True\nprint(json.dumps({"ok":True,"result":result}))\n''', encoding="utf-8")
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0)); server.listen(1)
            sources, _ = rar._sources(self.space, [first], first)
            real_popen = rar.subprocess.Popen
            calls = []
            def remember(*args, **kwargs):
                calls.append((args, kwargs))
                return real_popen(*args, **kwargs)
            with mock.patch.object(rar, "_WORKER", fake), mock.patch.object(rar.subprocess, "Popen", side_effect=remember), \
                 mock.patch.dict(os.environ, {"MICHIFLIN_SYNTHETIC_HOST_SECRET": "must-not-appear"}):
                with self.space.lock, rar._jail(self.space.root) as base:
                    result = rar._run(base, sources, {"password": synthetic_secret, "hostpath": str(marker),
                                      "port": server.getsockname()[1]}, 8 * 1024 * 1024)
            self.assertTrue(all(result[key] for key in ("nonroot", "nohost", "noenv", "readonly", "nohostnet")))
            self.assertEqual(result["read"], "52617221")
            for args, kwargs in calls:
                self.assertNotIn(synthetic_secret, repr(args))
                self.assertNotIn(synthetic_secret, repr(kwargs.get("env", {})))
                self.assertNotIn("ejemplo.rar", repr(args))
        self.assertEqual(self.space.content(first)[1].read_bytes(), (FIXTURES / "test_read_format_rar_encryption_data.rar").read_bytes())

    @unittest.skipUnless(os.environ.get("MICHIFLIN_LARGE_RAR_TEST") == "1", "optional 600 MiB streaming test")
    def test_streaming_600_mib_extraction(self):
        first = self.add_fixture("test_synthetic_large_streaming.rar", "grande.rar")
        listing = self.listed(first)
        sizes = {item["path"]: item["size"] for item in listing["entries"]}
        self.assertEqual(sizes["grande.bin"], 600 * 1024 * 1024)
        self.extraction(first, listing, ["grande.bin"])
        metadata, location = self.space.content("/seleccion/grande.bin")
        self.assertEqual(metadata["size"], 600 * 1024 * 1024)
        with location.open("rb") as stream:
            self.assertEqual(stream.read(32), bytes(32))
            stream.seek(-32, os.SEEK_END)
            self.assertEqual(stream.read(), bytes(32))
        self.assertNotIn("/seleccion/pequenio.txt", [item["path"] for item in self.space.view()["entries"]])
