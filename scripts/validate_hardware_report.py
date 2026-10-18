#!/usr/bin/env python3
"""Fail closed on public synthetic H.264 acceptance reports; never executes a driver."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys

ENCODER_ID = "com.nvmtl.videoencoder.avc.nvenc"
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+\Z")
ROOT_KEYS = {
    "schema_version", "probe", "profile", "pass", "hardware_required", "software_fallback_allowed",
    "encoder_requested", "encoder_selected", "hardware_used", "gpu", "video", "results", "statuses", "errors",
    "candidate_sha256", "upstream_tag", "loaded_encoder_sha256",
}
GPU_KEYS = {"detected", "matching_devices", "nvidia_display_devices", "vendor_id", "device_id"}
VIDEO_KEYS = {"codec", "input_format", "input_layout", "width", "height", "fps", "requested_frames"}
RESULT_KEYS = {
    "encoded_frames", "decoded_frames", "verified_frames", "callback_errors", "dropped_frames",
    "forced_keyframes", "forced_idr_passed", "idr_frames", "decoder_hardware_used",
    "decoder_hardware_query_success", "luma_mae_max", "chroma_mae_max", "region_mae_max",
    "thin_line_mae_max", "marker_error_max", "elapsed_seconds", "encoded_fps", "encode_ms_p50",
    "encode_ms_p95", "encode_ms_max",
}
STATUS_KEYS = {"encoder_create", "encoder_properties", "encoder_prepare", "input_buffer", "decoder_create"}


def _integer(value: object) -> bool:
    return type(value) is int


def _number(value: object) -> bool:
    # Bound integers before float conversion (malicious JSON can contain enormous integers).
    return type(value) in (int, float) and 0 <= value <= 1e12 and math.isfinite(value)


def validate_report(
    report: object, *, profile: str,
    candidate_sha256: str | None = None, encoder_sha256: str | None = None,
    upstream_tag: str | None = None,
) -> list[str]:
    """Return only fixed diagnostic strings, never untrusted report content."""
    problems: list[str] = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            problems.append(message)

    if not isinstance(report, dict) or set(report) != ROOT_KEYS:
        return ["report keys differ from the public allowlist"]
    require(profile in {"smoke", "qualification"}, "unknown validation profile")
    require(type(report["schema_version"]) is int and report["schema_version"] == 1, "unsupported schema")
    require(report["probe"] == "3060-nvenc-h264-v1", "wrong probe")
    require(report["profile"] == profile, "profile mismatch")
    require(report["pass"] is True, "probe did not pass")
    require(report["hardware_required"] is True, "hardware was not required")
    require(report["software_fallback_allowed"] is False, "software fallback was allowed")
    require(report["hardware_used"] is True, "hardware encoder was not confirmed")
    require(report["encoder_requested"] == ENCODER_ID and report["encoder_selected"] == ENCODER_ID,
            "NVENC encoder identity mismatch")
    require(report["errors"] == [], "probe errors are present")

    for name, expected in (("gpu", GPU_KEYS), ("video", VIDEO_KEYS), ("results", RESULT_KEYS)):
        if not isinstance(report[name], dict) or set(report[name]) != expected:
            problems.append(f"{name} keys differ from the public allowlist")
    if problems and any("keys differ" in item for item in problems):
        return problems
    gpu, video, results = report["gpu"], report["video"], report["results"]
    require(gpu["detected"] is True, "target GPU was not detected")
    for name, expected in (("matching_devices", 1), ("nvidia_display_devices", 1), ("vendor_id", 0x10DE), ("device_id", 0x2504)):
        require(_integer(gpu[name]) and gpu[name] == expected, "target PCI identity mismatch")
    for name, expected in (("width", 1920), ("height", 1080), ("fps", 60)):
        require(_integer(video[name]) and video[name] == expected, "video geometry/rate mismatch")
    require(video["codec"] == "h264" and video["input_format"] == "420v" and
            video["input_layout"] == "ordinary_nv12", "wrong codec or input layout")
    frames = video["requested_frames"]
    if not _integer(frames) or not 30 <= frames <= 36000:
        return problems + ["invalid requested frame count"]
    require(profile != "qualification" or frames >= 3600, "qualification needs at least 3600 frames")
    integer_fields = {"encoded_frames", "decoded_frames", "verified_frames", "callback_errors", "dropped_frames",
                      "forced_keyframes", "forced_idr_passed", "idr_frames"}
    for name in integer_fields:
        require(_integer(results[name]) and results[name] >= 0, "invalid integer result")
    numeric_fields = RESULT_KEYS - integer_fields - {"decoder_hardware_used", "decoder_hardware_query_success"}
    for name in numeric_fields:
        require(_number(results[name]), "invalid numeric result")
    if any(item.startswith("invalid ") for item in problems):
        return problems
    for name in ("encoded_frames", "decoded_frames", "verified_frames"):
        require(results[name] == frames, "incomplete frame count")
    require(results["callback_errors"] == 0 and results["dropped_frames"] == 0, "callbacks or frames failed")
    forced = (frames + 59) // 60
    require(results["forced_keyframes"] == forced and results["forced_idr_passed"] == forced,
            "forced IDR verification incomplete")
    require(forced <= results["idr_frames"] <= frames, "invalid IDR count")
    require(results["decoder_hardware_used"] is False and results["decoder_hardware_query_success"] is True,
            "independent software decode was not confirmed")
    for field, limit in (("luma_mae_max", 12), ("chroma_mae_max", 15), ("region_mae_max", 24),
                         ("thin_line_mae_max", 45), ("marker_error_max", 32)):
        require(results[field] <= limit, "decoded pixels exceed error bounds")
    elapsed, fps = results["elapsed_seconds"], results["encoded_fps"]
    require(elapsed > 0 and fps > 0, "missing elapsed time")
    if elapsed > 0:
        require(math.isclose(fps, frames / elapsed, rel_tol=1e-6, abs_tol=1e-6), "frame rate inconsistent with duration")
    require(0 < results["encode_ms_p50"] <= results["encode_ms_p95"] <= results["encode_ms_max"],
            "invalid latency quantiles")
    if profile == "qualification":
        require(elapsed >= frames / 60 - 0.5 and 59.5 <= fps <= 60.5, "sustained nominal 60 FPS was not met")
        require(results["encode_ms_p95"] <= 1000 / 60, "encoder p95 exceeds one 60 Hz frame")
    statuses = report["statuses"]
    require(isinstance(statuses, dict) and set(statuses) == STATUS_KEYS and
            all(_integer(value) and value == 0 for value in statuses.values()), "required API statuses did not all succeed")

    for field, pattern in (("candidate_sha256", SHA256), ("loaded_encoder_sha256", SHA256), ("upstream_tag", TAG)):
        value = report[field]
        require(isinstance(value, str) and (bool(pattern.fullmatch(value)) or
                (profile == "smoke" and field != "loaded_encoder_sha256" and value == "")), "invalid provenance format")
    for expected, field in ((candidate_sha256, "candidate_sha256"), (encoder_sha256, "loaded_encoder_sha256"),
                            (upstream_tag, "upstream_tag")):
        if expected is not None:
            require(report[field] == expected, "artifact provenance mismatch")
        elif profile == "qualification":
            problems.append("qualification requires externally supplied artifact provenance")
    return problems


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    output: dict = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("duplicate JSON key")
        output[key] = value
    return output


def read_report(path: Path) -> object:
    if path.stat().st_size > 64 * 1024:
        raise ValueError("report exceeds the public schema size limit")
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON number")))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--profile", choices=("smoke", "qualification"), required=True)
    parser.add_argument("--candidate-sha256")
    parser.add_argument("--encoder-sha256")
    parser.add_argument("--upstream-tag")
    args = parser.parse_args()
    try:
        report = read_report(args.report)
        problems = validate_report(report, profile=args.profile, candidate_sha256=args.candidate_sha256,
                                   encoder_sha256=args.encoder_sha256, upstream_tag=args.upstream_tag)
    except (OSError, ValueError, TypeError, RecursionError):
        print("FAIL: report is unreadable or malformed", file=sys.stderr)
        return 2
    if problems:
        for problem in dict.fromkeys(problems):
            print(f"FAIL: {problem}", file=sys.stderr)
        return 1
    digest = hashlib.sha256(args.report.read_bytes()).hexdigest()
    print(f"PASS: {args.profile}; report_sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
