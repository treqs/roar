#!/usr/bin/env bash
set -euo pipefail

# In-pod wrapper for the Tier-1 smoke test.
#
# This is the hand-rolled stand-in for the future `roar k8s` runtime wrapper:
# stage the roar runtime from the mounted wheel, trace the (roar-unaware)
# training command, then export the recorded job as an execution fragment and
# stream it to GLaaS via the fragment-session credentials in the environment.

echo "[roar-wrapper] installing roar-cli from /wheels"
# The wheel comes from the mounted dist/; its dependencies (blake3, click,
# ...) still come from the index. A fully hermetic runtime is the job of the
# future roar-runtime image (see the k8s integration design doc).
wheel="$(ls /wheels/roar_cli-*.whl | sort -V | tail -1)"
pip install --quiet "$wheel"
roar --version

mkdir -p /work/project
cd /work/project

printf 'x,y\n1.0,2.0\n2.0,3.9\n3.0,6.1\n4.0,8.2\n' >dataset.csv

roar init -n
roar run --tracer "${ROAR_K8S_TRACER:-preload}" python /workload/train.py dataset.csv

echo "[roar-wrapper] exporting and streaming lineage fragments"
python /workload/emit_fragments.py
