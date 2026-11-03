#!/usr/bin/env bash
# Regenerate gds/D10_D.gds from source with plain LibreLane inside the pinned
# IIC-OSIC-TOOLS image (no agent, no model, no network). See repro/README.md.
#
#   repro/D10_D/run.sh [run-tag]
#
# Output: repro/D10_D/runs/<run-tag>/final/{gds,pnl,def,metrics.json}
set -euo pipefail

IMAGE="hpretl/iic-osic-tools:chipathon26"
DIGEST="sha256:b3a754709b6d71bf11ce9357c07d2d63b6a4562c2a36b20c1dde33dd0079a9b4"
TAG="${1:-repro}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

got="$(docker image inspect --format '{{.Id}}' "$IMAGE" 2>/dev/null || true)"
if [[ "$got" != "$DIGEST" ]]; then
  echo "warning: $IMAGE id is '${got:-<not pulled>}', expected $DIGEST" >&2
fi

# The whole repo is mounted because MACROS reference ../../connectors/.
docker run --rm --network=none --cpus=4 --memory=32g \
  -v "$ROOT":/work -w /work/repro/D10_D \
  "$IMAGE" --skip \
  librelane --run-tag "$TAG" --overwrite config.json

M="$ROOT/repro/D10_D/runs/$TAG/final/metrics.json"
python3 - "$M" <<'EOF'
import json, sys
m = json.load(open(sys.argv[1]))
for k in [
    "design__power_grid_violation__count",
    "ir__drop__worst", "ir__drop__avg",
    "magic__drc_error__count", "klayout__drc_error__count",
    "design__lvs_error__count", "antenna__violating__nets",
    "design__xor_difference__count",
    "timing__setup_vio__count", "timing__hold_vio__count",
    "design__max_slew_violation__count", "design__max_cap_violation__count",
    "timing__setup__ws", "timing__hold__ws",
]:
    print(f"{k:40s} {m.get(k)}")
EOF
