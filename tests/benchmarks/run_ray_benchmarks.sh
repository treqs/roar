#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/tests/e2e/ray/docker-compose.yml"
RESULTS_DIR="$ROOT_DIR/tests/benchmarks/results"

if command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
elif [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
else
  echo "ERROR: no python interpreter found (checked: python, .venv/bin/python, python3)"
  exit 2
fi

check_cluster() {
  local running
  running="$(docker compose -f "$COMPOSE_FILE" ps --services --status running || true)"

  local required=("ray-head" "ray-worker-1" "ray-worker-2" "minio")
  local missing=()
  for svc in "${required[@]}"; do
    if ! grep -qx "$svc" <<<"$running"; then
      missing+=("$svc")
    fi
  done

  if ((${#missing[@]} > 0)); then
    echo "ERROR: Ray Docker cluster is not fully running (checked via docker compose ps)."
    echo "  Missing services: ${missing[*]}"
    if [[ -n "$running" ]]; then
      echo "  Running services:"
      echo "$running" | sed 's/^/    - /'
    else
      echo "  Running services: none"
    fi
    echo "  Start it with: docker compose -f $COMPOSE_FILE up -d --build"
    exit 2
  fi
}

usage() {
  echo "Usage: $0 [--quick] [--iterations N]"
}

quick_flags=()
iterations_flags=()
while (($# > 0)); do
  case "$1" in
    --quick)
      quick_flags=("--quick")
      shift
      ;;
    --iterations)
      if (($# < 2)); then
        echo "ERROR: --iterations requires a numeric value"
        usage
        exit 2
      fi
      if ! [[ "$2" =~ ^[0-9]+$ ]]; then
        echo "ERROR: invalid --iterations value: $2"
        usage
        exit 2
      fi
      iterations_flags=("--iterations" "$2")
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1"
      usage
      exit 2
      ;;
  esac
done

check_cluster

scripts=(
  "bench_ray_startup.py"
  "bench_ray_preload.py"
  "bench_ray_proxy.py"
  "bench_ray_e2e.py"
)

for script in "${scripts[@]}"; do
  echo ""
  echo "================================================================"
  echo "Running $script ${quick_flags[*]} ${iterations_flags[*]}"
  echo "================================================================"
  "$PYTHON_BIN" "$ROOT_DIR/tests/benchmarks/$script" "${quick_flags[@]}" "${iterations_flags[@]}"
done

echo ""
echo "================================================================"
echo "Ray Benchmark Summary"
echo "================================================================"

"$PYTHON_BIN" - "$RESULTS_DIR" <<'PY'
from __future__ import annotations

import json
from pathlib import Path
import sys

results_dir = Path(sys.argv[1])


def load(name: str) -> dict:
    path = results_dir / name
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


startup = load("ray_startup_latest.json")
preload = load("ray_preload_latest.json")
proxy = load("ray_proxy_latest.json")
e2e = load("ray_e2e_latest.json")

startup_overhead = startup.get("results", {}).get("overhead", {})
preload_ols = preload.get("results", {}).get("ols", {})
proxy_rows = proxy.get("results", {}).get("rows", [])
e2e_rows = e2e.get("results", {}).get("rows", [])

proxy_avg = 0.0
if proxy_rows:
    proxy_avg = sum(float(r.get("overhead_percent", 0.0)) for r in proxy_rows) / len(proxy_rows)

e2e_avg = 0.0
if e2e_rows:
    e2e_avg = sum(float(r.get("overhead_percent", 0.0)) for r in e2e_rows) / len(e2e_rows)

rows = [
    (
        "Startup warm overhead",
        f"{float(startup_overhead.get('warm_seconds', 0.0)):+.3f}s",
        f"{float(startup_overhead.get('warm_percent', 0.0)):+.1f}%",
    ),
    (
        "Preload per-open",
        f"{float(preload_ols.get('slope_us_per_open', 0.0)):.3f} us",
        f"startup={float(preload_ols.get('intercept_ms', 0.0)):.3f} ms",
    ),
    (
        "Proxy avg overhead",
        "across op/size matrix",
        f"{proxy_avg:+.1f}%",
    ),
    (
        "E2E avg overhead",
        "across task/io matrix",
        f"{e2e_avg:+.1f}%",
    ),
]

widths = [max(len(r[i]) for r in rows + [("Metric", "Value", "Notes")]) for i in range(3)]

print(f"{'Metric'.ljust(widths[0])}  {'Value'.ljust(widths[1])}  {'Notes'.ljust(widths[2])}")
print(f"{'-' * widths[0]}  {'-' * widths[1]}  {'-' * widths[2]}")
for metric, value, notes in rows:
    print(f"{metric.ljust(widths[0])}  {value.ljust(widths[1])}  {notes.ljust(widths[2])}")
PY
