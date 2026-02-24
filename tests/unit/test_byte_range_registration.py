"""Tests for byte_ranges propagation through DTOs and registration flow."""

from unittest.mock import MagicMock

from roar.core.dto.registration import JobDTO, JobIODTO
from roar.services.registration.coordinator import RegistrationCoordinator


class TestJobIODTOByteRanges:
    def test_to_dict_with_byte_ranges(self):
        dto = JobIODTO(hash="abc123", path="/data/file.csv", byte_ranges=[[0, 999]])
        d = dto.to_dict()
        assert d == {"hash": "abc123", "path": "/data/file.csv", "byte_ranges": [[0, 999]]}

    def test_to_dict_without_byte_ranges(self):
        dto = JobIODTO(hash="abc123", path="/data/file.csv")
        d = dto.to_dict()
        assert d == {"hash": "abc123", "path": "/data/file.csv"}
        assert "byte_ranges" not in d

    def test_to_dict_with_none_byte_ranges(self):
        dto = JobIODTO(hash="abc123", path="/data/file.csv", byte_ranges=None)
        d = dto.to_dict()
        assert d == {"hash": "abc123", "path": "/data/file.csv"}
        assert "byte_ranges" not in d

    def test_to_dict_with_multiple_byte_ranges(self):
        dto = JobIODTO(hash="abc123", path="/data/file.csv", byte_ranges=[[0, 999], [2000, 2999]])
        d = dto.to_dict()
        assert d["byte_ranges"] == [[0, 999], [2000, 2999]]


class TestJobDTOFromDictByteRanges:
    def test_from_dict_with_byte_ranges(self):
        data = {
            "job_uid": "test1234",
            "command": "python train.py",
            "timestamp": 1000.0,
            "git_commit": "abc123",
            "git_branch": "main",
            "duration_seconds": 60.0,
            "exit_code": 0,
            "step_number": 1,
            "_inputs": [
                {"hash": "h1", "path": "/input.csv", "byte_ranges": [[0, 999]]},
            ],
            "_outputs": [
                {"hash": "h2", "path": "/output.csv"},
            ],
        }
        dto = JobDTO.from_dict(data)
        assert len(dto.inputs) == 1
        assert dto.inputs[0].byte_ranges == [[0, 999]]
        assert len(dto.outputs) == 1
        assert dto.outputs[0].byte_ranges is None

    def test_from_dict_without_byte_ranges(self):
        data = {
            "job_uid": "test1234",
            "command": "python train.py",
            "timestamp": 1000.0,
            "git_commit": "abc123",
            "git_branch": "main",
            "duration_seconds": 60.0,
            "exit_code": 0,
            "step_number": 1,
            "_inputs": [
                {"hash": "h1", "path": "/input.csv"},
            ],
        }
        dto = JobDTO.from_dict(data)
        assert len(dto.inputs) == 1
        assert dto.inputs[0].byte_ranges is None


class TestExtractIoListPreservesByteRanges:
    def test_extract_io_list_preserves_byte_ranges(self):
        coordinator = RegistrationCoordinator(logger=MagicMock())

        job = {
            "_inputs": [
                {"hash": "h1", "path": "/input.csv", "byte_ranges": [[0, 999]]},
                {"hash": "h2", "path": "/config.json"},
            ],
            "_input_hashes": ["h1", "h2"],
        }

        result = coordinator._extract_io_list(job, "_inputs", "_input_hashes")
        assert len(result) == 2
        assert result[0] == {"hash": "h1", "path": "/input.csv", "byte_ranges": [[0, 999]]}
        assert result[1] == {"hash": "h2", "path": "/config.json"}
        assert "byte_ranges" not in result[1]

    def test_extract_io_list_no_byte_ranges(self):
        coordinator = RegistrationCoordinator(logger=MagicMock())

        job = {
            "_inputs": [
                {"hash": "h1", "path": "/input.csv"},
            ],
            "_input_hashes": ["h1"],
        }

        result = coordinator._extract_io_list(job, "_inputs", "_input_hashes")
        assert len(result) == 1
        assert result[0] == {"hash": "h1", "path": "/input.csv"}
        assert "byte_ranges" not in result[0]

    def test_extract_io_list_accepts_hashes_only_structured_items(self):
        coordinator = RegistrationCoordinator(logger=MagicMock())

        job = {
            "_inputs": [
                {
                    "hashes": [
                        {"algorithm": "sha256", "digest": "a" * 64},
                        {"algorithm": "blake3", "digest": "b" * 64},
                    ],
                    "path": "/input.csv",
                    "byte_ranges": [[0, 999]],
                },
            ],
            "_input_hashes": [],
        }

        result = coordinator._extract_io_list(job, "_inputs", "_input_hashes")
        assert len(result) == 1
        assert result[0] == {
            "hashes": [
                {"algorithm": "sha256", "digest": "a" * 64},
                {"algorithm": "blake3", "digest": "b" * 64},
            ],
            "path": "/input.csv",
            "byte_ranges": [[0, 999]],
        }
