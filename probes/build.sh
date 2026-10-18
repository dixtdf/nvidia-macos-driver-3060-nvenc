#!/bin/bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
output="${1:-$root/build/probes}"
mkdir -p "$output"
xcrun clang++ -std=c++17 -O2 -Wall -Wextra -Werror -fobjc-arc -arch x86_64 -mmacosx-version-min=15.0 \
  "$root/probes/nvenc_acceptance.mm" -o "$output/nvenc-acceptance" \
  -framework Foundation -framework CoreFoundation -framework CoreMedia \
  -framework CoreVideo -framework VideoToolbox -framework IOKit
printf 'Built %s/nvenc-acceptance\n' "$output"
