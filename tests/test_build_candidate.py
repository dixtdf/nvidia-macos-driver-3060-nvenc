from pathlib import Path
import copy
import hashlib
import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


build = module("build_candidate")
apply = module("apply_overlay")


class CandidateTests(unittest.TestCase):
    def test_state_rejects_wrong_origin_hash_and_version(self):
        state = json.loads((ROOT / "state/upstream.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(state))
            self.assertEqual(build.read_state(path), state)
            mutations = [("upstream_repository", "other/project"),
                         ("release.tag", "v2.3.6; echo injected"),
                         ("release.commit", "main"), ("asset.sha256", "0"),
                         ("asset.url", "https://example.com/package.tar.gz"),
                         ("asset.name", "../package.tar.gz"), ("asset.size", -1)]
            for key, value in mutations:
                with self.subTest(key=key):
                    candidate = copy.deepcopy(state)
                    target = candidate
                    parts = key.split(".")
                    for part in parts[:-1]:
                        target = target[part]
                    target[parts[-1]] = value
                    path.write_text(json.dumps(candidate))
                    with self.assertRaises(ValueError):
                        build.read_state(path)

    def make_tar(self, path, extra=None):
        with tarfile.open(path, "w:gz") as archive:
            for name in (*build.CHANGED, "Contents/Info.plist"):
                value = ("fixture " + name).encode()
                entry = tarfile.TarInfo(build.BUNDLE + name)
                entry.size = len(value)
                archive.addfile(entry, io.BytesIO(value))
            if extra is not None:
                archive.addfile(extra, io.BytesIO(b"x") if extra.isreg() else None)

    def test_bundle_extraction_ignores_unrelated_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = tarfile.TarInfo("pkgroot/private.txt")
            entry.size = 1
            self.make_tar(root / "input.tar.gz", entry)
            hashes = build.extract_bundle(root / "input.tar.gz", root / "bundle")
            self.assertEqual(set(hashes), set(build.CHANGED) | {"Contents/Info.plist"})
            self.assertFalse((root / "private.txt").exists())

    def test_bundle_extraction_rejects_traversal_duplicates_links(self):
        for name, kind in (("../outside", tarfile.REGTYPE),
                           ("Contents/MacOS/NVMTLDriver", tarfile.REGTYPE),
                           ("contents/macos/nvmtldriver", tarfile.REGTYPE),
                           ("Contents/extra", tarfile.SYMTYPE)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                entry = tarfile.TarInfo(build.BUNDLE + name)
                entry.type = kind
                entry.linkname = "/outside"
                entry.size = 1 if kind == tarfile.REGTYPE else 0
                self.make_tar(root / "input.tar.gz", entry)
                with self.assertRaises(ValueError):
                    build.extract_bundle(root / "input.tar.gz", root / "bundle")
                self.assertFalse((root / "outside").exists())

    def test_cached_asset_must_match_size_and_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = b"not an official driver"
            asset = {"name": "fixture.tar.gz", "size": len(data),
                     "sha256": hashlib.sha256(data).hexdigest()}
            path = root / asset["name"]
            path.write_bytes(data)
            self.assertEqual(build.download({"asset": asset}, root), path)
            path.write_bytes(data + b"tamper")
            with self.assertRaises(ValueError):
                build.download({"asset": asset}, root)

    def test_application_verifies_every_file_and_refuses_unknown_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "owned").write_bytes(b"original")
            expected = {"owned": apply.sha256(root / "owned")}
            apply.verify_tree(root, expected)
            (root / "owned").write_bytes(b"changed")
            with self.assertRaises(ValueError):
                apply.verify_tree(root, expected)
            (root / "owned").write_bytes(b"original")
            (root / "unknown").write_bytes(b"user file")
            with self.assertRaises(ValueError):
                apply.verify_tree(root, expected)

    def test_manifest_paths_cannot_escape_or_collide(self):
        digest = "a" * 64
        for mapping in ({"../escape": digest}, {"/escape": digest}, {"C:file": digest},
                        {"dir\\file": digest}, {"Name": digest, "name": digest},
                        {"name.": digest}, {"name": "bad"}):
            with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                apply.validate_hashes(mapping)

    def test_backup_rejects_parent_traversal_and_symlink_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "live.bundle"
            target.mkdir()
            with self.assertRaises(ValueError):
                apply.backup_path(root / "unused" / ".." / "live.bundle" / "backup", target)
            alias = root / "alias"
            try:
                alias.symlink_to(target, target_is_directory=True)
            except OSError:
                return
            with self.assertRaises(ValueError):
                apply.backup_path(alias / "backup", target)
            self.assertFalse((target / "backup").exists())

    def test_failed_automatic_recovery_preserves_original_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, overlay, backup = root / "live.bundle", root / "overlay", root / "backup"
            target.mkdir()
            original, changed = {}, {}
            for name in apply.EXPECTED_FILES:
                path = target / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"original")
                original[name] = apply.sha256(path)
                candidate = overlay / apply.PREFIX / name
                candidate.parent.mkdir(parents=True, exist_ok=True)
                candidate.write_bytes(b"candidate")
                changed[name] = apply.sha256(candidate)
            (overlay / "manifest.json").write_text(json.dumps({
                "schema_version": 1, "status": "experimental-not-hardware-qualified",
                "hardware_validation": "not-run", "upstream_tag": "v2.3.6",
                "target_pci": {"vendor_id": "10de", "device_id": "2504"},
                "base_bundle_files": original, "overlay_files": changed,
            }))
            calls = []
            def swap(first, second):
                calls.append((first, second))
                if len(calls) == 2:
                    raise OSError("injected recovery failure")
                temporary = root / "exchange"
                first.rename(temporary)
                second.rename(first)
                temporary.rename(second)
            def signature(path):
                if path == target:
                    raise ValueError("injected post-swap verification failure")
            with mock.patch.object(apply, "TARGET", target), \
                 mock.patch.object(apply, "hardware_check"), \
                 mock.patch.object(apply, "signature_check", side_effect=signature), \
                 mock.patch.object(apply, "swap_directories", side_effect=swap), \
                 mock.patch.object(apply.os, "geteuid", return_value=0, create=True):
                with self.assertRaisesRegex(RuntimeError, "Preserved original stage"):
                    apply.apply(overlay, backup)
            stages = list(root.glob(".nvenc3060-stage-*"))
            self.assertEqual(len(stages), 1)
            apply.verify_tree(stages[0], original)
            apply.verify_tree(backup / target.name, original)


if __name__ == "__main__":
    unittest.main()
