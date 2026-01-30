"""
Integration test to reproduce PayloadTooLargeError against GLaaS dev server.

Requires: GLaaS dev server running on localhost:3001
Run with: pytest tests/unit/test_large_payload_error.py -v -m integration
"""

import json
from unittest.mock import MagicMock

import pytest

from roar.glaas_client import GlaasClient
from roar.services.registration.artifact import ArtifactRegistrationService
from roar.services.registration.job import (
    JobRegistrationService,
    _batch_artifacts,
)

DEV_SERVER_URL = "http://localhost:3001"


class TestSizeBasedBatching:
    """Unit tests for size-based artifact batching."""

    def test_batch_by_size_small_artifacts_single_batch(self):
        """Small artifacts fitting under limit should be in single batch."""
        from roar.services.registration.artifact import _batch_by_size

        artifacts = [
            {"hashes": [{"algorithm": "blake3", "digest": f"small{i:060d}"}], "size": 100}
            for i in range(100)
        ]
        # 100 small artifacts (~140 bytes each = ~14KB) should fit in one 90KB batch
        batches = _batch_by_size(artifacts, max_bytes=90 * 1024)
        assert len(batches) == 1
        assert len(batches[0]) == 100

    def test_batch_by_size_splits_at_limit(self):
        """Artifacts should be split when approaching size limit."""
        from roar.services.registration.artifact import _batch_by_size

        artifacts = [
            {
                "hashes": [{"algorithm": "blake3", "digest": f"test{i:060d}"}],
                "size": 1000,
                "source_url": f"https://example.com/path/to/artifact/{i}/data.zarr",
            }
            for i in range(600)
        ]

        batches = _batch_by_size(artifacts, max_bytes=90 * 1024)

        # Should create multiple batches
        assert len(batches) > 1

        # Each batch should be under the limit
        for batch in batches:
            batch_size = len(json.dumps(batch))
            assert batch_size <= 90 * 1024

    def test_batch_by_size_empty_list(self):
        """Empty list should return empty list."""
        from roar.services.registration.artifact import _batch_by_size

        assert _batch_by_size([]) == []

    def test_batch_by_size_preserves_order(self):
        """Batching should preserve original order of artifacts."""
        from roar.services.registration.artifact import _batch_by_size

        artifacts = [
            {"hashes": [{"algorithm": "blake3", "digest": f"order{i:060d}"}], "size": i, "index": i}
            for i in range(200)
        ]
        batches = _batch_by_size(artifacts, max_bytes=10 * 1024)  # Force multiple batches
        flattened = [item for batch in batches for item in batch]
        for i, item in enumerate(flattened):
            assert item["index"] == i

    def test_batch_by_size_oversized_single_artifact(self):
        """Single artifact exceeding limit should be in its own batch."""
        from roar.services.registration.artifact import _batch_by_size

        # Create one huge artifact that exceeds the limit
        huge_artifact = {
            "hashes": [{"algorithm": "blake3", "digest": "x" * 64}],
            "size": 1000,
            "metadata": json.dumps({"data": "x" * 100000}),  # ~100KB just in metadata
        }
        small_artifact = {
            "hashes": [{"algorithm": "blake3", "digest": "y" * 64}],
            "size": 100,
        }

        artifacts = [small_artifact, huge_artifact, small_artifact]
        batches = _batch_by_size(artifacts, max_bytes=1024)  # 1KB limit

        # Should have 3 batches: small, huge (alone), small
        assert len(batches) == 3
        assert batches[1] == [huge_artifact]


