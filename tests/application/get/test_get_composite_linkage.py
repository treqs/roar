"""Tests for two get behaviors:

1. `_build_get_command` records the subset/identity flags (--limit, -n, --tag,
   --force, -m), not just a bare `roar get <source>`.
2. When an HF get forms a composite, the get job links to the composite artifact
   only — its leaf files are the composite's components, not sibling outputs.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from roar.application.get.requests import GetRequest
from roar.application.get.results import GetDownloadedFile
from roar.application.get.service import (
    _build_get_command,
    _materialize_get_result,
)
from roar.application.get.transfer import GetTransferResult


def _request(tmp_path: Path, **overrides) -> GetRequest:
    return GetRequest(
        source=overrides.pop("source", "hf://datasets/openai/gsm8k"),
        destination=overrides.pop("destination", tmp_path / "data"),
        roar_dir=overrides.pop("roar_dir", tmp_path / ".roar"),
        cwd=overrides.pop("cwd", tmp_path),
        repo_root=overrides.pop("repo_root", tmp_path),
        **overrides,
    )


def _transfer(tmp_path: Path) -> GetTransferResult:
    from roar.application.get.results import GetDownloadedFile

    return GetTransferResult(
        success=True,
        downloaded_files=[
            GetDownloadedFile(
                remote_url="hf://datasets/openai/gsm8k/train-0.parquet",
                local_path=str(tmp_path / "data/train-0.parquet"),
                hash="a" * 64,
                size=1000,
            ),
            GetDownloadedFile(
                remote_url="hf://datasets/openai/gsm8k/train-1.parquet",
                local_path=str(tmp_path / "data/train-1.parquet"),
                hash="b" * 64,
                size=2000,
            ),
        ],
    )


# ---- _build_get_command -------------------------------------------------------


def test_build_get_command_bare(tmp_path: Path) -> None:
    cmd = _build_get_command(_request(tmp_path))
    assert cmd == "roar get hf://datasets/openai/gsm8k"


def test_build_get_command_includes_limit(tmp_path: Path) -> None:
    cmd = _build_get_command(_request(tmp_path, limit=2))
    assert cmd == "roar get hf://datasets/openai/gsm8k --limit 2"


def test_build_get_command_includes_all_flags(tmp_path: Path) -> None:
    cmd = _build_get_command(
        _request(tmp_path, limit=8, step_name="prep", tag=True, force=True, message="hi")
    )
    assert "--limit 8" in cmd
    assert '-n "prep"' in cmd
    assert "--tag" in cmd
    assert "--force" in cmd
    assert '-m "hi"' in cmd


def test_build_get_command_omits_absolute_destination(tmp_path: Path) -> None:
    cmd = _build_get_command(_request(tmp_path, limit=2))
    assert str(tmp_path) not in cmd


# ---- composite-only job output ------------------------------------------------


def _materialize(tmp_path: Path, *, composite):
    db_ctx = MagicMock()
    recorder = MagicMock()
    recorder.record.return_value = (1, "uid")
    with (
        patch("roar.application.get.service.LocalJobRecorder", return_value=recorder),
        patch("roar.application.get.service._form_get_composite", return_value=composite),
    ):
        _materialize_get_result(
            db_ctx=db_ctx,
            request=_request(tmp_path, limit=2),
            parsed_source=MagicMock(scheme="hf"),
            transfer_result=_transfer(tmp_path),
            git_commit=None,
            backend=MagicMock(),
        )
    return db_ctx, recorder


def test_composite_get_links_composite_not_subfiles(tmp_path: Path) -> None:
    db_ctx, recorder = _materialize(
        tmp_path, composite=("comp-id", "hf://datasets/openai/gsm8k", "d" * 64)
    )
    # The job is recorded with NO per-file outputs...
    _, kwargs = recorder.record.call_args
    assert kwargs["output_artifacts"] == []
    # ...and the anchor is the single linked output.
    db_ctx.jobs.add_output.assert_called_once_with(1, "comp-id", "hf://datasets/openai/gsm8k")


def test_composite_get_records_anchor_and_selector_view(tmp_path: Path) -> None:
    import json

    _db_ctx, recorder = _materialize(
        tmp_path, composite=("comp-id", "hf://datasets/openai/gsm8k", "d" * 64)
    )
    _, kwargs = recorder.record.call_args
    meta = json.loads(kwargs["metadata"])["get"]
    # A `--limit 2` get is recorded as a `first:2` view over the anchor digest, not a
    # subset composite.
    assert meta["anchor"] == "d" * 64
    assert meta["selector"] == "first:2"


def test_non_composite_get_links_files(tmp_path: Path) -> None:
    db_ctx, recorder = _materialize(tmp_path, composite=None)
    _, kwargs = recorder.record.call_args
    assert len(kwargs["output_artifacts"]) == 2  # both parquet files
    db_ctx.jobs.add_output.assert_not_called()


# ---- crosswalk cache rows (Gap B) ---------------------------------------------


def _materialize_with_backend(tmp_path: Path, *, composite, backend):
    db_ctx = MagicMock()
    recorder = MagicMock()
    recorder.record.return_value = (1, "uid")
    transfer = GetTransferResult(
        success=True,
        downloaded_files=[
            GetDownloadedFile(
                remote_url="hf://datasets/openai/gsm8k/train-0.parquet",
                local_path=str(tmp_path / "data/train-0.parquet"),
                hash="a" * 64,
                size=1000,
                remote_key="train-0.parquet",
            ),
        ],
    )
    with (
        patch("roar.application.get.service.LocalJobRecorder", return_value=recorder),
        patch("roar.application.get.service._form_get_composite", return_value=composite),
    ):
        _materialize_get_result(
            db_ctx=db_ctx,
            request=_request(tmp_path, limit=2),
            parsed_source=MagicMock(scheme="hf"),
            transfer_result=transfer,
            git_commit=None,
            backend=backend,
        )
    return db_ctx


def test_composite_get_registers_crosswalk_row_blake3_only(tmp_path: Path) -> None:
    import json

    backend = MagicMock()
    backend._sha256_by_path.return_value = {"train-0.parquet": "f" * 64}
    db_ctx = _materialize_with_backend(
        tmp_path, composite=("comp-id", "hf://datasets/openai/gsm8k", "d" * 64), backend=backend
    )
    # The shard publishes its blake3 only; the crosswalk sha256 (HF oid) lives in
    # metadata, never as a second hash — so no registration path can leak it (F1).
    _, kwargs = db_ctx.artifacts.register.call_args
    assert kwargs["hashes"] == {"blake3": "a" * 64}
    assert json.loads(kwargs["metadata"])["origin"] == {"algorithm": "sha256", "digest": "f" * 64}


def test_non_composite_get_registers_no_crosswalk(tmp_path: Path) -> None:
    backend = MagicMock()
    db_ctx = _materialize_with_backend(tmp_path, composite=None, backend=backend)
    db_ctx.artifacts.register.assert_not_called()
