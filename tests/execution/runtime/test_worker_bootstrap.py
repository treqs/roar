from __future__ import annotations

import shutil
from pathlib import Path

from roar.execution.runtime.inject.support import is_suppressed
from roar.execution.runtime.worker_bootstrap import build_packaged_worker_runtime_env


def test_build_packaged_worker_runtime_env_copies_user_files_and_preload(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source_working_dir = tmp_path / "user-working-dir"
    source_working_dir.mkdir()
    (source_working_dir / "user-file.txt").write_text("hello", encoding="utf-8")

    preload_path = tmp_path / "libroar_tracer_preload.so"
    preload_path.write_text("preload", encoding="utf-8")
    monkeypatch.setattr(
        "roar.execution.runtime.tracer_backends.find_preload_library",
        lambda _package_path: str(preload_path),
    )

    prepared = build_packaged_worker_runtime_env(
        {"working_dir": str(source_working_dir)},
        "job1234",
    )

    working_dir = Path(str(prepared["working_dir"]))
    assert (working_dir / "user-file.txt").read_text(encoding="utf-8") == "hello"
    assert (working_dir / "libroar_tracer_preload.so").read_text(encoding="utf-8") == "preload"
    assert (working_dir / "roar" / "backends" / "ray" / "roar_worker.py").exists()
    assert not (working_dir / "sitecustomize.py").exists()


def test_build_packaged_worker_runtime_env_suppresses_internal_copy_operations(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source_working_dir = tmp_path / "user-working-dir"
    source_working_dir.mkdir()

    preload_path = tmp_path / "libroar_tracer_preload.so"
    preload_path.write_text("preload", encoding="utf-8")
    monkeypatch.setattr(
        "roar.execution.runtime.tracer_backends.find_preload_library",
        lambda _package_path: str(preload_path),
    )

    suppressed_state: dict[str, bool] = {}

    def fake_merge_working_dir(_source: str, _target: str) -> None:
        suppressed_state["merge"] = is_suppressed()

    def fake_copytree(_src, dst, dirs_exist_ok=False):
        del dirs_exist_ok
        suppressed_state["copytree"] = is_suppressed()
        Path(dst).mkdir(parents=True, exist_ok=True)
        return dst

    def fake_copy2(_src, dst):
        suppressed_state["copy2"] = is_suppressed()
        Path(dst).write_text("preload", encoding="utf-8")
        return str(dst)

    monkeypatch.setattr(shutil, "copytree", fake_copytree)
    monkeypatch.setattr(shutil, "copy2", fake_copy2)

    build_packaged_worker_runtime_env(
        {"working_dir": str(source_working_dir)},
        "jobtrace",
        merge_working_dir=fake_merge_working_dir,
    )

    assert suppressed_state == {"merge": True, "copytree": True, "copy2": True}


def test_build_packaged_worker_runtime_env_warns_for_non_local_working_dir(
    monkeypatch,
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(
        "roar.execution.runtime.tracer_backends.find_preload_library",
        lambda _package_path: None,
    )

    prepared = build_packaged_worker_runtime_env(
        {"working_dir": "s3://bucket/path"},
        "job5678",
        warn=lambda message, *args: warnings.append(message % args if args else message),
    )

    assert warnings
    assert prepared["working_dir"] == "s3://bucket/path"


def test_build_packaged_worker_runtime_env_preserves_non_local_working_dir(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "roar.execution.runtime.tracer_backends.find_preload_library",
        lambda _package_path: None,
    )

    prepared = build_packaged_worker_runtime_env(
        {"working_dir": "s3://bucket/cloud-demo"},
        "jobraywd",
    )

    assert prepared["working_dir"] == "s3://bucket/cloud-demo"
