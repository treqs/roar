"""Unit tests for tracer report loading and normalization."""

import json
from pathlib import Path

import msgpack

from roar.execution.provenance.data_loader import DataLoaderService


def _write_msgpack(path: Path, payload: dict) -> None:
    path.write_bytes(msgpack.packb(payload, use_bin_type=True))


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload))


class TestDataLoaderService:
    def test_loads_json_report_when_tracer_writes_json(self, tmp_path: Path) -> None:
        report = {
            "version": 1,
            "tracer_mode": "ptrace",
            "opened_files": ["/tmp/a.txt"],
            "read_files": ["/tmp/a.txt"],
            "written_files": ["/tmp/b.txt"],
            "processes": [{"pid": 1, "parent_pid": None, "command": ["python", "train.py"]}],
            "start_time": 1.0,
            "end_time": 2.5,
        }
        report_path = tmp_path / "trace.msgpack"
        _write_json(report_path, report)

        data = DataLoaderService().load_tracer_data(str(report_path))

        assert data.opened_files == ["/tmp/a.txt"]
        assert data.read_files == ["/tmp/a.txt"]
        assert data.written_files == ["/tmp/b.txt"]
        assert data.tracer_mode == "ptrace"
        assert data.version == 1

    def test_loads_legacy_ptrace_report(self, tmp_path: Path) -> None:
        report = {
            "opened_files": ["/tmp/a.txt", "/tmp/a.txt"],
            "read_files": ["/tmp/a.txt"],
            "written_files": ["/tmp/b.txt"],
            "processes": [{"pid": 1, "parent_pid": None, "command": ["python"]}],
            "start_time": 1.0,
            "end_time": 2.5,
        }
        report_path = tmp_path / "trace.msgpack"
        _write_msgpack(report_path, report)

        data = DataLoaderService().load_tracer_data(str(report_path))

        # Legacy arrays are preserved (and deduplicated by the model validator).
        assert data.opened_files == ["/tmp/a.txt"]
        assert data.read_files == ["/tmp/a.txt"]
        assert data.written_files == ["/tmp/b.txt"]
        # Synthetic file records are still generated for downstream consistency.
        assert {f["path"] for f in data.files} == {"/tmp/a.txt", "/tmp/b.txt"}
        assert data.version == 1
        assert data.tracer_mode == "ptrace"
        assert data.events_dropped == 0

    def test_loads_ebpf_report_and_derives_aggregates(self, tmp_path: Path) -> None:
        report = {
            "version": 1,
            "tracer_mode": "ebpf",
            "processes": [{"pid": 123, "parent_pid": None, "command": ["python", "train.py"]}],
            "files": [
                {"path": "/repo/input.csv", "read": True, "written": False},
                {"path": "/repo/output.csv", "read": False, "written": True},
                {"path": "/repo/lock.file", "read": False, "written": False},
            ],
            "start_time": 10.0,
            "end_time": 12.0,
            "events_dropped": 3,
        }
        report_path = tmp_path / "trace.msgpack"
        _write_msgpack(report_path, report)

        data = DataLoaderService().load_tracer_data(str(report_path))

        assert data.tracer_mode == "ebpf"
        assert data.version == 1
        assert data.events_dropped == 3
        # All files are considered opened when explicit opened_files are absent.
        assert sorted(data.opened_files) == sorted(
            ["/repo/input.csv", "/repo/output.csv", "/repo/lock.file"]
        )
        assert data.read_files == ["/repo/input.csv"]
        assert data.written_files == ["/repo/output.csv"]

    def test_explicit_legacy_arrays_take_precedence_over_files(self, tmp_path: Path) -> None:
        report = {
            "opened_files": ["/legacy/opened.txt"],
            "read_files": ["/legacy/read.txt"],
            "written_files": ["/legacy/written.txt"],
            "files": [
                {"path": "/from/files.txt", "read": True, "written": True},
            ],
            "processes": [],
            "start_time": 1.0,
            "end_time": 1.1,
        }
        report_path = tmp_path / "trace.msgpack"
        _write_msgpack(report_path, report)

        data = DataLoaderService().load_tracer_data(str(report_path))

        assert data.opened_files == ["/legacy/opened.txt"]
        assert data.read_files == ["/legacy/read.txt"]
        assert data.written_files == ["/legacy/written.txt"]
        # Raw files are still retained for callers that need richer info.
        assert data.files == [{"path": "/from/files.txt", "read": True, "written": True}]

    def test_preserves_thread_aware_file_contract_fields(self, tmp_path: Path) -> None:
        report = {
            "version": 1,
            "tracer_mode": "preload",
            "files": [
                {
                    "path": "/repo/native.txt",
                    "read": True,
                    "written": True,
                    "read_threads": [101, 202],
                    "written_threads": [202],
                }
            ],
            "processes": [],
            "start_time": 1.0,
            "end_time": 2.0,
        }
        report_path = tmp_path / "trace.msgpack"
        _write_msgpack(report_path, report)

        data = DataLoaderService().load_tracer_data(str(report_path))

        assert data.files == [
            {
                "path": "/repo/native.txt",
                "read": True,
                "written": True,
                "read_threads": [101, 202],
                "written_threads": [202],
            }
        ]


