from __future__ import annotations

import copy
import ctypes
import mmap
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import tempfile
import unittest

from nvenc3060.adapter import (
    AdaptationError, adapt_bundle, adapt_bytes, load_profile, sha256,
    validate_profile,
)


ROOT = Path(__file__).resolve().parents[1]
PROFILE = load_profile(ROOT / "profiles/v2.3.6.json")


def fixture(name: str, filetype: int) -> tuple[bytes, dict]:
    """A small independent thin Mach-O, with one RX code section."""
    data = bytearray(512)
    struct.pack_into("<8I", data, 0, 0xFEEDFACF, 0x01000007, 3, filetype, 1, 152, 0, 0)
    struct.pack_into("<II16s4Q4I", data, 32, 0x19, 152, b"__TEXT", 0, 512, 0, 512, 5, 5, 1, 0)
    struct.pack_into("<16s16sQQIIIIIIII", data, 104, b"__text", b"__TEXT", 256, 256,
                     256, 4, 0, 0, 0x80000400, 0, 0, 0)
    data[256:260] = b"\x90\x90\x90\xc3"
    patched = bytearray(data)
    patched[256:258] = b"\x31\xc0"
    item = {"path": name, "size": 512, "macho_filetype": filetype,
            "sha256_before": sha256(data), "sha256_after": sha256(patched),
            "patches": [{"name": "synthetic", "offset": 256,
                         "before_hex": "9090", "after_hex": "31c0"}]}
    return bytes(data), item


class AdapterGuards(unittest.TestCase):
    def setUp(self):
        self.binary, self.item = fixture("libnvenc_h264.dylib", 6)

    def test_exact_patch(self):
        result = adapt_bytes(self.binary, self.item)
        self.assertEqual(result[256:260], b"\x31\xc0\x90\xc3")
        self.assertEqual(self.binary[256:260], b"\x90\x90\x90\xc3")
        self.assertEqual(sha256(result), self.item["sha256_after"])

    def test_unknown_digest_refused(self):
        data = bytearray(self.binary)
        data[-1] ^= 1
        with self.assertRaises(AdaptationError):
            adapt_bytes(bytes(data), self.item)

    def test_repeated_patch_refused(self):
        with self.assertRaises(AdaptationError):
            adapt_bytes(adapt_bytes(self.binary, self.item), self.item)

    def test_original_bytes_and_result_digest_guard(self):
        for key, value in (("before_hex", "9091"), ("after_hex", "31c1")):
            with self.subTest(key=key):
                item = copy.deepcopy(self.item)
                item["patches"][0][key] = value
                with self.assertRaises(AdaptationError):
                    adapt_bytes(self.binary, item)

    def test_architecture_and_executable_bounds(self):
        for offset, value in ((4, 0x0100000C), (36, 0), (92, 1), (148, 255)):
            with self.subTest(offset=offset):
                data = bytearray(self.binary)
                struct.pack_into("<I", data, offset, value)
                item = copy.deepcopy(self.item)
                item["sha256_before"] = sha256(data)
                with self.assertRaises(AdaptationError):
                    adapt_bytes(bytes(data), item)

    def test_patch_outside_text(self):
        item = copy.deepcopy(self.item)
        item["patches"][0]["offset"] = 250
        item["patches"][0]["before_hex"] = "0000"
        with self.assertRaises(AdaptationError):
            adapt_bytes(self.binary, item)

    def test_bundle_validates_every_input_before_creating_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            data, plugin = fixture("NVMTLDriver", 8)
            (source / "NVMTLDriver").write_bytes(data)
            (source / "libnvenc_h264.dylib").write_bytes(self.binary + b"bad")
            profile = copy.deepcopy(PROFILE)
            profile["files"] = [plugin, self.item]
            with self.assertRaises(AdaptationError):
                adapt_bundle(profile, source, root / "output")
            self.assertFalse((root / "output").exists())
            self.assertEqual((source / "NVMTLDriver").read_bytes(), data)

    def test_bundle_is_new_copy_with_audit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            data, plugin = fixture("NVMTLDriver", 8)
            (source / "NVMTLDriver").write_bytes(data)
            (source / "libnvenc_h264.dylib").write_bytes(self.binary)
            profile = copy.deepcopy(PROFILE)
            profile["files"] = [plugin, self.item]
            report = adapt_bundle(profile, source, root / "output")
            self.assertFalse(report["hardware_qualified"])
            self.assertEqual({p.name for p in (root / "output").iterdir()},
                             {"NVMTLDriver", "libnvenc_h264.dylib", "audit.json"})
            self.assertEqual((source / "NVMTLDriver").read_bytes(), data)
            with self.assertRaises(AdaptationError):
                adapt_bundle(profile, source, root / "output")
            with self.assertRaises(AdaptationError):
                adapt_bundle(profile, source, source / "child")

    def test_profile_overlap_path_and_status_guards(self):
        mutations = [
            lambda p: p.update(status="qualified"),
            lambda p: p.update(hardware_qualified=True),
            lambda p: p["hardware"].update(device_id="2503"),
            lambda p: p["files"][0].update(path="../NVMTLDriver"),
            lambda p: p["files"][0]["patches"].append(p["files"][0]["patches"][0]),
        ]
        for change in mutations:
            profile = copy.deepcopy(PROFILE)
            change(profile)
            with self.assertRaises(AdaptationError):
                validate_profile(profile)

    def test_profile_h264_only(self):
        plugin = next(f for f in PROFILE["files"] if f["path"] == "NVMTLDriver")
        self.assertEqual(len(plugin["patches"]), 1)
        patch = plugin["patches"][0]
        self.assertEqual(patch["offset"], 0x9C879)
        self.assertEqual(bytes.fromhex(patch["after_hex"]), b"\xe9\xb1\x00\x00\x00\x90")
        # The sole plug-in edit jumps to this H.264 function's registration path.
        self.assertEqual(patch["offset"] + 5 + struct.unpack("<i", bytes.fromhex(patch["after_hex"])[1:5])[0],
                         0x9C92F)


