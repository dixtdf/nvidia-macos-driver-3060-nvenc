#!/usr/bin/env python3
"""Build an experimental overlay from one pinned official package; never install it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
BUNDLE = "pkgroot/Library/GPUBundles/NVMTLDriver.bundle/"
OVERLAY_BUNDLE = "Library/GPUBundles/NVMTLDriver.bundle/"
CHANGED = ("Contents/MacOS/NVMTLDriver", "Contents/MacOS/libnvenc_h264.dylib",
           "Contents/_CodeSignature/CodeResources")


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def read_state(path: Path) -> dict:
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("schema_version") != 1 or state.get("upstream_repository") != "nullmoth/nvidia-macos-driver":
        raise ValueError("Unknown upstream state schema or repository")
    release, asset = state["release"], state["asset"]
    tag = release["tag"]
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag) or not re.fullmatch(r"[0-9a-f]{40}", release["commit"]):
        raise ValueError("Unpinned upstream tag or commit")
    expected = f"nullmoth-nvidia-{tag[1:]}.tar.gz"
    expected_url = f"https://github.com/nullmoth/nvidia-macos-driver/releases/download/{tag}/{expected}"
    if asset["name"] != expected or asset["url"] != expected_url:
        raise ValueError("Asset must be the exact official release package")
    if not re.fullmatch(r"[0-9a-f]{64}", asset["sha256"]) or not 0 < asset["size"] < 2 * 1024**3:
        raise ValueError("Invalid official asset SHA-256 or size")
    return state


def download(state: dict, cache: Path) -> Path:
    asset = state["asset"]
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / asset["name"]
    if destination.is_symlink():
        raise ValueError("Refusing symlinked package cache")
    if not destination.exists():
        request = urllib.request.Request(asset["url"], headers={"User-Agent": "nvenc3060-actions"})
        descriptor, temporary_name = tempfile.mkstemp(prefix=asset["name"] + ".", suffix=".part", dir=cache)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output, urllib.request.urlopen(request, timeout=120) as response:
                count = 0
                while chunk := response.read(1024 * 1024):
                    count += len(chunk)
                    if count > asset["size"]:
                        raise ValueError("Downloaded asset exceeds pinned size")
                    output.write(chunk)
            if temporary.stat().st_size != asset["size"] or digest(temporary) != asset["sha256"]:
                raise ValueError("Official download SHA-256 or size mismatch")
            temporary.replace(destination)
        finally:
            if temporary.exists():
                temporary.unlink()
    if not destination.is_file() or destination.stat().st_size != asset["size"] or digest(destination) != asset["sha256"]:
        raise ValueError("Cached package does not match pinned official release")
    return destination


def extract_bundle(package: Path, destination: Path) -> dict[str, str]:
    """Extract regular bundle files explicitly, rejecting duplicate/alternate paths."""
    seen: set[str] = set()
    base_files: dict[str, str] = {}
    total = 0
    with tarfile.open(package, "r:gz") as archive:
        for member in archive:
            if not member.name.startswith(BUNDLE):
                continue
            name = member.name[len(BUNDLE):]
            if member.isdir() and (not name or name.endswith("/")):
                continue
            if not name and member.isdir():
                continue
            parts = name.rstrip("/").split("/")
            if any(part in ("", ".", "..") or part.endswith((" ", ".")) for part in parts) or "\\" in name or ":" in name:
                raise ValueError("Unsafe bundle member path")
            if member.isdir():
                continue
            if not member.isfile() or member.issym() or member.islnk() or name.casefold() in seen:
                raise ValueError("Non-regular or duplicate bundle member")
            seen.add(name.casefold())
            total += member.size
            if not 0 < member.size < 256 * 1024**2 or total > 512 * 1024**2:
                raise ValueError("Unexpected bundle member size")
            target = destination.joinpath(*PurePosixPath(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("Unreadable bundle member")
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
            base_files[name] = digest(target)
    if not set(CHANGED).issubset(base_files) or "Contents/Info.plist" not in base_files:
        raise ValueError("Official package lacks required NVMTL bundle files")
    return base_files


def build(state_path: Path, output: Path, cache: Path, inspect_only: bool = False) -> dict:
    state = read_state(state_path)
    tag = state["release"]["tag"]
    profile_path = ROOT / "profiles" / f"{tag}.json"
    if not profile_path.is_file():
        raise ValueError(f"No reviewed adaptation profile for {tag}; stop for binary review")
    if not inspect_only and (platform.system() != "Darwin" or platform.machine() != "x86_64"):
        raise ValueError("Signed candidate builds require an Intel macOS runner; use --inspect for static analysis")
    if output.exists() and (output.is_symlink() or any(output.iterdir())):
        raise ValueError("Candidate output must be an empty regular directory")
    from nvenc3060.adapter import adapt_bundle, load_profile
    profile = load_profile(profile_path)
    if profile.get("upstream_tag") != tag:
        raise ValueError("Adaptation profile does not match the pinned release tag")
    package = download(state, cache)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nvenc3060-build-") as temporary:
        stage = Path(temporary)
        bundle = stage / "NVMTLDriver.bundle"
        base_files = extract_bundle(package, bundle)
        patched = stage / "patched"
        audit = adapt_bundle(profile, bundle / "Contents/MacOS", patched)
        report = {
            "schema_version": 1, "status": "static-inspection-only" if inspect_only else "experimental-built",
            "hardware_validation": "not-run", "upstream": state,
            "profile_sha256": digest(profile_path), "patch_audit": audit,
            "target_pci": {"vendor_id": "10de", "device_id": "2504"},
        }
        if inspect_only:
            (output / "build-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            return report
        for name in ("NVMTLDriver", "libnvenc_h264.dylib"):
            destination = bundle / "Contents/MacOS" / name
            shutil.copyfile(patched / name, destination)
            destination.chmod(0o755)
        subprocess.run(["codesign", "--force", "--sign", "-", str(bundle / CHANGED[1])], check=True)
        subprocess.run(["codesign", "--force", "--sign", "-", str(bundle)], check=True)
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(bundle)], check=True)
        overlay = stage / "overlay"
        output_files = {}
        for name in CHANGED:
            destination = overlay / OVERLAY_BUNDLE / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(bundle / name, destination)
            destination.chmod(0o755 if name in CHANGED[:2] else 0o644)
            output_files[name] = digest(destination)
        manifest = {
            "schema_version": 1, "status": "experimental-not-hardware-qualified",
            "upstream_tag": tag, "upstream_commit": state["release"]["commit"],
            "upstream_asset_sha256": state["asset"]["sha256"],
            "target_pci": report["target_pci"], "signing": "ad-hoc",
            "base_bundle_files": base_files, "overlay_files": output_files,
            "hardware_validation": "not-run",
            "scope": "H.264 registration and classic input layout only; HEVC is unchanged",
        }
        (overlay / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        for name in ("LICENSE", "NOTICE"):
            shutil.copyfile(ROOT / name, overlay / name)
        shutil.copytree(ROOT / "LICENSES", overlay / "LICENSES")
        shutil.copyfile(ROOT / "scripts/apply_overlay.py", overlay / "apply_overlay.py")
        archive_path = output / f"nvenc3060-overlay-{tag}-experimental.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            for path in sorted(overlay.rglob("*")):
                if path.is_file():
                    archive.add(path, arcname=path.relative_to(overlay).as_posix(), recursive=False)
        report.update({"signing": "ad-hoc", "signed_files": output_files,
                       "artifact": {"name": archive_path.name, "sha256": digest(archive_path)}})
        (output / "build-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        (output / "SHA256SUMS.txt").write_text(f"{digest(archive_path)}  {archive_path.name}\n", encoding="ascii")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=ROOT / "state/upstream.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, default=ROOT / "build/cache")
    parser.add_argument("--inspect", action="store_true", help="Static-only inspection; no installable archive")
    args = parser.parse_args()
    try:
        result = build(args.state, args.output, args.cache, args.inspect)
        print(f"{result['status']}; hardware_validation=not-run")
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Candidate build stopped: {error}\n")
