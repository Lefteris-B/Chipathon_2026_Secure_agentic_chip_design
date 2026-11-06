#!/usr/bin/env bash
# T1.3: gate-level sim of verilog/gl/D10_D.pnl.v in the pinned image (no network).
#
#   test/gl/run.sh
#
# Output: test/gl/out/. Exits non-zero if any run differs from its expected result.
set -euo pipefail

IMAGE="hpretl/iic-osic-tools:chipathon26"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

rm -rf "$ROOT/test/gl/out"
mkdir -p "$ROOT/test/gl/out"
docker run --rm --network=none --cpus=4 --memory=8g \
  -v "$ROOT":/work -w /work \
  "$IMAGE" --skip \
  bash test/gl/sim.sh
