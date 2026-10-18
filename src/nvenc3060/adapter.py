"""Produce an experimental copy, refusing every unreviewed binary.

This module never installs, signs, loads, or replaces a system driver. A matching
profile proves the static transformation, not hardware encoding correctness.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
from typing import Any


class AdaptationError(ValueError):
    """An input or profile does not meet the reviewed transformation contract."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _integer(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        raise AdaptationError(f"{label} must be a non-negative integer")
    return value


def _hex(value: Any, label: str) -> bytes:
    if not isinstance(value, str) or not value or not re.fullmatch(r"(?:[0-9a-f]{2})+", value):
        raise AdaptationError(f"{label} must contain lowercase byte pairs")
    return bytes.fromhex(value)


def validate_profile(profile: dict[str, Any]) -> None:
    if not isinstance(profile, dict) or type(profile.get("schema_version")) is not int or profile["schema_version"] != 1:
        raise AdaptationError("unsupported profile schema")
    if profile.get("status") != "experimental" or profile.get("hardware_qualified") is not False:
        raise AdaptationError("this adapter accepts experimental, unqualified profiles only")
    if profile.get("hardware") != {"vendor_id": "10de", "device_id": "2504", "gpu": "RTX 3060 12GB"}:
        raise AdaptationError("profile must be restricted to PCI 10de:2504")
    files = profile.get("files")
    if not isinstance(files, list) or any(not isinstance(f, dict) for f in files) or {f.get("path") for f in files} != {
        "NVMTLDriver", "libnvenc_h264.dylib"
    } or len(files) != 2:
        raise AdaptationError("profile must contain exactly the H.264 library and Metal plug-in")
    for item in files:
        for key in ("sha256_before", "sha256_after"):
            if not isinstance(item.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", item[key]):
                raise AdaptationError(f"invalid {key}")
        _integer(item.get("size"), "file size")
        if item.get("macho_filetype") not in (6, 8):
            raise AdaptationError("profile must describe MH_DYLIB or MH_BUNDLE")
        patches = item.get("patches")
        if not isinstance(patches, list) or not patches:
            raise AdaptationError("missing exact byte patches")
        intervals = []
        for patch in patches:
            if not isinstance(patch, dict):
                raise AdaptationError("patch must be an object")
            offset = _integer(patch.get("offset"), "patch offset")
            before = _hex(patch.get("before_hex"), "original bytes")
            after = _hex(patch.get("after_hex"), "replacement bytes")
            if len(before) != len(after) or before == after:
                raise AdaptationError("patches must change equal-length instruction regions")
            if offset + len(before) > item["size"]:
                raise AdaptationError("patch exceeds file size")
            intervals.append((offset, offset + len(before)))
        intervals.sort()
        if any(left[1] > right[0] for left, right in zip(intervals, intervals[1:])):
            raise AdaptationError("overlapping patches are forbidden")


def load_profile(path: str | Path) -> dict[str, Any]:
    try:
        profile = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AdaptationError(f"cannot read profile: {error}") from error
    validate_profile(profile)
    return profile


def text_section(data: bytes, expected_filetype: int) -> tuple[int, int]:
    """Parse thin x86-64 Mach-O, validating every load command and section bound."""
    if len(data) < 32:
        raise AdaptationError("truncated Mach-O header")
    magic, cpu, subtype, filetype, ncmds, sizeofcmds, _, _ = struct.unpack_from("<8I", data)
    if magic != 0xFEEDFACF or cpu != 0x01000007 or subtype & 0xFFFFFF != 3:
        raise AdaptationError("only a thin x86_64 Mach-O is supported")
    if filetype != expected_filetype:
        raise AdaptationError("unexpected Mach-O file type")
    command_end = 32 + sizeofcmds
    if command_end > len(data) or ncmds > sizeofcmds // 8:
        raise AdaptationError("invalid Mach-O load-command extent")
    offset = 32
    found = []
    for _ in range(ncmds):
        if offset + 8 > command_end:
            raise AdaptationError("truncated load command")
        command, size = struct.unpack_from("<II", data, offset)
        if size < 8 or size % 8 or offset + size > command_end:
            raise AdaptationError("invalid load command size")
        if command == 0x19:  # LC_SEGMENT_64
            if size < 72:
                raise AdaptationError("truncated segment")
            segment = struct.unpack_from("<II16s4Q4I", data, offset)
            _, _, name, vmaddr, vmsize, fileoff, filesize, maxprot, initprot, count, _ = segment
            if size != 72 + count * 80 or fileoff + filesize > len(data) or filesize > vmsize:
                raise AdaptationError("invalid segment bounds")
            for index in range(count):
                section = struct.unpack_from("<16s16sQQIIIIIIII", data, offset + 72 + index * 80)
                sectname, segname, address, length, position = section[:5]
                # Virtual-only sections do not occupy file bytes.
                section_type = section[8] & 0xFF
                if section_type not in (1, 0xC, 0x12):
                    if position < fileoff or position + length > fileoff + filesize:
                        raise AdaptationError("section lies outside its file segment")
                if sectname.rstrip(b"\0") == b"__text" and segname.rstrip(b"\0") == b"__TEXT":
                    if name.rstrip(b"\0") != b"__TEXT" or not initprot & 4 or not maxprot & 4:
                        raise AdaptationError("text segment is not executable")
                    if address - vmaddr != position - fileoff or position < command_end:
                        raise AdaptationError("text mapping does not match file offsets")
                    found.append((position, position + length))
        offset += size
    if offset != command_end or len(found) != 1:
        raise AdaptationError("expected exactly one bounded text section")
    return found[0]


def adapt_bytes(data: bytes, item: dict[str, Any]) -> bytes:
    if len(data) != item["size"] or sha256(data) != item["sha256_before"]:
        raise AdaptationError(f"{item['path']}: unreviewed input digest/size; no changes made")
    start, end = text_section(data, item["macho_filetype"])
    result = bytearray(data)
    for patch in item["patches"]:
        offset = patch["offset"]
        before = bytes.fromhex(patch["before_hex"])
        if not start <= offset < offset + len(before) <= end:
            raise AdaptationError("patch is not contained in executable __text")
        if data[offset:offset + len(before)] != before:
            raise AdaptationError("original instruction bytes do not match")
        result[offset:offset + len(before)] = bytes.fromhex(patch["after_hex"])
    patched = bytes(result)
    if sha256(patched) != item["sha256_after"]:
        raise AdaptationError("patched digest is not the reviewed result")
    return patched


def adapt_bundle(profile: dict[str, Any], input_dir: str | Path,
                 output_dir: str | Path) -> dict[str, Any]:
    validate_profile(profile)
    source = Path(input_dir).resolve(strict=True)
    destination = Path(output_dir).absolute()
    # Outputs are new directories, never an overwrite or an in-place mutation.
    if destination.exists() or destination.is_symlink():
        raise AdaptationError("output directory must not exist")
    if source == destination.resolve() or source in destination.resolve().parents:
        raise AdaptationError("output must be separate from the input tree")
    patched_files = []
    for item in profile["files"]:
        path = source / item["path"]
        if path.is_symlink() or not path.is_file():
            raise AdaptationError(f"input must be a regular file: {path.name}")
        original = path.read_bytes()
        patched_files.append((item, adapt_bytes(original, item)))
    report = {
        "schema_version": 1,
        "profile_id": profile["profile_id"],
        "status": "experimental-static-candidate",
        "hardware_qualified": False,
        "hardware": profile["hardware"],
        "scope_warning": "H.264 registration override is global inside this binary; only apply with the external single-10de:2504 hardware guard. HEVC registration is unchanged.",
        "codesign_required": True,
        "files": [{"path": item["path"], "input_sha256": item["sha256_before"],
                   "unsigned_output_sha256": sha256(patched)} for item, patched in patched_files],
    }
    destination.mkdir(parents=False, exist_ok=False)
    for item, patched in patched_files:
        path = destination / item["path"]
        with path.open("xb") as handle:
            handle.write(patched)
        path.chmod(0o755)
    with (destination / "audit.json").open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = adapt_bundle(load_profile(args.profile), args.input_dir, args.output_dir)
    except (AdaptationError, OSError) as error:
        parser.exit(2, f"refused: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
