"""CLI tests for `roar register --bind` / `--no-bind` — the "register implies bind" rule.

Uses a real local roar DB (via job_recording) for the artifact + tag data, and
mocks only register_lineage_target (the actual GLaaS network call) — so the
implicit-bind target classification and the real TagService.bind path are
genuinely exercised end to end through the CLI.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from roar.application.publish.results import RegisterLineageResponse
from roar.cli.commands.register import register
from roar.core.label_origins import LABEL_ORIGIN_SYSTEM
from roar.db.context import create_database_context


def _seed_tagged_artifact(roar_dir: Path, model_path: Path) -> str:
    """Record a job producing model_path, tag it (system-origin, no bind), return its hash."""
    model_path.write_bytes(b"weights")
    with create_database_context(roar_dir) as db_ctx:
        job_id, job_uid = db_ctx.job_recording.record_job(
            command="train.py",
            timestamp=1_700_000_000.0,
            output_files=[str(model_path)],
        )
        artifact_id = db_ctx.jobs.get_outputs(job_id)[0]["artifact_id"]
        db_ctx.labels.create_version(
            "artifact",
            {
                "tag": {
                    "license": {
                        "values": [{"value": "MIT", "origin": LABEL_ORIGIN_SYSTEM, "job": job_uid}]
                    }
                }
            },
            artifact_id=artifact_id,
            write_origin=LABEL_ORIGIN_SYSTEM,
        )
        artifact = db_ctx.artifacts.get(artifact_id)
    return next(h["digest"] for h in artifact["hashes"] if h["algorithm"] == "blake3")


def _ctx(tmp_path: Path) -> MagicMock:
    ctx = MagicMock()
    ctx.roar_dir = tmp_path / ".roar"
    ctx.cwd = tmp_path
    ctx.is_initialized = True
    return ctx


def _response(artifact_hash: str, **overrides) -> RegisterLineageResponse:
    defaults = {
        "success": True,
        "session_hash": "a" * 64,
        "artifact_hash": artifact_hash,
        "jobs_registered": 1,
        "artifacts_registered": 1,
        "links_created": 0,
    }
    defaults.update(overrides)
    return RegisterLineageResponse(**defaults)


def _invoke(tmp_path: Path, args: list[str], response: RegisterLineageResponse):
    runner = CliRunner()
    with (
        patch("roar.cli.publish_intent._is_logged_in", return_value=True),
        patch("roar.cli.commands.register.register_lineage_target", return_value=response),
        patch(
            "roar.cli.commands.register._resolve_glaas_web_url",
            return_value="https://glaas.example",
        ),
    ):
        return runner.invoke(register, args, obj=_ctx(tmp_path))


def test_register_by_artifact_hash_implicitly_binds(tmp_path: Path) -> None:
    artifact_hash = _seed_tagged_artifact(tmp_path / ".roar", tmp_path / "model.pt")
    result = _invoke(tmp_path, [artifact_hash], _response(artifact_hash))
    assert result.exit_code == 0, result.output
    assert "Bound:" in result.output
    assert "license=MIT" in result.output


def test_no_bind_skips_the_implicit_bind(tmp_path: Path) -> None:
    artifact_hash = _seed_tagged_artifact(tmp_path / ".roar", tmp_path / "model.pt")
    result = _invoke(tmp_path, [artifact_hash, "--no-bind"], _response(artifact_hash))
    assert result.exit_code == 0, result.output
    assert "Bound:" not in result.output


def test_bind_flag_binds_an_artifact_unrelated_to_the_registered_target(tmp_path: Path) -> None:
    """`register --bind X` binds X even when the response has no artifact_hash of its own
    (a session-wide register) — --bind is independent of the implicit target rule."""
    artifact_hash = _seed_tagged_artifact(tmp_path / ".roar", tmp_path / "model.pt")
    response = _response("", jobs_registered=2, artifacts_registered=2, links_created=1)
    result = _invoke(tmp_path, ["--bind", artifact_hash], response)
    assert result.exit_code == 0, result.output
    assert "Bound:" in result.output
    assert "license=MIT" in result.output


def test_already_bound_target_reports_no_change(tmp_path: Path) -> None:
    artifact_hash = _seed_tagged_artifact(tmp_path / ".roar", tmp_path / "model.pt")
    response = _response(artifact_hash)
    first = _invoke(tmp_path, [artifact_hash], response)
    assert "Bound:" in first.output

    second = _invoke(tmp_path, [artifact_hash], response)
    assert second.exit_code == 0, second.output
    assert "no change" in second.output
