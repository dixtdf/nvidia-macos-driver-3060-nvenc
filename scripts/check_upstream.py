#!/usr/bin/env python3
"""Pin the latest official release as an unvalidated RTX 3060 candidate.

Release descriptions and other free-form upstream content are deliberately not
copied into repository automation. A candidate is provenance, never approval.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
import urllib.error
import urllib.request

UPSTREAM = "nullmoth/nvidia-macos-driver"
API_ROOT = "https://api.github.com"
TAG = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"sha256:([0-9a-f]{64})\Z")


class CandidateError(ValueError):
    """Missing, ambiguous, or changed upstream provenance."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CandidateError("GitHub API redirected; refusing to forward credentials")


class GitHubAPI:
    def __init__(self, token: str | None = None):
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def get(self, path: str) -> dict[str, Any]:
        if not path.startswith(f"/repos/{UPSTREAM}/") or ".." in path:
            raise CandidateError("API path is outside the official upstream repository")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "nvidia-macos-driver-3060-nvenc-release-check",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(API_ROOT + path, headers=headers)
        with self.opener.open(request, timeout=60) as response:
            # API responses are small; reject unexpectedly large data.
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise CandidateError("GitHub API response exceeds 2 MiB")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise CandidateError("Expected a GitHub API object")
        return result


def positive_integer(value: Any, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise CandidateError(f"Invalid {field}")
    return value


def version(tag: Any) -> tuple[int, int, int]:
    if not isinstance(tag, str) or not TAG.fullmatch(tag):
        raise CandidateError("Only stable vMAJOR.MINOR.PATCH releases are accepted")
    return tuple(int(part) for part in tag[1:].split("."))


def resolve_commit(api: GitHubAPI, tag: str) -> str:
    version(tag)
    ref = api.get(f"/repos/{UPSTREAM}/git/ref/tags/{tag}")
    if ref.get("ref") != f"refs/tags/{tag}":
        raise CandidateError("GitHub returned a different tag reference")
    obj = ref.get("object", {})
    for _ in range(6):
        if not isinstance(obj, dict):
            break
        sha = obj.get("sha")
        if not isinstance(sha, str) or not SHA.fullmatch(sha):
            break
        if obj.get("type") == "commit":
            return sha
        if obj.get("type") != "tag":
            break
        annotated = api.get(f"/repos/{UPSTREAM}/git/tags/{sha}")
        if annotated.get("sha") != sha:
            raise CandidateError("Annotated tag object SHA does not match")
        obj = annotated.get("object")
    raise CandidateError("Release tag does not resolve to a commit")


def build_candidate(release: dict[str, Any], commit: str) -> dict[str, Any]:
    tag = release.get("tag_name")
    version(tag)
    if release.get("draft") is not False or release.get("prerelease") is not False:
        raise CandidateError("Draft and prerelease builds are not tracked automatically")
    if not isinstance(commit, str) or not SHA.fullmatch(commit):
        raise CandidateError("Invalid release commit")
    release_url = f"https://github.com/{UPSTREAM}/releases/tag/{tag}"
    if release.get("html_url") != release_url:
        raise CandidateError("Release URL is outside the expected official release")
    published = release.get("published_at")
    try:
        timestamp = dt.datetime.fromisoformat(published.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise CandidateError("Invalid release publication timestamp") from exc
    if timestamp.utcoffset() != dt.timedelta(0):
        raise CandidateError("Publication timestamp must be UTC")
    asset_name = f"nullmoth-nvidia-{tag[1:]}.tar.gz"
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise CandidateError("Release has no asset list")
    matches = [item for item in assets if isinstance(item, dict) and item.get("name") == asset_name]
    if len(matches) != 1:
        raise CandidateError("Expected exactly one complete official driver tarball")
    asset = matches[0]
    asset_url = f"https://github.com/{UPSTREAM}/releases/download/{tag}/{asset_name}"
    if asset.get("browser_download_url") != asset_url or asset.get("state") != "uploaded":
        raise CandidateError("Asset is not a completed official release upload")
    digest = asset.get("digest")
    digest_match = DIGEST.fullmatch(digest) if isinstance(digest, str) else None
    if not digest_match:
        raise CandidateError("Official asset is missing a SHA-256 digest; manual review required")
    size = positive_integer(asset.get("size"), "asset size")
    if size > 2 * 1024 * 1024 * 1024:
        raise CandidateError("Driver archive exceeds the 2 GiB download limit")
    return {
        "schema_version": 1,
        "upstream_repository": UPSTREAM,
        "release": {
            "id": positive_integer(release.get("id"), "release ID"),
            "tag": tag,
            "commit": commit,
            "published_at": published,
            "url": release_url,
        },
        "asset": {
            "id": positive_integer(asset.get("id"), "asset ID"),
            "name": asset_name,
            "url": asset_url,
            "size": size,
            "sha256": digest_match.group(1),
        },
        "hardware_validation": "not-run",
    }


def update_candidate(path: Path, candidate: dict[str, Any]) -> bool:
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        if previous == candidate:
            return False
        try:
            previous_version = version(previous["release"]["tag"])
        except (KeyError, TypeError) as exc:
            raise CandidateError("Existing candidate state is malformed") from exc
        if version(candidate["release"]["tag"]) <= previous_version:
            raise CandidateError("Same or older release changed; refusing to replace provenance automatically")
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(candidate, ensure_ascii=True, indent=2) + "\n"
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False) as handle:
            temporary_path = Path(handle.name)
            handle.write(serialized)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("state/upstream.json"))
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    api = GitHubAPI(os.environ.get("GITHUB_TOKEN"))
    try:
        release = api.get(f"/repos/{UPSTREAM}/releases/latest")
        tag = release.get("tag_name")
        version(tag)
        candidate = build_candidate(release, resolve_commit(api, tag))
        changed = update_candidate(args.output, candidate)
    except (CandidateError, urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        parser.exit(1, f"Release check failed: {exc}\n")
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"changed={'true' if changed else 'false'}\ntag={tag}\n")
    print(f"{tag}: {'new unvalidated candidate' if changed else 'unchanged'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
