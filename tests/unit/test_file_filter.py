from __future__ import annotations

from pathlib import Path

from roar.core.models.provenance import PythonInjectData, TracerData
from roar.execution.provenance import file_filter
from roar.execution.provenance.file_filter import FileFilterService


def _filter_config(
    ignore_tmp_files: bool = False,
    ignore_library_caches: bool = False,
) -> dict[str, dict[str, bool]]:
    return {
        "filters": {
            "ignore_system_reads": False,
            "ignore_package_reads": True,
            "ignore_torch_cache": False,
            "ignore_library_caches": ignore_library_caches,
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


def test_preexisting_tmp_read_is_kept_even_with_tmp_filter_on(monkeypatch) -> None:
    """A /tmp file the job only *reads* pre-existed the run — a real (ephemeral)
    input — and must survive the default ignore_tmp_files filter."""
    monkeypatch.setattr(file_filter, "_get_editable_install_dirs", lambda: frozenset())

    preexisting_input = "/tmp/dataset.parquet"
    tracer_data = TracerData(
        opened_files=[preexisting_input],
        read_files=[preexisting_input],
        written_files=[],
    )
    python_data = PythonInjectData(sys_prefix="", sys_base_prefix="", roar_inject_dir="")

    filtered = FileFilterService().filter_files(
        tracer_data,
        python_data,
        _filter_config(ignore_tmp_files=True),
    )

    assert preexisting_input in filtered.read_files
    assert preexisting_input in filtered.opened_files


def test_tmp_intermediate_read_and_written_is_dropped(monkeypatch) -> None:
    """A /tmp file the job both writes and reads back is an endogenous
    intermediate (created under roar's watch) — drop it from reads as before."""
    monkeypatch.setattr(file_filter, "_get_editable_install_dirs", lambda: frozenset())

    intermediate = "/tmp/scratch.bin"
    tracer_data = TracerData(
        opened_files=[intermediate],
        read_files=[intermediate],
        written_files=[intermediate],
    )
    python_data = PythonInjectData(sys_prefix="", sys_base_prefix="", roar_inject_dir="")

    filtered = FileFilterService().filter_files(
        tracer_data,
        python_data,
        _filter_config(ignore_tmp_files=True),
    )

    assert intermediate not in filtered.read_files
    assert intermediate not in filtered.written_files


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


def test_filter_files_drops_huggingface_cache_when_library_caches_enabled(monkeypatch) -> None:
    """Reads + writes under ~/.cache/huggingface/ are dropped by ignore_library_caches.

    Covers both home-dir variants (/home/<user>/, /root/) since the matcher is
    a substring check.
    """
    monkeypatch.setattr(file_filter, "_get_editable_install_dirs", lambda: frozenset())

    hf_dataset_meta = (
        "/home/ubuntu/.cache/huggingface/datasets/openai___gsm8k/main/0.0.0/dataset_info.json"
    )
    hf_hub_blob = "/root/.cache/huggingface/hub/datasets--cais--mmlu/blobs/abcdef"
    hf_xet_chunk = (
        "/home/ubuntu/.cache/huggingface/xet/https___cas_serv-x_y_z/chunk-cache/-8/_AIAAAAD"
    )

    tracer_data = TracerData(
        opened_files=[hf_dataset_meta, hf_hub_blob, hf_xet_chunk],
        read_files=[hf_dataset_meta, hf_hub_blob],
        written_files=[hf_hub_blob, hf_xet_chunk],
    )
    python_data = PythonInjectData(sys_prefix="", sys_base_prefix="", roar_inject_dir="")

    filtered = FileFilterService().filter_files(
        tracer_data, python_data, _filter_config(ignore_library_caches=True)
    )

    for p in (hf_dataset_meta, hf_hub_blob, hf_xet_chunk):
        assert p not in filtered.opened_files
        assert p not in filtered.read_files
        assert p not in filtered.written_files
    assert filtered.counts.library_caches >= 3


def test_filter_files_drops_triton_jit_cache_when_library_caches_enabled(monkeypatch) -> None:
    """Triton's JIT kernel cache lives at ~/.triton/cache/ (NOT under ~/.cache/),
    so it needs its own pattern. torch.compile on GPU writes compiled .so
    launchers here every run — confirmed leaking on a nanochat A10 run.
    """
    monkeypatch.setattr(file_filter, "_get_editable_install_dirs", lambda: frozenset())

    triton_so = (
        "/home/ubuntu/.triton/cache/3HMPBPHQEJAVV3TKQCBPMFNMCUDE4S5Y624MJ2OCL5QA/"
        "__triton_launcher.cpython-310-x86_64-linux-gnu.so"
    )
    triton_cuda = "/root/.triton/cache/KJGBFCDPODDSTMI2D3ESJLJ6/cuda_utils.cpython-310.so"

    tracer_data = TracerData(
        opened_files=[triton_so, triton_cuda],
        read_files=[triton_so],
        written_files=[triton_so, triton_cuda],
    )
    python_data = PythonInjectData(sys_prefix="", sys_base_prefix="", roar_inject_dir="")

    filtered = FileFilterService().filter_files(
        tracer_data, python_data, _filter_config(ignore_library_caches=True)
    )

    for p in (triton_so, triton_cuda):
        assert p not in filtered.opened_files
        assert p not in filtered.read_files
        assert p not in filtered.written_files
    assert filtered.counts.library_caches >= 2


def test_filter_files_keeps_huggingface_cache_when_library_caches_disabled(monkeypatch) -> None:
    """Setting ignore_library_caches=False (the override) keeps the paths."""
    monkeypatch.setattr(file_filter, "_get_editable_install_dirs", lambda: frozenset())

    hf_path = "/home/ubuntu/.cache/huggingface/datasets/foo/info.json"
    tracer_data = TracerData(
        opened_files=[hf_path],
        read_files=[hf_path],
        written_files=[],
    )
    python_data = PythonInjectData(sys_prefix="", sys_base_prefix="", roar_inject_dir="")

    filtered = FileFilterService().filter_files(
        tracer_data, python_data, _filter_config(ignore_library_caches=False)
    )

    assert hf_path in filtered.opened_files
    assert hf_path in filtered.read_files


def test_filter_files_keeps_project_specific_cache_under_dot_cache(monkeypatch) -> None:
    """~/.cache/<your-project>/ is NOT on the curated list and stays tracked.

    This is the load-bearing property: nanochat writes outputs under
    ~/.cache/nanochat/ and those must NOT be filtered as cache.
    """
    monkeypatch.setattr(file_filter, "_get_editable_install_dirs", lambda: frozenset())

    project_cache_output = "/home/ubuntu/.cache/nanochat/base_checkpoints/d20/model_003320.pt"
    project_cache_tokenizer = "/root/.cache/nanochat/tokenizer/tokenizer.pkl"
    tracer_data = TracerData(
        opened_files=[project_cache_output, project_cache_tokenizer],
        read_files=[project_cache_tokenizer],
        written_files=[project_cache_output, project_cache_tokenizer],
    )
    python_data = PythonInjectData(sys_prefix="", sys_base_prefix="", roar_inject_dir="")

    filtered = FileFilterService().filter_files(
        tracer_data, python_data, _filter_config(ignore_library_caches=True)
    )

    assert project_cache_output in filtered.opened_files
    assert project_cache_output in filtered.written_files
    assert project_cache_tokenizer in filtered.read_files
    assert project_cache_tokenizer in filtered.written_files


def test_filter_files_drops_other_library_caches(monkeypatch) -> None:
    """A handful of the other curated entries — pip, uv, wandb, mlflow."""
    monkeypatch.setattr(file_filter, "_get_editable_install_dirs", lambda: frozenset())

    paths = [
        "/home/ubuntu/.cache/pip/wheels/abc/some.whl",
        "/home/ubuntu/.cache/uv/cache-v0/somefile",
        "/home/ubuntu/.cache/wandb/run-20260101_000000/log",
        "/home/ubuntu/.cache/mlflow/mlruns/0/meta.yaml",
        "/home/ubuntu/.cache/poetry/cache/some.tar.gz",
    ]
    tracer_data = TracerData(
        opened_files=list(paths),
        read_files=list(paths),
        written_files=list(paths),
    )
    python_data = PythonInjectData(sys_prefix="", sys_base_prefix="", roar_inject_dir="")

    filtered = FileFilterService().filter_files(
        tracer_data, python_data, _filter_config(ignore_library_caches=True)
    )

    for p in paths:
        assert p not in filtered.opened_files
        assert p not in filtered.read_files
        assert p not in filtered.written_files
    assert filtered.counts.library_caches >= len(paths)


def test_filter_files_always_drops_netrc_credentials(monkeypatch) -> None:
    """`.netrc`/`_netrc` hold machine login tokens (wandb, huggingface, curl)
    and must NEVER enter lineage — dropped from reads AND writes, regardless of
    any filter config. Observed leaking into a nanochat run (wandb reads ~/.netrc).
    """
    monkeypatch.setattr(file_filter, "_get_editable_install_dirs", lambda: frozenset())

    netrc = str(Path("~/.netrc").expanduser())
    win_netrc = "/home/ubuntu/_netrc"
    tracer_data = TracerData(
        opened_files=[netrc, win_netrc],
        read_files=[netrc, win_netrc],
        written_files=[netrc],
    )
    python_data = PythonInjectData(sys_prefix="", sys_base_prefix="", roar_inject_dir="")

    filtered = FileFilterService().filter_files(tracer_data, python_data, _filter_config())

    for p in (netrc, win_netrc):
        assert p not in filtered.opened_files
        assert p not in filtered.read_files
        assert p not in filtered.written_files
