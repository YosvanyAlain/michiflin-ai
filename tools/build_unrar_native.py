#!/usr/bin/env python3
"""Build the pinned, separately licensed UnRAR library for the isolated worker.

Only unpacking is supported. See vendor/unrar/LICENSE and NOTICE.txt. UnRAR
source code may be used in any software to handle RAR archives without
limitations free of charge, but cannot be used to develop RAR (WinRAR)
compatible archiver and to re-create RAR compression algorithm, which is
proprietary. Distribution of modified UnRAR source code in separate form or
as a part of other software is permitted, provided that full text of this
paragraph, starting from "UnRAR source code" words, is included in license, or
in documentation if license is not available, and in source code comments of
resulting package.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "vendor/unrar/unrarsrc-7.3.2.tar.gz"
SHA256 = "f61a1160d6e2ebbda2ad353c8e82f62ab4312f97fde23c6c2c982d25e0014f99"


def build(output: Path) -> None:
    if hashlib.sha256(SOURCE.read_bytes()).hexdigest() != SHA256:
        raise ValueError("UnRAR source checksum mismatch")
    output = output.absolute()
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="unrar-build-", dir=output.parent) as directory:
        base = Path(directory)
        with tarfile.open(SOURCE, "r:gz") as archive:
            for member in archive.getmembers():
                parts = Path(member.name).parts
                if not parts or parts[0] != "unrar" or Path(member.name).is_absolute() or any(p in {".", ".."} for p in parts):
                    raise ValueError("unsafe source archive")
                if not (member.isdir() or member.isfile()):
                    raise ValueError("unsupported source entry")
                target = base.joinpath(*parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise ValueError("source entry missing")
                    with source, target.open("xb") as destination:
                        shutil.copyfileobj(source, destination, 1024 * 1024)
        env = {"PATH": os.defpath + ":/usr/local/bin", "LANG": "C.UTF-8"}
        subprocess.run(["make", "-j2", "lib"], cwd=base / "unrar", env=env, check=True, timeout=600)
        result = base / "unrar/libunrar.so"
        if not result.is_file() or result.is_symlink():
            raise RuntimeError("UnRAR build failed")
        staged = output.with_name(output.name + ".tmp")
        shutil.copyfile(result, staged)
        os.chmod(staged, 0o755)
        os.replace(staged, output)
    print("UnRAR native unpacking library built from the pinned source.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    build(parser.parse_args().output)
