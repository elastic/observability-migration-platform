#!/usr/bin/env bash
# Stamp each canonical fixture dashboard (in ../infra/grafana/dashboards/)
# with the rig's parity-prom datasource UID and copy it into the Grafana
# provisioning dir so the rig's Grafana picks it up. Also optionally fetch
# the canonical Node Exporter Full (id 1860) from grafana.com.
#
# Usage:
#   bash scripts/prepare-dashboards.sh           # stamp local fixtures only
#   bash scripts/prepare-dashboards.sh --1860   # also fetch dashboard 1860
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
REPO=$(cd "$HERE/.." && pwd)
SRC=$REPO/infra/grafana/dashboards
DST=$HERE/grafana/dashboards
mkdir -p "$DST"

PYTHON=${PYTHON:-python3}

for f in diverse-panels-test home k8s-views-global node-exporter-full prometheus-all; do
  if [ -f "$SRC/$f.json" ]; then
    $PYTHON - "$SRC/$f.json" "$DST/$f.json" <<'PY'
import json, sys
src, dst = sys.argv[1], sys.argv[2]
def stamp(o):
    if isinstance(o, dict):
        if o.get("type") == "prometheus" and "uid" in o:
            o["uid"] = "parity-prom"
        for v in o.values(): stamp(v)
    elif isinstance(o, list):
        for v in o: stamp(v)
data = json.loads(open(src).read())
stamp(data)
for t in (data.get("templating", {}).get("list") or []):
    if t.get("name") == "DS_PROMETHEUS":
        t.setdefault("current", {})["value"] = "parity-prom"
open(dst, "w").write(json.dumps(data, indent=2))
PY
    echo "stamped $f.json"
  fi
done

if [ "${1:-}" = "--1860" ]; then
  TARGET=$DST/node-exporter-full-1860.json
  if [ ! -f "$TARGET" ]; then
    echo "fetching canonical 1860 from grafana.com..."
    curl -fsSL 'https://grafana.com/api/dashboards/1860/revisions/latest/download' -o "$TARGET"
    $PYTHON - "$TARGET" <<'PY'
import json, sys
p = sys.argv[1]
def stamp(o):
    if isinstance(o, dict):
        if o.get("type") == "prometheus" and "uid" in o:
            o["uid"] = "parity-prom"
        for v in o.values(): stamp(v)
    elif isinstance(o, list):
        for v in o: stamp(v)
data = json.loads(open(p).read())
stamp(data)
for t in (data.get("templating", {}).get("list") or []):
    if t.get("name", "").startswith("DS_PROMETHEUS"):
        t.setdefault("current", {})["value"] = "parity-prom"
open(p, "w").write(json.dumps(data, indent=2))
PY
    echo "stamped node-exporter-full-1860.json"
  else
    echo "1860 already present"
  fi
fi

echo
echo "Dashboards in $DST:"
ls "$DST"
