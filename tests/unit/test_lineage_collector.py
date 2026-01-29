"""Unit tests for lineage collector service."""

from roar.services.upload.lineage_collector import LineageCollector, compute_io_signature


class TestComputeIoSignature:
    """Tests for compute_io_signature function."""

    def test_empty_io_uses_job_uid(self):
        """Jobs with empty I/O should use job_uid as signature."""
        job = {"id": 1, "job_uid": "abc123", "_input_hashes": [], "_output_hashes": []}
        sig = compute_io_signature(job)
        assert sig == "unique:abc123"

    def test_empty_io_falls_back_to_id(self):
        """Jobs with empty I/O and no job_uid should fall back to id."""
        job = {"id": 42, "_input_hashes": [], "_output_hashes": []}
        sig = compute_io_signature(job)
        assert sig == "unique:42"

    def test_jobs_with_io_use_hash_signature(self):
        """Jobs with I/O should use hash-based signature."""
        job = {"id": 1, "job_uid": "abc", "_input_hashes": ["hash1"], "_output_hashes": ["hash2"]}
        sig = compute_io_signature(job)
        assert sig == "('hash1',)|('hash2',)"

    def test_signature_sorts_hashes(self):
        """Hashes should be sorted for consistent signatures."""
        job = {"id": 1, "_input_hashes": ["z", "a", "m"], "_output_hashes": ["b", "a"]}
        sig = compute_io_signature(job)
        assert sig == "('a', 'm', 'z')|('a', 'b')"


class TestDeduplicateReruns:
    """Tests for _deduplicate_reruns method."""

    def test_preserves_build_jobs_with_empty_io(self):
        """Build jobs with empty I/O should not be deduplicated."""
        collector = LineageCollector()
        jobs = [
            {
                "id": 1,
                "job_uid": "b1",
                "job_type": "build",
                "timestamp": 1.0,
                "_input_hashes": [],
                "_output_hashes": [],
            },
            {
                "id": 2,
                "job_uid": "b2",
                "job_type": "build",
                "timestamp": 2.0,
                "_input_hashes": [],
                "_output_hashes": [],
            },
            {
                "id": 3,
                "job_uid": "b3",
                "job_type": "build",
                "timestamp": 3.0,
                "_input_hashes": [],
                "_output_hashes": [],
            },
        ]
        result = collector._deduplicate_reruns(jobs)
        assert len(result) == 3

    def test_still_deduplicates_run_jobs_with_io(self):
        """Run jobs with same I/O should still be deduplicated."""
        collector = LineageCollector()
        jobs = [
            {
                "id": 1,
                "job_uid": "r1",
                "timestamp": 1.0,
                "_input_hashes": ["abc"],
                "_output_hashes": ["def"],
            },
            {
                "id": 2,
                "job_uid": "r2",
                "timestamp": 2.0,
                "_input_hashes": ["abc"],
                "_output_hashes": ["def"],
            },
        ]
        result = collector._deduplicate_reruns(jobs)
        assert len(result) == 1
        assert result[0]["job_uid"] == "r2"  # Latest kept

    def test_keeps_jobs_with_different_io(self):
        """Jobs with different I/O should not be deduplicated."""
        collector = LineageCollector()
        jobs = [
            {
                "id": 1,
                "job_uid": "r1",
                "timestamp": 1.0,
                "_input_hashes": ["abc"],
                "_output_hashes": ["def"],
            },
            {
                "id": 2,
                "job_uid": "r2",
                "timestamp": 2.0,
                "_input_hashes": ["xyz"],
                "_output_hashes": ["uvw"],
            },
        ]
        result = collector._deduplicate_reruns(jobs)
        assert len(result) == 2

    def test_results_sorted_by_timestamp(self):
        """Deduplicated results should be sorted by timestamp."""
        collector = LineageCollector()
        jobs = [
            {
                "id": 3,
                "job_uid": "j3",
                "timestamp": 3.0,
                "_input_hashes": ["c"],
                "_output_hashes": ["c"],
            },
            {
                "id": 1,
                "job_uid": "j1",
                "timestamp": 1.0,
                "_input_hashes": ["a"],
                "_output_hashes": ["a"],
            },
            {
                "id": 2,
                "job_uid": "j2",
                "timestamp": 2.0,
                "_input_hashes": ["b"],
                "_output_hashes": ["b"],
            },
        ]
        result = collector._deduplicate_reruns(jobs)
        assert [j["id"] for j in result] == [1, 2, 3]