# ---------------------------------------------------------------------------
# load_python_data — the inject-log reader side
# ---------------------------------------------------------------------------


class TestLoadPythonData:
    def test_missing_log_is_distinct_from_complete_empty_capture(self, tmp_path: Path) -> None:
        missing = DataLoaderService().load_python_data(None)
        assert missing.capture_status == "missing"

        log_path = tmp_path / "inject-log.json"
        _write_json(log_path, {})
        complete = DataLoaderService().load_python_data(str(log_path))
        assert complete.capture_status == "complete"

    def test_invalid_log_is_reported(self, tmp_path: Path) -> None:
        log_path = tmp_path / "inject-log.json"
        log_path.write_text("{not-json", encoding="utf-8")

        assert DataLoaderService().load_python_data(str(log_path)).capture_status == "invalid"

    def test_python_identity_keys_flow_through(self, tmp_path: Path) -> None:
        """python_version / python_implementation make it from JSON into the model."""
        log_path = tmp_path / "inject-log.json"
        _write_json(
            log_path,
            {
                "opened_files": [],
                "imported_modules": [],
                "env_reads": {},
                "modules_files": [],
                "roar_inject_dir": str(tmp_path),
                "shared_libs": [],
                "sys_prefix": "/opt/py312",
                "sys_base_prefix": "/opt/py312",
                "installed_packages": {},
                "used_packages": {},
                "python_version": "3.12.3",
                "python_implementation": "CPython",
            },
        )

        data = DataLoaderService().load_python_data(str(log_path))

        assert data.python_version == "3.12.3"
        assert data.python_implementation == "CPython"

    def test_python_identity_keys_default_to_empty_for_older_logs(self, tmp_path: Path) -> None:
        """An inject log without the new keys loads cleanly (back-compat)."""
        log_path = tmp_path / "inject-log.json"
        _write_json(
            log_path,
            {
                "opened_files": [],
                "imported_modules": [],
                "env_reads": {},
                "modules_files": [],
                "roar_inject_dir": str(tmp_path),
                "shared_libs": [],
                "sys_prefix": "/opt/py312",
                "sys_base_prefix": "/opt/py312",
                "installed_packages": {},
                "used_packages": {},
            },
        )

        data = DataLoaderService().load_python_data(str(log_path))

        assert data.python_version == ""
        assert data.python_implementation == ""

    def test_writer_reader_roundtrip_carries_python_identity(self, tmp_path: Path) -> None:
        """End-to-end seam: the real tracker writes the JSON, the real loader reads it.

        Would have caught both halves of the friction-journal bug — if the
        writer drops the key or the reader doesn't extract it, the loaded
        model's python_version is empty.
        """
        from roar.execution.runtime.inject.tracker import (
            RuntimeInjectionTracker,
            merge_inject_logs,
        )

        log_path = tmp_path / "inject-log.json"

        class _FakeController:
            def handle_import(self, module_name, module) -> None:
                return None

        tracker = RuntimeInjectionTracker(
            {"ROAR_LOG_FILE": str(log_path)},
            _FakeController(),
            log_file=str(log_path),
            inject_dir=str(tmp_path / "inject"),
        )
        tracker.write_log()
        merge_inject_logs(str(log_path))  # write_log writes a per-PID shard; merge -> canonical

        data = DataLoaderService().load_python_data(str(log_path))

        # In-process round-trip: this test's interpreter wrote the JSON, so
        # the version is the test runner's — but the *seam* is what we care
        # about, not the value.
        assert data.python_version
        assert data.python_version.count(".") >= 2  # e.g. "3.12.3"
        assert data.python_implementation
