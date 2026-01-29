"""Live GLaaS integration tests.

These tests require a running GLaaS server at http://localhost:3001 (or GLAAS_URL env).
They are marked with @pytest.mark.live_glaas and skipped by default.

To run these tests:
    1. Start glaas-api: cd /home/trevor/dev/glaas-api && npm run dev
    2. Run: pytest tests/live_glaas -v -m live_glaas
"""
