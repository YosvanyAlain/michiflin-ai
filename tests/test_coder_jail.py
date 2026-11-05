"""Filesystem isolation checks with synthetic public data, never real user files."""
from pathlib import Path
import shlex
import tempfile

import pytest

from michiflin import coder, coder_jail, config


def test_setup_failure_never_falls_back_to_host(monkeypatch):
    monkeypatch.setattr(coder, "_isolation", None)
    monkeypatch.setattr(config, "CODE_RUN", True)
    monkeypatch.setattr(coder.shutil, "which", lambda _name, path=None: "/usr/bin/tool")
    calls = []
    def failed_jail(work, command, **_kwargs):
        calls.append((work, command))
        return 1, "synthetic setup failure"
    monkeypatch.setattr(coder_jail, "run", failed_jail)
    assert coder.isolation() is False
    assert len(calls) == 1  # no unisolated fallback and no automatic command retry


def test_runtime_roots_never_mount_all_opt_or_home():
    roots = coder_jail.runtime_roots()
    assert all(root.startswith("/opt/") for root in roots)
    assert "/opt" not in roots and "/opt/hostedtoolcache" not in roots
    assert all(not root.startswith(str(Path.home()) + "/") for root in roots)


def test_real_jail_hides_public_host_file_and_uses_private_tmp(monkeypatch):
    if not coder.isolation():
        pytest.skip("requires sudo, mount namespaces, chroot and toolchains")
    with tempfile.TemporaryDirectory(prefix="michi-code-boundary-") as directory:
        root = Path(directory)
        root.chmod(0o755)
        project = root / "project"
        project.mkdir(mode=0o777)
        project.chmod(0o777)
        outside = root / "public-fixture.txt"
        outside.write_text("ONLY-SYNTHETIC", encoding="utf-8")
        outside.chmod(0o644)
        host_tmp = Path("/tmp") / (root.name + "-temp-marker")
        assert not host_tmp.exists()
        (project / "test.py").write_text(
            "import os\nfrom pathlib import Path\n"
            f"outside = Path({str(outside)!r})\n"
            f"marker = Path({str(host_tmp)!r})\n"
            "print('uid', os.getuid())\n"
            "print('outside', outside.exists())\n"
            "print('secret', 'MICHIFLIN_SYNTHETIC_SECRET' in os.environ)\n"
            "try:\n os.readlink('/proc/1/root'); print('host-root', 'exposed')\n"
            "except (PermissionError, FileNotFoundError):\n print('host-root', 'blocked')\n"
            "marker.write_text('only in private tmp')\n"
            "mounts = Path('/proc/self/mountinfo').read_text().splitlines()\n"
            "print('private-tmp', any(line.split()[4] == '/tmp' and ' - tmpfs ' in line for line in mounts))\n",
            encoding="utf-8",
        )
        (project / "test.py").chmod(0o644)
        monkeypatch.setenv("MICHIFLIN_SYNTHETIC_SECRET", "not-a-real-secret")
        code, output = coder._run(project, "python3 test.py")
        assert code == 0, output
        assert "uid 65534" in output
        assert "outside False" in output
        assert "secret False" in output
        assert "host-root blocked" in output
        assert "private-tmp True" in output
        assert not host_tmp.exists()


def test_jail_can_use_a_project_under_a_private_host_parent():
    if not coder.isolation():
        pytest.skip("requires sudo, mount namespaces, chroot and toolchains")
    with tempfile.TemporaryDirectory(prefix="michi-private-project-") as directory:
        root = Path(directory)
        root.chmod(0o700)
        project = root / "project"
        project.mkdir(mode=0o777)
        project.chmod(0o777)
        code, output = coder._run(project, "printf sandbox-ok > result.txt")
        assert code == 0, output
        assert (project / "result.txt").read_text() == "sandbox-ok"
        assert (root.stat().st_mode & 0o777) == 0o700


def test_timeout_removes_the_jail():
    if not coder.isolation():
        pytest.skip("requires sudo, mount namespaces, chroot and toolchains")
    with tempfile.TemporaryDirectory(prefix="michi-code-timeout-") as directory:
        project = Path(directory)
        project.chmod(0o777)
        parent = Path(tempfile.gettempdir())
        before = set(parent.glob("michi-coder-jail-*"))
        code, _output = coder_jail.run(project, "sleep 10", timeout=1, tool_path=coder._tool_path())
        assert code == 124
        assert set(parent.glob("michi-coder-jail-*")) == before


def test_large_command_output_is_bounded():
    if not coder.isolation():
        pytest.skip("requires sudo, mount namespaces, chroot and toolchains")
    with tempfile.TemporaryDirectory(prefix="michi-code-output-") as directory:
        project = Path(directory)
        project.chmod(0o777)
        code, output = coder._run(project, "python3 -c \"print('x' * 1000000)\"")
        assert code == 0
        assert len(output) <= coder.OUTPUT_MAX + 10
        assert "[…]" in output


def test_real_jail_can_write_the_project_but_not_an_outside_path():
    if not coder.isolation():
        pytest.skip("requires sudo, mount namespaces, chroot and toolchains")
    with tempfile.TemporaryDirectory(prefix="michi-code-work-") as directory:
        root = Path(directory)
        root.chmod(0o755)
        project = root / "project"
        project.mkdir(mode=0o777)
        project.chmod(0o777)
        outside = root / "outside.txt"
        outside.write_text("original fixture", encoding="utf-8")
        outside.chmod(0o666)
        command = "echo project-result > result.txt; echo changed > " + shlex.quote(str(outside))
        code, _output = coder._run(project, command)
        assert code != 0
        assert (project / "result.txt").read_text().strip() == "project-result"
        assert outside.read_text() == "original fixture"
