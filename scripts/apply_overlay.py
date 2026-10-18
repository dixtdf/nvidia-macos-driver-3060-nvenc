#!/usr/bin/env python3
"""Check, explicitly apply, or restore a matching experimental overlay on macOS."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

TARGET = Path("/Library/GPUBundles/NVMTLDriver.bundle")
PREFIX = "Library/GPUBundles/NVMTLDriver.bundle"
EXPECTED_FILES = {"Contents/MacOS/NVMTLDriver", "Contents/MacOS/libnvenc_h264.dylib",
                  "Contents/_CodeSignature/CodeResources"}


def sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def validate_hashes(value: dict) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise ValueError("Missing file hashes")
    folded = set()
    for name, digest in value.items():
        if not isinstance(name, str) or "\\" in name or ":" in name or name.startswith("/"):
            raise ValueError("Invalid manifest path")
        if any(part in ("", ".", "..") or part.endswith((" ", ".")) for part in name.split("/")):
            raise ValueError("Unsafe manifest path")
        if name.casefold() in folded or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Duplicate path or invalid SHA-256")
        folded.add(name.casefold())
    return value


def verify_tree(directory: Path, files: dict[str, str]) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Bundle is not a regular directory")
    observed = set()
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ValueError("Bundle contains a symlink")
        if path.is_file():
            name = path.relative_to(directory).as_posix()
            observed.add(name)
            if name not in files or sha256(path) != files[name]:
                raise ValueError(f"Bundle file is not the expected version: {name}")
        elif not path.is_dir():
            raise ValueError("Bundle contains a special file")
    if observed != set(files):
        raise ValueError("Bundle is missing expected files")


def manifest_at(overlay: Path) -> dict:
    if overlay.is_symlink() or not overlay.is_dir():
        raise ValueError("Overlay must be an extracted regular directory")
    path = overlay / "manifest.json"
    if path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise ValueError("Invalid overlay manifest")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or manifest.get("status") != "experimental-not-hardware-qualified":
        raise ValueError("Unexpected overlay schema or qualification claim")
    if manifest.get("hardware_validation") != "not-run" or manifest.get("target_pci") != {"vendor_id": "10de", "device_id": "2504"}:
        raise ValueError("Unexpected hardware scope")
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", manifest.get("upstream_tag", "")):
        raise ValueError("Invalid upstream tag")
    base = validate_hashes(manifest["base_bundle_files"])
    changed = validate_hashes(manifest["overlay_files"])
    if set(changed) != EXPECTED_FILES or not EXPECTED_FILES <= set(base):
        raise ValueError("Overlay must change only the two reviewed binaries and bundle signature")
    for name, expected in changed.items():
        path = overlay / PREFIX / name
        if path.is_symlink() or not path.is_file() or sha256(path) != expected:
            raise ValueError(f"Overlay hash mismatch: {name}")
    return manifest


def hardware_check(tag: str) -> None:
    if sys.platform != "darwin" or platform.machine() != "x86_64":
        raise ValueError("Application requires Intel macOS")
    inventory = subprocess.run(["/usr/sbin/ioreg", "-a", "-r", "-c", "IOPCIDevice"], check=True, capture_output=True).stdout
    devices = []
    for item in plistlib.loads(inventory):
        vendor, device, code = (item.get(key) for key in ("vendor-id", "device-id", "class-code"))
        if all(isinstance(value, bytes) for value in (vendor, device, code)):
            if int.from_bytes(vendor, "little") == 0x10DE and int.from_bytes(code, "little") >> 16 == 3:
                devices.append(int.from_bytes(device, "little"))
    if devices != [0x2504]:
        raise ValueError("Requires exactly one NVIDIA display GPU, PCI 10de:2504")
    loaded = subprocess.run(["/usr/bin/kmutil", "showloaded", "--list-only"], check=True, capture_output=True, text=True).stdout
    if not re.search(r"com\.nullmoth\.NVRM\s+\(" + re.escape(tag[1:]) + r"\)", loaded):
        raise ValueError("The matching official NVRM kernel driver must already be installed and loaded; reboot after its official update")


def signature_check(bundle: Path) -> None:
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(bundle)], check=True)


def backup_path(path: Path, target: Path | None = None) -> Path:
    if target is None:
        target = TARGET
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("Backup must be a new absolute directory, not a symlink")
    canonical = path.resolve(strict=False)
    live = target.resolve(strict=True)
    if canonical == live or live in canonical.parents:
        raise ValueError("Backup must be outside the live bundle, including resolved aliases")
    if canonical.exists():
        raise ValueError("Existing backups are never overwritten")
    return canonical


def swap_directories(first: Path, second: Path) -> None:
    """Darwin RENAME_SWAP, so an incomplete directory never becomes the live bundle."""
    library = ctypes.CDLL(None, use_errno=True)
    function = library.renameatx_np
    function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    function.restype = ctypes.c_int
    if function(-2, os.fsencode(first), -2, os.fsencode(second), 2) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def apply(overlay: Path, backup: Path) -> None:
    backup = backup_path(backup)
    manifest = manifest_at(overlay)
    hardware_check(manifest["upstream_tag"])
    verify_tree(TARGET, manifest["base_bundle_files"])
    if os.geteuid() != 0:
        raise ValueError("Applying requires root; --check is read-only")
    backup.mkdir(parents=False, mode=0o700)
    shutil.copytree(TARGET, backup / TARGET.name)
    verify_tree(backup / TARGET.name, manifest["base_bundle_files"])
    expected = dict(manifest["base_bundle_files"])
    expected.update(manifest["overlay_files"])
    metadata = {"schema_version": 1, "upstream_tag": manifest["upstream_tag"],
                "base_bundle_files": manifest["base_bundle_files"], "candidate_bundle_files": expected}
    (backup / "rollback.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    shutil.copyfile(Path(__file__), backup / "apply_overlay.py")
    stage = TARGET.parent / (".nvenc3060-stage-" + uuid.uuid4().hex)
    if stage.exists():
        raise ValueError("Unexpected staging collision")
    swapped = False
    cleanup = True
    try:
        shutil.copytree(TARGET, stage)
        for name in EXPECTED_FILES:
            shutil.copyfile(overlay / PREFIX / name, stage / name)
        verify_tree(stage, expected)
        signature_check(stage)
        swap_directories(stage, TARGET)
        swapped = True
        cleanup = False
        verify_tree(TARGET, expected)
        signature_check(TARGET)
        cleanup = True
    except Exception:
        if swapped:
            try:
                swap_directories(stage, TARGET)
                cleanup = True
            except Exception as error:
                raise RuntimeError(f"Automatic recovery failed. Preserved original stage: {stage}; verified backup: {backup}") from error
        raise
    finally:
        if cleanup and stage.parent == TARGET.parent and stage.name.startswith(".nvenc3060-stage-") and stage.exists():
            shutil.rmtree(stage)
    print(f"Applied experimental H.264 overlay. Backup: {backup}")
    print("Restart macOS before the hardware probe. No reboot or service restart was performed by this tool.")


def restore(backup: Path) -> None:
    if sys.platform != "darwin" or os.geteuid() != 0:
        raise ValueError("Restoring requires root on macOS")
    if backup.is_symlink() or not backup.is_dir() or backup.stat().st_uid != 0 or backup.stat().st_mode & 0o022:
        raise ValueError("Backup must be a root-owned directory that other users cannot write")
    metadata_path = backup / "rollback.json"
    if metadata_path.is_symlink() or metadata_path.stat().st_size > 1024 * 1024:
        raise ValueError("Invalid rollback metadata")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("schema_version") != 1:
        raise ValueError("Invalid rollback schema")
    original = validate_hashes(metadata["base_bundle_files"])
    candidate = validate_hashes(metadata["candidate_bundle_files"])
    verify_tree(backup / TARGET.name, original)
    verify_tree(TARGET, candidate)
    stage = TARGET.parent / (".nvenc3060-stage-" + uuid.uuid4().hex)
    swapped = False
    cleanup = True
    try:
        shutil.copytree(backup / TARGET.name, stage)
        signature_check(stage)
        swap_directories(stage, TARGET)
        swapped = True
        cleanup = False
        verify_tree(TARGET, original)
        cleanup = True
    except Exception:
        if swapped:
            try:
                swap_directories(stage, TARGET)
                cleanup = True
            except Exception as error:
                raise RuntimeError(f"Automatic recovery failed. Preserved previous stage: {stage}; original backup: {backup}") from error
        raise
    finally:
        if cleanup and stage.parent == TARGET.parent and stage.name.startswith(".nvenc3060-stage-") and stage.exists():
            shutil.rmtree(stage)
    print("Original official NVMTL bundle restored. Restart macOS before using it.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--overlay", type=Path, default=Path(__file__).resolve().parent)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Read-only preflight (the default)")
    mode.add_argument("--apply", action="store_true", help="Explicit experimental application; requires a new backup path")
    mode.add_argument("--restore", type=Path, metavar="BACKUP", help="Restore only the exact previously applied bundle")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    try:
        if args.restore:
            restore(args.restore.absolute())
        elif args.apply:
            if args.backup is None:
                raise ValueError("--apply requires --backup with a new, absolute path")
            apply(args.overlay.absolute(), args.backup)
        else:
            manifest = manifest_at(args.overlay.absolute())
            hardware_check(manifest["upstream_tag"])
            verify_tree(TARGET, manifest["base_bundle_files"])
            print("Read-only preflight passed; no files changed. Hardware validation is still not-run.")
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Overlay operation stopped: {error}\n")
