from __future__ import annotations

from roar.core.models.provenance import PythonInjectData, TracerData
from roar.services.execution.provenance import file_filter
from roar.services.execution.provenance.file_filter import FileFilterService


def _filter_config(ignore_tmp_files: bool = False) -> dict[str, dict[str, bool]]:
    return {
        "filters": {
            "ignore_system_reads": False,
            "ignore_package_reads": True,
            "ignore_torch_cache": False,
            "ignore_tmp_files": ignore_tmp_files,
        }
    }


def test_filter_files_excludes_editable_install_reads(monkeypatch) -> None:
    monkeypatch.setattr(
        file_filter,
        "_get_editable_install_dirs",
        lambda: frozenset({"/home/user/dev/roar/"}),
    )

    tracer_data = TracerData(
        opened_files=[
            "/home/user/dev/roar/roar/__init__.py",
            "/workspace/project/main.py",
        ],
        read_files=[
            "/home/user/dev/roar/roar/__init__.py",
            "/workspace/project/main.py",
        ],
        written_files=[],
    )
    python_data = PythonInjectData(sys_prefix="", sys_base_prefix="", roar_inject_dir="")

    filtered = FileFilterService().filter_files(tracer_data, python_data, _filter_config())

    assert "/home/user/dev/roar/roar/__init__.py" not in filtered.read_files
    assert "/home/user/dev/roar/roar/__init__.py" not in filtered.opened_files
    assert "/workspace/project/main.py" in filtered.read_files


def test_filter_files_excludes_worker_bundle_paths_even_without_tmp_filter(monkeypatch) -> None:
    monkeypatch.setattr(file_filter, "_get_editable_install_dirs", lambda: frozenset())

    worker_path = "/tmp/roar-worker-env-job123/roar/worker.py"
    tracer_data = TracerData(
        opened_files=[worker_path],
        read_files=[worker_path],
        written_files=[worker_path],
    )
    python_data = PythonInjectData(sys_prefix="", sys_base_prefix="", roar_inject_dir="")

    filtered = FileFilterService().filter_files(
        tracer_data,
        python_data,
        _filter_config(ignore_tmp_files=False),
    )

    assert worker_path not in filtered.opened_files
    assert worker_path not in filtered.read_files
    assert worker_path not in filtered.written_files


def test_filter_files_keeps_regular_tmp_writes_when_tmp_filter_disabled(monkeypatch) -> None:
    monkeypatch.setattr(file_filter, "_get_editable_install_dirs", lambda: frozenset())

    user_tmp_output = "/tmp/user-output.json"
    tracer_data = TracerData(
        opened_files=[],
        read_files=[],
        written_files=[user_tmp_output],
    )
    python_data = PythonInjectData(sys_prefix="", sys_base_prefix="", roar_inject_dir="")

    filtered = FileFilterService().filter_files(
        tracer_data,
        python_data,
        _filter_config(ignore_tmp_files=False),
    )

    assert user_tmp_output in filtered.written_files
