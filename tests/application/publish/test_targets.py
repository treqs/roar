from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from roar.application.publish.targets import (
    ResolvedRegisterTarget,
    parse_register_step_reference,
    resolve_register_lineage_target,
)


def test_parse_register_step_reference_supports_run_and_build_steps() -> None:
    assert parse_register_step_reference("@4") == (4, False)
    assert parse_register_step_reference("@B2") == (2, True)
    assert parse_register_step_reference("metrics.json") is None


def test_resolve_register_lineage_target_prefers_tracked_job_uid(tmp_path: Path) -> None:
    with (
        patch(
            "roar.application.publish.targets.resolve_local_job_uid",
            return_value="deadbeefcafebabe",
        ),
        patch("roar.application.publish.targets.resolve_local_artifact_hash", return_value=None),
    ):
        resolved = resolve_register_lineage_target(
            "deadbeef",
            cwd=tmp_path,
            roar_dir=tmp_path / ".roar",
        )

    assert resolved == ResolvedRegisterTarget(kind="job_uid", value="deadbeefcafebabe")


def test_resolve_register_lineage_target_prefers_existing_artifact_path(tmp_path: Path) -> None:
    artifact = tmp_path / "metrics.json"
    artifact.write_text("{}\n", encoding="utf-8")

    resolved = resolve_register_lineage_target(
        "metrics.json",
        cwd=tmp_path,
        roar_dir=tmp_path / ".roar",
    )

    assert resolved == ResolvedRegisterTarget(kind="artifact_path", value="metrics.json")


def test_resolve_register_lineage_target_resolves_artifact_hash_from_db(tmp_path: Path) -> None:
    with (
        patch("roar.application.publish.targets.resolve_local_job_uid", return_value=None),
        patch(
            "roar.application.publish.targets.resolve_local_artifact_hash",
            return_value="a" * 64,
        ),
    ):
        resolved = resolve_register_lineage_target(
            "a" * 64,
            cwd=tmp_path,
            roar_dir=tmp_path / ".roar",
        )

    assert resolved == ResolvedRegisterTarget(kind="artifact_hash", value="a" * 64)


def test_resolve_register_lineage_target_passes_session_hash_prefix_through(tmp_path: Path) -> None:
    with (
        patch("roar.application.publish.targets.resolve_local_job_uid", return_value=None),
        patch("roar.application.publish.targets.resolve_local_artifact_hash", return_value=None),
    ):
        resolved = resolve_register_lineage_target(
            "b" * 8,
            cwd=tmp_path,
            roar_dir=tmp_path / ".roar",
        )

    assert resolved == ResolvedRegisterTarget(kind="session_hash", value="b" * 8)