class TestBatchArtifacts:
    """Unit tests for artifact batching functionality."""

    def test_batch_artifacts_empty_list(self):
        """Empty list returns empty list."""
        assert _batch_artifacts([], 100) == []

    def test_batch_artifacts_smaller_than_batch_size(self):
        """List smaller than batch size returns single batch."""
        artifacts = [{"hash": f"h{i}"} for i in range(50)]
        batches = _batch_artifacts(artifacts, 100)
        assert len(batches) == 1
        assert len(batches[0]) == 50

    def test_batch_artifacts_exact_batch_size(self):
        """List exactly at batch size returns single batch."""
        artifacts = [{"hash": f"h{i}"} for i in range(100)]
        batches = _batch_artifacts(artifacts, 100)
        assert len(batches) == 1
        assert len(batches[0]) == 100

    def test_batch_artifacts_multiple_batches(self):
        """Large list is split into multiple batches."""
        artifacts = [{"hash": f"h{i}"} for i in range(250)]
        batches = _batch_artifacts(artifacts, 100)
        assert len(batches) == 3
        assert len(batches[0]) == 100
        assert len(batches[1]) == 100
        assert len(batches[2]) == 50

    def test_batch_artifacts_preserves_order(self):
        """Batching preserves original order of artifacts."""
        artifacts = [{"hash": f"h{i}", "index": i} for i in range(250)]
        batches = _batch_artifacts(artifacts, 100)
        flattened = [item for batch in batches for item in batch]
        for i, item in enumerate(flattened):
            assert item["index"] == i


class TestBatchingIntegration:
    """Test that link_job_artifacts uses batching correctly."""

    def test_link_job_artifacts_batches_large_inputs(self):
        """Verify large input lists are batched into multiple API calls."""
        mock_client = MagicMock()
        mock_client.register_job_inputs.return_value = ({"inputs_linked": 100}, None)
        mock_client.register_job_outputs.return_value = ({"outputs_linked": 100}, None)

        service = JobRegistrationService(client=mock_client)

        # 500 inputs should result in 5 batches of 100
        inputs = [{"hash": f"h{i:032d}", "path": f"/path/{i}"} for i in range(500)]

        result = service.link_job_artifacts(
            session_hash="test_session",
            job_uid="test_job",
            inputs=inputs,
            outputs=[],
        )

        assert result.success is True
        assert result.inputs_linked == 500
        # Should have made 5 calls (500 / 100 = 5 batches)
        assert mock_client.register_job_inputs.call_count == 5

    def test_link_job_artifacts_batches_large_outputs(self):
        """Verify large output lists are batched into multiple API calls."""
        mock_client = MagicMock()
        mock_client.register_job_inputs.return_value = ({"inputs_linked": 100}, None)
        # Return counts matching actual batch sizes: 100, 100, 100, 50
        mock_client.register_job_outputs.side_effect = [
            ({"outputs_linked": 100}, None),
            ({"outputs_linked": 100}, None),
            ({"outputs_linked": 100}, None),
            ({"outputs_linked": 50}, None),
        ]

        service = JobRegistrationService(client=mock_client)

        # 350 outputs should result in 4 batches (100, 100, 100, 50)
        outputs = [{"hash": f"h{i:032d}", "path": f"/path/{i}"} for i in range(350)]

        result = service.link_job_artifacts(
            session_hash="test_session",
            job_uid="test_job",
            inputs=[],
            outputs=outputs,
        )

        assert result.success is True
        assert result.outputs_linked == 350
        # Should have made 4 calls
        assert mock_client.register_job_outputs.call_count == 4

    def test_link_job_artifacts_stops_on_error(self):
        """Verify batching stops on first error and reports partial progress."""
        mock_client = MagicMock()
        # First two batches succeed, third fails
        mock_client.register_job_inputs.side_effect = [
            ({"inputs_linked": 100}, None),
            ({"inputs_linked": 100}, None),
            (None, "Server error"),
        ]

        service = JobRegistrationService(client=mock_client)

        inputs = [{"hash": f"h{i:032d}", "path": f"/path/{i}"} for i in range(500)]

        result = service.link_job_artifacts(
            session_hash="test_session",
            job_uid="test_job",
            inputs=inputs,
            outputs=[],
        )

        assert result.success is False
        assert result.inputs_linked == 200  # Only first two batches succeeded
        assert "Server error" in result.error
        # Should have stopped after 3 calls (not 5)
        assert mock_client.register_job_inputs.call_count == 3


