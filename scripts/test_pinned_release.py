#!/usr/bin/env python3
"""Run existing adapter/assembly/native tests against the exact pinned package."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from build_candidate import ROOT, download, extract_bundle, read_state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=ROOT / 'state/upstream.json')
    parser.add_argument('--cache', type=Path, default=ROOT / 'build/cache')
    args = parser.parse_args()
    state = read_state(args.state)
    package = download(state, args.cache)
    tag = state['release']['tag']
    with tempfile.TemporaryDirectory(prefix='nvenc3060-test-') as temporary:
        bundle = Path(temporary) / 'NVMTLDriver.bundle'
        extract_bundle(package, bundle)
        environment = dict(os.environ, PYTHONPATH=str(ROOT / 'src'),
                           NVENC3060_TEST_PROFILE=f'{tag}.json',
                           NVENC3060_TEST_INPUT_DIR=str(bundle / 'Contents/MacOS'))
        subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests',
                        '-p', 'test_adapter.py'], cwd=ROOT, env=environment, check=True)


if __name__ == '__main__':
    main()
