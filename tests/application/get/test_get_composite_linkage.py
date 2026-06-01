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
    db_ctx, recorder = _materialize(tmp_path, composite=("comp-id", "hf://datasets/openai/gsm8k"))
    # The job is recorded with NO per-file outputs...
    _, kwargs = recorder.record.call_args
    assert kwargs["output_artifacts"] == []
    # ...and the composite is the single linked output.
    db_ctx.jobs.add_output.assert_called_once_with(1, "comp-id", "hf://datasets/openai/gsm8k")


def test_non_composite_get_links_files(tmp_path: Path) -> None:
    db_ctx, recorder = _materialize(tmp_path, composite=None)
    _, kwargs = recorder.record.call_args
    assert len(kwargs["output_artifacts"]) == 2  # both parquet files
    db_ctx.jobs.add_output.assert_not_called()