def native_wrapper(payload: bytes, chroma: bool) -> bytes:
    """Call the exact machine-code body with its original encoder registers.

    The reviewed outer-loop jump is replaced with a return epilogue only here.
    All tiling instructions and the inner branch are executed unchanged.
    ABI: void pack(uint32 row, uint32 pitch, byte *source, byte *dest, uint64 width).
    """
    if payload[-5] != 0xE9:
        raise AssertionError("expected reviewed outer-loop jump")
    if os.name == "nt":
        # Preserve Windows nonvolatile rsi/rdi as well as the common registers.
        prologue = bytes.fromhex("5341544156415757564189cc4d89c6")
        if chroma:
            prologue += bytes.fromhex("4d89ca488b5c24584883ec20895424104989e1")
        else:
            prologue += bytes.fromhex("4c89cb4c8b4c24584883ec20895424104989e7")
        epilogue = bytes.fromhex("4883c4205e5f415f415e415c5bc3")
    else:
        prologue = bytes.fromhex("534154415641574189fc4989d6")
        if chroma:
            prologue += bytes.fromhex("4989ca4c89c34883ec20897424104989e1")
        else:
            prologue += bytes.fromhex("4889cb4d89c14883ec20897424104989e7")
        epilogue = bytes.fromhex("4883c420415f415e415c5bc3")
    return prologue + payload[:-5] + epilogue


class Executable:
    def __init__(self, code: bytes):
        self.mapping = None
        if os.name == "nt":
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_uint32]
            kernel.VirtualAlloc.restype = ctypes.c_void_p
            kernel.VirtualProtect.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32,
                                             ctypes.POINTER(ctypes.c_uint32)]
            kernel.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32]
            self.address = kernel.VirtualAlloc(None, len(code), 0x3000, 0x04)
            if not self.address:
                raise ctypes.WinError(ctypes.get_last_error())
            ctypes.memmove(self.address, code, len(code))
            old = ctypes.c_uint32()
            if not kernel.VirtualProtect(self.address, len(code), 0x20, ctypes.byref(old)):
                kernel.VirtualFree(self.address, 0, 0x8000)
                raise ctypes.WinError(ctypes.get_last_error())
            self.kernel = kernel
        else:
            self.mapping = mmap.mmap(-1, len(code), prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC)
            self.mapping.write(code)
            self.address = ctypes.addressof(ctypes.c_char.from_buffer(self.mapping))
        self.function = ctypes.CFUNCTYPE(None, ctypes.c_uint32, ctypes.c_uint32,
                                        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64)(self.address)

    def close(self):
        if self.mapping is not None:
            self.mapping.close()
        elif self.address:
            self.kernel.VirtualFree(self.address, 0, 0x8000)
        self.address = 0


