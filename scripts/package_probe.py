#!/usr/bin/env python3
"""Package the Actions-built Intel macOS acceptance probe for on-device testing."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def package(binary: Path, output: Path) -> Path:
    if binary.is_symlink() or not binary.is_file():
        raise ValueError("Probe must be a regular compiled file")
    header = binary.read_bytes()[:32]
    if len(header) != 32 or struct.unpack_from("<I", header)[0] != 0xFEEDFACF or struct.unpack_from("<I", header, 4)[0] != 0x01000007 or struct.unpack_from("<I", header, 12)[0] != 2:
        raise ValueError("Probe must be a thin Intel Mach-O executable")
    state = json.loads((ROOT / "state/upstream.json").read_text(encoding="utf-8"))
    tag = state["release"]["tag"]
    output.mkdir(parents=True, exist_ok=True)
    archive_path = output / f"nvenc3060-probe-{tag}.tar.gz"
    checksums = output / "SHA256SUMS.txt"
    if archive_path.exists():
        raise ValueError("Probe archive already exists")
    if checksums.is_symlink() or not checksums.is_file():
        raise ValueError("Build the candidate checksum manifest before packaging the probe")
    with tempfile.TemporaryDirectory(prefix="nvenc3060-probe-") as temporary:
        stage = Path(temporary)
        target = stage / "nvenc-acceptance"
        shutil.copyfile(binary, target)
        target.chmod(0o755)
        subprocess.run(["codesign", "--force", "--sign", "-", str(target)], check=True)
        subprocess.run(["codesign", "--verify", "--strict", str(target)], check=True)
        for source, destination in ((ROOT / "scripts/validate_hardware_report.py", "validate_hardware_report.py"),
                                    (ROOT / "docs/hardware-acceptance.md", "HARDWARE-ACCEPTANCE.md"),
                                    (ROOT / "LICENSE", "LICENSE"), (ROOT / "NOTICE", "NOTICE")):
            shutil.copyfile(source, stage / destination)
        report = {"schema_version": 1, "upstream_tag": tag, "signing": "ad-hoc",
                  "probe_sha256": digest(target), "input_probe_sha256": digest(binary),
                  "hardware_validation": "not-run"}
        (stage / "probe-build.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        with tarfile.open(archive_path, "w:gz") as archive:
            for path in sorted(stage.iterdir()):
                archive.add(path, arcname=path.name, recursive=False)
    with checksums.open("a", encoding="ascii") as manifest:
        manifest.write(f"{digest(archive_path)}  {archive_path.name}\n")
    print(f"Packaged {archive_path.name}; no hardware qualification claimed")
    return archive_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    package(args.binary, args.output)