class TestLargePayloadError:
    """Test that large payloads properly return errors against real server."""

    @pytest.fixture
    def client(self):
        """Create client pointing to dev server."""
        return GlaasClient(base_url=DEV_SERVER_URL)

    @pytest.mark.integration
    def test_register_job_inputs_payload_too_large(self, client):
        """Test that registering many inputs triggers payload error on dev server."""
        service = JobRegistrationService(client=client)

        # Create 5000 input artifacts with metadata to exceed server body-parser limit
        # Each artifact is ~200 bytes, so 5000 * 200 = ~1MB which should exceed most limits
        inputs = [
            {
                "hash": f"hash{i:032d}",
                "path": f"/workspace/data/very/long/path/to/file/number/{i}.zarr",
                "metadata": {"description": f"This is artifact number {i} with extra padding" * 3},
            }
            for i in range(5000)
        ]

        result = service.link_job_artifacts(
            session_hash="test_session_hash",
            job_uid="test_job_uid",
            inputs=inputs,
            outputs=[],
        )

        # With batching, this should succeed (or fail for other reasons like auth)
        # The key is it should NOT fail with payload too large errors
        if not result.success and result.error:
            error_lower = result.error.lower()
            # Should NOT be a payload size error - batching should prevent that
            assert "413" not in result.error, (
                f"Batching failed to prevent payload error: {result.error}"
            )
            assert "large" not in error_lower or "payload" not in error_lower, (
                f"Batching failed to prevent payload error: {result.error}"
            )


class TestArtifactBatchingBug:
    """Test that verifies artifact batching fix against live dev server.

    The GLaaS server has a ~100KB body-parser limit. Without batching:
    - 550 artifacts (~99KB) succeeds
    - 555 artifacts (~100KB) fails with HTTP 500

    The fix batches artifacts into groups of 100, ensuring each request stays
    well under the limit.
    """

    @pytest.fixture
    def client(self):
        """Create client pointing to dev server."""
        return GlaasClient(base_url=DEV_SERVER_URL)

    @pytest.mark.integration
    def test_register_batch_600_artifacts_exceeds_payload_limit(self, client):
        """Verify 600 artifacts can be registered despite exceeding single-request payload limit.

        Without batching, 600 artifacts (~111KB) would fail with HTTP 500 because
        the server's body-parser limit is ~100KB.

        With batching (6 batches of 100), each request is ~19KB and succeeds.

        This test reproduces the PayloadTooLargeError issue from diffusion-example-db.
        """
        service = ArtifactRegistrationService(client=client)

        # 600 artifacts at ~185 bytes each = ~111KB total payload
        # Server limit is ~100KB, so this MUST be batched to succeed
        artifacts = [
            {
                "hashes": [{"algorithm": "blake3", "digest": f"testbatch600art{i:048d}"}],
                "size": 2000,
                "source_type": None,
            }
            for i in range(600)
        ]

        result = service.register_batch(artifacts, "test_session_600")

        # With batching, should succeed (or fail for non-payload reasons like missing session)
        # The critical assertion: must NOT fail with HTTP 500 from payload size
        for error in result.errors:
            assert "HTTP 500" not in error, (
                f"Batching failed - payload exceeded server limit: {error}"
            )

    @pytest.mark.integration
    def test_register_batch_1000_artifacts_well_over_limit(self, client):
        """Verify 1000 artifacts (~195KB) succeeds with batching.

        This payload is nearly 2x the server's ~100KB limit, requiring
        10 batches of 100 artifacts each.
        """
        service = ArtifactRegistrationService(client=client)

        artifacts = [
            {
                "hashes": [{"algorithm": "blake3", "digest": f"testbatch1000a{i:049d}"}],
                "size": 5000,
                "source_type": None,
            }
            for i in range(1000)
        ]

        result = service.register_batch(artifacts, "test_session_1000")

        # Must NOT fail with HTTP 500 from payload size
        for error in result.errors:
            assert "HTTP 500" not in error, (
                f"Batching failed - payload exceeded server limit: {error}"
            )
