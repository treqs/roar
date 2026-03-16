from __future__ import annotations

from pathlib import Path

from roar.core.models.provenance import PythonInjectData, TracerData
from roar.execution.provenance import file_filter
from roar.execution.provenance.file_filter import FileFilterService


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


def test_filter_files_excludes_roar_db_reads(monkeypatch) -> None:
    monkeypatch.setattr(file_filter, "_get_editable_install_dirs", lambda: frozenset())

    roar_db_path = "/workspace/project/.roar/roar.db"
    tracer_data = TracerData(
        opened_files=[roar_db_path],
        read_files=[roar_db_path],
        written_files=[],
    )
    python_data = PythonInjectData(sys_prefix="", sys_base_prefix="", roar_inject_dir="")

    filtered = FileFilterService().filter_files(tracer_data, python_data, _filter_config())

    assert roar_db_path not in filtered.opened_files
    assert roar_db_path not in filtered.read_files


def test_filter_files_excludes_roar_config_reads(monkeypatch) -> None:
    monkeypatch.setattr(file_filter, "_get_editable_install_dirs", lambda: frozenset())

    roar_config_path = "/workspace/project/.roar/config.toml"
    tracer_data = TracerData(
        opened_files=[roar_config_path],
        read_files=[roar_config_path],
        written_files=[],
    )
    python_data = PythonInjectData(sys_prefix="", sys_base_prefix="", roar_inject_dir="")

    filtered = FileFilterService().filter_files(tracer_data, python_data, _filter_config())

    assert roar_config_path not in filtered.opened_files
    assert roar_config_path not in filtered.read_files


def test_filter_files_excludes_git_head_reads(monkeypatch) -> None:
    monkeypatch.setattr(file_filter, "_get_editable_install_dirs", lambda: frozenset())

    git_head_path = "/workspace/project/.git/HEAD"
    tracer_data = TracerData(
        opened_files=[git_head_path],
        read_files=[git_head_path],
        written_files=[],
    )
    python_data = PythonInjectData(sys_prefix="", sys_base_prefix="", roar_inject_dir="")

    filtered = FileFilterService().filter_files(tracer_data, python_data, _filter_config())

    assert git_head_path not in filtered.opened_files
    assert git_head_path not in filtered.read_files


def test_filter_files_excludes_expanded_home_gitconfig_reads(monkeypatch) -> None:
    monkeypatch.setattr(file_filter, "_get_editable_install_dirs", lambda: frozenset())

    gitconfig_path = str(Path("~/.gitconfig").expanduser())
    tracer_data = TracerData(
        opened_files=[gitconfig_path],
        read_files=[gitconfig_path],
        written_files=[],
    )
    python_data = PythonInjectData(sys_prefix="", sys_base_prefix="", roar_inject_dir="")

    filtered = FileFilterService().filter_files(tracer_data, python_data, _filter_config())

    assert gitconfig_path not in filtered.opened_files
    assert gitconfig_path not in filtered.read_files


def test_filter_files_keeps_user_data_reads(monkeypatch) -> None:
    monkeypatch.setattr(file_filter, "_get_editable_install_dirs", lambda: frozenset())

    user_data_path = "/data/myfile.csv"
    tracer_data = TracerData(
        opened_files=[user_data_path],
        read_files=[user_data_path],
        written_files=[],
    )
    python_data = PythonInjectData(sys_prefix="", sys_base_prefix="", roar_inject_dir="")

    filtered = FileFilterService().filter_files(tracer_data, python_data, _filter_config())

    assert user_data_path in filtered.opened_files
    assert user_data_path in filtered.read_files
