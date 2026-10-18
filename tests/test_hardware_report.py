"""Gate contract tests; fabricated fixtures are never hardware acceptance evidence."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("hardware_report", Path(__file__).parents[1] / "scripts/validate_hardware_report.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def fixture(profile="smoke"):
    frames = 3600 if profile == "qualification" else 30
    return {
        "schema_version": 2, "probe": "3060-nvenc-h264-v2", "profile": profile, "pass": True,
        "hardware_required": True, "software_fallback_allowed": False, "hardware_used": True,
        "encoder_requested": MODULE.ENCODER_ID, "encoder_selected": MODULE.ENCODER_ID,
        "candidate_sha256": "a" * 64, "loaded_encoder_sha256": "b" * 64, "upstream_tag": "v2.3.6",
        "gpu": {"detected": True, "matching_devices": 1, "nvidia_display_devices": 1, "vendor_id": 0x10DE, "device_id": 0x2504},
        "video": {"codec": "h264", "input_format": "420v", "input_layout": "ordinary_nv12",
                  "width": 1920, "height": 1080, "fps": 60, "requested_frames": frames},
        "results": {
            "encoded_frames": frames, "decoded_frames": frames, "verified_frames": frames,
            "callback_errors": 0, "dropped_frames": 0, "forced_keyframes": (frames + 59) // 60,
            "forced_idr_passed": (frames + 59) // 60, "idr_frames": (frames + 59) // 60,
            "decoder_hardware_used": False, "decoder_hardware_query_success": True,
            "decoder_hardware_query_status": 0, "decoder_hardware_disabled_by_spec": True,
            "luma_mae_max": 1.1, "chroma_mae_max": 1.5, "region_mae_max": 3,
            "thin_line_mae_max": 6, "marker_error_max": 4, "elapsed_seconds": frames / 60,
            "encoded_fps": 60, "encode_ms_p50": 3, "encode_ms_p95": 5, "encode_ms_max": 8,
        },
        "statuses": dict.fromkeys(MODULE.STATUS_KEYS, 0), "errors": [],
    }


def unsupported_decoder_fixture(profile="smoke"):
    report = fixture(profile)
    report["results"].update(decoder_hardware_used=None, decoder_hardware_query_success=False,
                             decoder_hardware_query_status=-12900)
    return report


class HardwareReportTest(unittest.TestCase):
    def test_valid_smoke(self):
        self.assertEqual([], MODULE.validate_report(fixture(), profile="smoke"))

    def test_valid_qualification_with_external_binding(self):
        self.assertEqual([], MODULE.validate_report(fixture("qualification"), profile="qualification",
            candidate_sha256="a" * 64, encoder_sha256="b" * 64, upstream_tag="v2.3.6"))

    def test_unsupported_query_with_explicit_software_spec_is_valid(self):
        self.assertEqual([], MODULE.validate_report(unsupported_decoder_fixture(), profile="smoke"))
        self.assertEqual([], MODULE.validate_report(unsupported_decoder_fixture("qualification"),
            profile="qualification", candidate_sha256="a" * 64, encoder_sha256="b" * 64, upstream_tag="v2.3.6"))

    def test_decoder_evidence_must_be_consistent_and_exactly_typed(self):
        cases = [
            # Even a supported false observation must preserve the explicit no-HW specification.
            (0, True, False, False, 0), (0, True, False, 1, 0),
            # Query true, unknown, numeric false, or status-0 type failure cannot pass.
            (0, True, True, True, 0), (0, True, None, True, 0),
            (0, True, 0, True, 0), (0, False, None, True, 0),
            (False, True, False, True, 0), (0.0, True, False, True, 0),
            (0, 1, False, True, 0),
            # Unsupported must stay unknown, with a failed query and successful creation.
            (-12900, True, None, True, 0), (-12900, False, False, True, 0),
            (-12900, False, True, True, 0), (-12900, False, None, False, 0),
            (-12900, False, None, True, -12908), (-12900, False, None, True, False),
            (-12900.0, False, None, True, 0), (-12900, 0, None, True, 0),
            (-12903, False, None, True, 0), (None, False, None, True, 0),
        ]
        for status, success, used, disabled, created in cases:
            with self.subTest(status=status, success=success, used=used, disabled=disabled, created=created):
                report = fixture()
                report["results"].update(decoder_hardware_query_status=status,
                    decoder_hardware_query_success=success, decoder_hardware_used=used,
                    decoder_hardware_disabled_by_spec=disabled)
                report["statuses"]["decoder_create"] = created
                self.assertTrue(MODULE.validate_report(report, profile="smoke"))

    def test_unsupported_decoder_does_not_bypass_encoder_content_or_provenance(self):
        mutations = [("hardware_used", False), ("encoder_selected", "software"),
                     ("results.luma_mae_max", 13), ("results.verified_frames", 29),
                     ("results.forced_idr_passed", 0), ("loaded_encoder_sha256", "c" * 64)]
        for field, value in mutations:
            with self.subTest(field=field):
                report = unsupported_decoder_fixture()
                keys = field.split(".")
                target = report if len(keys) == 1 else report[keys[0]]
                target[keys[-1]] = value
                self.assertTrue(MODULE.validate_report(report, profile="smoke", encoder_sha256="b" * 64))
        report = unsupported_decoder_fixture("qualification")
        report["results"]["encode_ms_p95"] = 17
        report["results"]["encode_ms_max"] = 18
        self.assertTrue(MODULE.validate_report(report, profile="qualification",
            candidate_sha256="a" * 64, encoder_sha256="b" * 64, upstream_tag="v2.3.6"))

    def test_old_schema_cannot_claim_new_decoder_evidence(self):
        for old_shape in (False, True):
            with self.subTest(old_shape=old_shape):
                report = fixture()
                report.update(schema_version=1, probe="3060-nvenc-h264-v1")
                if old_shape:
                    del report["results"]["decoder_hardware_disabled_by_spec"]
                    del report["results"]["decoder_hardware_query_status"]
                self.assertTrue(MODULE.validate_report(report, profile="smoke"))

    def test_qualification_needs_external_binding(self):
        self.assertTrue(MODULE.validate_report(fixture("qualification"), profile="qualification"))

    def test_rejects_wrong_binding(self):
        self.assertTrue(MODULE.validate_report(fixture(), profile="smoke", encoder_sha256="c" * 64))

    def test_mutated_evidence_is_rejected(self):
        cases = [
            ("pass", False), ("hardware_used", False), ("hardware_required", False),
            ("software_fallback_allowed", True), ("encoder_selected", "com.apple.videotoolbox.videoencoder.h264"),
            ("encoder_requested", "other"), ("errors", ["private-token"]),
            ("gpu.device_id", 0x2484), ("gpu.matching_devices", 2), ("gpu.detected", False),
            ("gpu.nvidia_display_devices", 2),
            ("video.input_layout", "prepacked"), ("video.fps", 30), ("video.width", 1280),
            ("video.requested_frames", True), ("results.encoded_frames", 29), ("results.decoded_frames", 29),
            ("results.verified_frames", 29), ("results.dropped_frames", 1), ("results.callback_errors", 1),
            ("results.forced_idr_passed", 0), ("results.idr_frames", 0),
            ("results.decoder_hardware_used", True), ("results.decoder_hardware_query_success", False),
            ("results.luma_mae_max", 13), ("results.chroma_mae_max", 16), ("results.region_mae_max", 25),
            ("results.thin_line_mae_max", 46), ("results.marker_error_max", 33),
            ("results.encode_ms_p95", float("nan")), ("results.encode_ms_max", -1),
            ("results.encode_ms_max", 10 ** 1000),
            ("results.encoded_fps", 120), ("statuses.encoder_create", -1),
            ("loaded_encoder_sha256", ""), ("candidate_sha256", "secret"), ("upstream_tag", "private-host"),
        ]
        for field, value in cases:
            with self.subTest(field=field):
                report = fixture()
                cursor = report
                keys = field.split(".")
                for key in keys[:-1]:
                    cursor = cursor[key]
                cursor[keys[-1]] = value
                self.assertTrue(MODULE.validate_report(report, profile="smoke"))

    def test_rejects_unknown_fields_at_every_level(self):
        for section in (None, "gpu", "video", "results", "statuses"):
            with self.subTest(section=section):
                report = fixture()
                target = report if section is None else report[section]
                target["serial_number"] = "must-never-be-published"
                self.assertTrue(MODULE.validate_report(report, profile="smoke"))

    def test_rejects_short_or_slow_qualification(self):
        report = fixture("qualification")
        report["results"]["elapsed_seconds"] = 61
        report["results"]["encoded_fps"] = 3600 / 61
        self.assertTrue(MODULE.validate_report(report, profile="qualification",
            candidate_sha256="a" * 64, encoder_sha256="b" * 64, upstream_tag="v2.3.6"))
        report = fixture("qualification")
        report["results"]["encode_ms_p95"] = 17
        report["results"]["encode_ms_max"] = 18
        self.assertTrue(MODULE.validate_report(report, profile="qualification",
            candidate_sha256="a" * 64, encoder_sha256="b" * 64, upstream_tag="v2.3.6"))
        self.assertTrue(MODULE.validate_report(fixture(), profile="qualification"))

    def test_json_duplicate_nonfinite_and_oversized_fail(self):
        for data in ('{"pass": true, "pass": false}', '{"elapsed": NaN}', "x" * 65537):
            with self.subTest(data=data[:30]), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "report.json"
                path.write_text(data)
                with self.assertRaises(ValueError):
                    MODULE.read_report(path)

    def test_json_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps(fixture()))
            self.assertEqual([], MODULE.validate_report(MODULE.read_report(path), profile="smoke"))


if __name__ == "__main__":
    unittest.main()
