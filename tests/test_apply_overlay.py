from pathlib import Path
import copy
import importlib.util
import plistlib
import subprocess
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("apply_overlay_hardware", ROOT / "scripts/apply_overlay.py")
apply = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apply)


def pci(entry_id, device=0x2504, code=0x030000, vendor=0x10DE):
    return {"IORegistryEntryID": entry_id,
            "vendor-id": vendor.to_bytes(4, "little"),
            "device-id": device.to_bytes(4, "little"),
            "class-code": code.to_bytes(4, "little")}


class HardwareInventoryTests(unittest.TestCase):
    def check(self, inventory, loaded="com.nullmoth.NVRM (2.3.6)"):
        responses = [subprocess.CompletedProcess([], 0, stdout=plistlib.dumps(inventory)),
                     subprocess.CompletedProcess([], 0, stdout=loaded)]
        with mock.patch.object(apply.sys, "platform", "darwin"), \
             mock.patch.object(apply.platform, "machine", return_value="x86_64"), \
             mock.patch.object(apply.subprocess, "run", side_effect=responses):
            apply.hardware_check("v2.3.6")

    def test_gpu_below_two_bridges_and_audio_sibling(self):
        gpu = pci(4294967751)
        audio = pci(4294967752, device=0x228E, code=0x040300)
        self.check([{"IORegistryEntryID": 1, "IORegistryEntryChildren": [
            {"IORegistryEntryID": 2, "IORegistryEntryChildren": [gpu, audio]}]}])

    def test_duplicate_roots_and_subtrees_count_gpu_once(self):
        gpu = pci(4294967751)
        tree = {"IORegistryEntryID": 1, "IORegistryEntryChildren": [gpu]}
        self.check([copy.deepcopy(tree), copy.deepcopy(tree), gpu])

    def test_second_distinct_gpu_is_rejected_even_with_same_device_id(self):
        inventory = [pci(1), {"IORegistryEntryChildren": [pci(2)]}]
        with self.assertRaisesRegex(ValueError, "exactly one NVIDIA display GPU"):
            self.check(inventory)

    def test_audio_is_not_a_display_gpu(self):
        audio = pci(2, device=0x228E, code=0x040300)
        self.check([pci(1), audio, copy.deepcopy(audio)])
        with self.assertRaisesRegex(ValueError, "exactly one NVIDIA display GPU"):
            self.check([audio])

    def test_other_nvidia_display_device_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "exactly one NVIDIA display GPU"):
            self.check([pci(1, device=0x2684)])

    def test_conflicting_duplicate_id_cannot_hide_another_gpu(self):
        with self.assertRaisesRegex(ValueError, "Conflicting PCI inventory"):
            self.check([pci(1), {"IORegistryEntryChildren": [pci(1, device=0x2684)]}])

    def test_missing_registry_id_is_rejected(self):
        gpu = pci(1)
        del gpu["IORegistryEntryID"]
        with self.assertRaisesRegex(ValueError, "valid registry ID"):
            self.check([gpu])

    def test_nested_gpu_still_requires_matching_loaded_kernel_version(self):
        with self.assertRaisesRegex(ValueError, "matching official NVRM"):
            self.check([{"IORegistryEntryChildren": [pci(1)]}], loaded="com.nullmoth.NVRM (1.9.0)")


if __name__ == "__main__":
    unittest.main()
