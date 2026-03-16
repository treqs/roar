#!/usr/bin/env bash
# Prepare the live GLaaS test environment.
# Safe to run repeatedly (idempotent).
set -euo pipefail

GLAAS_URL="${GLAAS_URL:-http://localhost:3001}"
GLAAS_API_DIR="${GLAAS_API_DIR:-/home/trevor/dev/glaas-api}"
DB_URL="${DATABASE_URL:-postgresql://postgres:postgres@localhost:5434/glaas_dev}"

echo "=== GLaaS Live Test Setup ==="

# 1. Health check
echo -n "Checking GLaaS health... "
curl -sf "$GLAAS_URL/api/v1/health" > /dev/null || { echo "FAIL — is glaas-api running?"; exit 1; }
echo "OK"

# 2. Seed test user with local SSH pubkey
echo "Seeding test user..."
DATABASE_URL="$DB_URL" npx --prefix "$GLAAS_API_DIR" tsx "$GLAAS_API_DIR/scripts/seed-test-user.ts"

echo ""
echo "=== Setup complete. Run tests with: ==="
echo "  cd /home/trevor/dev/roar"
echo "  GLAAS_URL=$GLAAS_URL .venv/bin/pytest tests/backends/ray/live/test_ray_register_live.py -v -m live_glaas --dist no"