def classic_reference(x: int, y: int, pitch: int) -> int:
    # Independent expression from the documented classic GOB/bh2 byte layout.
    return ((y // 16) * (pitch // 64) * 1024 + (x // 64) * 1024 +
            ((y % 16) // 8) * 512 + ((x % 64) // 32) * 256 +
            ((y % 8) // 4) * 128 + ((x % 32) // 16) * 64 +
            (y % 4) * 16 + x % 16)


@unittest.skipUnless(platform.machine().lower() in ("amd64", "x86_64"), "native x86-64 execution required")
class NativePackerDifferential(unittest.TestCase):
    def test_reference_detects_wrong_high_row_addressing(self):
        # Regression control: OR-ing the high row base into the x address works
        # for small power-of-two pitches, but tears 1920-byte-pitch pictures.
        library = next(f for f in PROFILE["files"] if f["path"] == "libnvenc_h264.dylib")
        patch = library["patches"][0]
        payload = bytes.fromhex(patch["after_hex"])[:patch["payload_size"]]
        self.assertEqual(payload.count(bytes.fromhex("89cf01d7")), 1)
        bad_payload = payload.replace(bytes.fromhex("89cf01d7"), bytes.fromhex("89cf09d7"))
        executable = Executable(native_wrapper(bad_payload, False))
        try:
            width, y = 1920, 16
            length = width * 32
            source = ctypes.create_string_buffer(b"\x31" * width)
            destination = ctypes.create_string_buffer(b"\xcd" * (length + 128))
            expected = bytearray(b"\xcd" * (length + 128))
            executable.function(y, width, ctypes.addressof(source), ctypes.addressof(destination) + 64, width)
            for x in range(width):
                expected[64 + classic_reference(x, y, width)] = 0x31
            actual = destination.raw[:length + 128]
            self.assertNotEqual(actual, bytes(expected))
            self.assertEqual(actual[:64], b"\xcd" * 64)
            self.assertEqual(actual[-64:], b"\xcd" * 64)
        finally:
            executable.close()

    def test_source_assembles_to_exact_profile_payloads(self):
        clang = os.getenv("NVENC3060_CLANG") or shutil.which("clang")
        objcopy = os.getenv("NVENC3060_OBJCOPY") or shutil.which("llvm-objcopy") or shutil.which("objcopy")
        if not clang or not objcopy:
            self.skipTest("clang and objcopy required for independent assembly reproduction")
        library = next(f for f in PROFILE["files"] if f["path"] == "libnvenc_h264.dylib")
        assembly = ROOT / "src/nvenc3060/patches/v2_3_6_t2d.S"
        with tempfile.TemporaryDirectory() as temporary:
            for chroma, patch in enumerate(library["patches"]):
                object_path = Path(temporary) / f"{chroma}.o"
                raw_path = Path(temporary) / f"{chroma}.bin"
                command = [clang, "-target", "x86_64-unknown-linux-gnu", "-c", str(assembly), "-o", str(object_path)]
                if chroma:
                    command.append("-DCHROMA")
                subprocess.run(command, check=True, capture_output=True)
                subprocess.run([objcopy, "-O", "binary", "--only-section=.text", str(object_path), str(raw_path)],
                               check=True, capture_output=True)
                payload = bytes.fromhex(patch["after_hex"])[:patch["payload_size"]]
                self.assertEqual(raw_path.read_bytes(), payload)
                target = 0x6450 if chroma else 0x6330
                self.assertEqual(patch["offset"] + len(payload) + struct.unpack("<i", payload[-4:])[0], target)

    def test_actual_patched_instructions_both_planes(self):
        library = next(f for f in PROFILE["files"] if f["path"] == "libnvenc_h264.dylib")
        for chroma, patch in enumerate(library["patches"]):
            payload = bytes.fromhex(patch["after_hex"])[:patch["payload_size"]]
            executable = Executable(native_wrapper(payload, bool(chroma)))
            try:
                for width, rows in ((16, 16), (64, 32), (80, 48), (128, 32), (1920, 1088)):
                    with self.subTest(chroma=chroma, width=width, rows=rows):
                        pitch = (width + 63) & -64
                        length = pitch * rows
                        destination = ctypes.create_string_buffer(b"\xcd" * (length + 128))
                        expected = bytearray(b"\xcd" * (length + 128))
                        for y in range(rows):
                            row = bytes((x * 13 + y * 7 + (x >> 3) * 17 + (y >> 2) * 43) % 251
                                        for x in range(width))
                            source = ctypes.create_string_buffer(row)
                            executable.function(y, pitch, ctypes.addressof(source),
                                                ctypes.addressof(destination) + 64, width)
                            for x, value in enumerate(row):
                                expected[64 + classic_reference(x, y, pitch)] = value
                        self.assertEqual(destination.raw[:length + 128], bytes(expected))
            finally:
                executable.close()


@unittest.skipUnless(os.getenv("NVENC3060_TEST_INPUT_DIR"), "release binaries supplied only in artifact CI")
class ActualReleaseInputs(unittest.TestCase):
    def test_real_release_produces_exact_reviewed_outputs(self):
        source = Path(os.environ["NVENC3060_TEST_INPUT_DIR"])
        with tempfile.TemporaryDirectory() as directory:
            report = adapt_bundle(PROFILE, source, Path(directory) / "candidate")
            self.assertFalse(report["hardware_qualified"])
            for item in PROFILE["files"]:
                self.assertEqual(sha256((source / item["path"]).read_bytes()), item["sha256_before"])
                self.assertEqual(sha256((Path(directory) / "candidate" / item["path"]).read_bytes()),
                                 item["sha256_after"])


if __name__ == "__main__":
    unittest.main()
