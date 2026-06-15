from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from roar.application.publish.put_execution import PutResult
from roar.application.publish.register_execution import RegisterResult
from roar.application.publish.register_preparation import PreparedRegisterExecution
from roar.application.publish.requests import PutRequest, RegisterLineageRequest
from roar.application.publish.results import PutDryRunItem, PutResponse, RegisterLineageResponse
from roar.application.publish.service import put_artifacts, register_lineage_target
from roar.application.publish.targets import ResolvedRegisterTarget
from roar.core.interfaces.lineage import LineageData


def test_register_lineage_target_returns_collection_error(tmp_path: Path) -> None:
    runtime = MagicMock()

    with (
        patch("roar.application.publish.service.build_publish_runtime", return_value=runtime),
        patch(
            "roar.application.publish.service.resolve_register_lineage_target",
            return_value=ResolvedRegisterTarget(kind="artifact_path", value="missing.csv"),
        ),
        patch(
            "roar.application.publish.service.collect_register_lineage",
            return_value=(None, "File not found: missing.csv"),
        ),
        patch("roar.application.publish.service.RegisterService") as mock_cls,
    ):
        response = register_lineage_target(
            RegisterLineageRequest(
                target="missing.csv",
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
            )
        )

    assert response == RegisterLineageResponse(success=False, error="File not found: missing.csv")
    mock_cls.return_value.register_prepared_lineage.assert_not_called()


def test_register_lineage_target_returns_preparation_error(tmp_path: Path) -> None:
    runtime = MagicMock()
    logger = MagicMock()
    collected = MagicMock()
    collected.lineage = LineageData(
        jobs=[], artifacts=[], artifact_hashes=set(), pipeline={"id": 7}
    )
    collected.session_id = 7
    collected.artifact_hash = "a" * 64
    collected.session_hash_override = None

    with (
        patch("roar.application.publish.service.build_publish_runtime", return_value=runtime),
        patch("roar.application.publish.service.get_logger", return_value=logger),
        patch(
            "roar.application.publish.service.resolve_register_lineage_target",
            return_value=ResolvedRegisterTarget(kind="artifact_path", value="model.pt"),
        ),
        patch(
            "roar.application.publish.service.collect_register_lineage",
            return_value=(collected, None),
        ),
        patch(
            "roar.application.publish.service.prepare_register_execution",
            side_effect=ValueError("GLaaS not configured"),
        ),
        patch("roar.application.publish.service.RegisterService") as mock_cls,
    ):
        response = register_lineage_target(
            RegisterLineageRequest(
                target="model.pt",
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
            )
        )

    assert response == RegisterLineageResponse(
        success=False,
        artifact_hash="a" * 64,
        error="GLaaS not configured",
    )
    mock_cls.return_value.register_prepared_lineage.assert_not_called()


def test_register_lineage_target_pushes_tags_before_glaas_write(tmp_path: Path) -> None:
    """P1-23: tag push runs BEFORE the GLaaS write so any failure means
    no record is created; success means the response carries a tag summary."""
    from roar.application.publish.results import RegisterTagSummary

    expected = RegisterResult(
        success=True,
        session_hash="a" * 64,
        session_url="https://glaas.local/sessions/published",
    )
    runtime = MagicMock()
    logger = MagicMock()
    collected = MagicMock()
    collected.lineage = LineageData(
        jobs=[], artifacts=[], artifact_hashes=set(), pipeline={"id": 7}
    )
    collected.session_id = 7
    collected.artifact_hash = "a" * 64
    collected.session_hash_override = None
    git_context = MagicMock(commit="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
    prepared = PreparedRegisterExecution(
        git_context=git_context,
        session_id=7,
        session_hash="a" * 64,
        session_url="https://glaas.local/dag/session",
        git_tag_name="roar/deadbeef",
        git_tag_repo_root=tmp_path,
    )
    tag_summary = RegisterTagSummary(session_tag="roar/deadbeef", remote="origin")

    with (
        patch("roar.application.publish.service.build_publish_runtime", return_value=runtime),
        patch("roar.application.publish.service.get_logger", return_value=logger),
        patch(
            "roar.application.publish.service.resolve_register_lineage_target",
            return_value=ResolvedRegisterTarget(kind="artifact_path", value="model.pt"),
        ),
        patch(
            "roar.application.publish.service.collect_register_lineage",
            return_value=(collected, None),
        ),
        patch(
            "roar.application.publish.service.prepare_register_execution",
            return_value=prepared,
        ),
        patch(
            "roar.application.publish.register_tag_push.ensure_roar_tags_pushed",
            return_value=tag_summary,
        ) as ensure_push,
        patch("roar.application.publish.service.RegisterService") as mock_cls,
    ):
        mock_cls.return_value.register_prepared_lineage.return_value = expected

        response = register_lineage_target(
            RegisterLineageRequest(
                target="model.pt",
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
            )
        )

    # Tag push ran exactly once, with the prepared session-tag name.
    ensure_push.assert_called_once()
    kwargs = ensure_push.call_args.kwargs
    assert kwargs["session_tag_name"] == "roar/deadbeef"
    assert kwargs["session_commit"] == "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    assert kwargs["dry_run"] is False
    assert kwargs["tagging_enabled"] is True

    assert response.success is True
    assert response.session_hash == "a" * 64
    assert response.tag_summary == tag_summary


def test_register_lineage_target_aborts_on_tag_push_failure(tmp_path: Path) -> None:
    """P1-23 fail-closed: if the push fails, no GLaaS write happens and the
    response carries the error message verbatim."""
    from roar.application.publish.register_tag_push import TagPushError

    runtime = MagicMock()
    logger = MagicMock()
    collected = MagicMock()
    collected.lineage = LineageData(
        jobs=[], artifacts=[], artifact_hashes=set(), pipeline={"id": 7}
    )
    collected.session_id = 7
    collected.artifact_hash = "a" * 64
    collected.session_hash_override = None
    prepared = PreparedRegisterExecution(
        git_context=MagicMock(commit="deadbeef"),
        session_id=7,
        session_hash="a" * 64,
        session_url=None,
        git_tag_name="roar/deadbeef",
        git_tag_repo_root=tmp_path,
    )

    with (
        patch("roar.application.publish.service.build_publish_runtime", return_value=runtime),
        patch("roar.application.publish.service.get_logger", return_value=logger),
        patch(
            "roar.application.publish.service.resolve_register_lineage_target",
            return_value=ResolvedRegisterTarget(kind="artifact_path", value="model.pt"),
        ),
        patch(
            "roar.application.publish.service.collect_register_lineage",
            return_value=(collected, None),
        ),
        patch(
            "roar.application.publish.service.prepare_register_execution",
            return_value=prepared,
        ),
        patch(
            "roar.application.publish.register_tag_push.ensure_roar_tags_pushed",
            side_effect=TagPushError("auth failed pushing to origin"),
        ),
        patch("roar.application.publish.service.RegisterService") as mock_cls,
    ):
        register_service = mock_cls.return_value
        response = register_lineage_target(
            RegisterLineageRequest(
                target="model.pt",
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
            )
        )

    # GLaaS write never happened.
    register_service.register_prepared_lineage.assert_not_called()
    assert response.success is False
    assert "auth failed pushing to origin" in (response.error or "")


def test_register_lineage_target_anonymous_downgrades_tag_push_failure(tmp_path: Path) -> None:
    """Anonymous register continues past a tag-push failure with a warning.

    The user explicitly opted into no-account publication, so requiring
    git-remote auth (a separate concern from GLaaS) would defeat the
    point. The GLaaS write must still happen, and the response must
    carry a self-contained warning that names the actual failure kind
    (git auth, not GLaaS) and the config knob that silences it.
    """
    from roar.application.publish.register_tag_push import TagPushError

    expected = RegisterResult(
        success=True,
        session_hash="b" * 64,
        session_url="https://glaas.local/dag/session",
    )
    runtime = MagicMock()
    logger = MagicMock()
    collected = MagicMock()
    collected.lineage = LineageData(
        jobs=[], artifacts=[], artifact_hashes=set(), pipeline={"id": 9}
    )
    collected.session_id = 9
    collected.artifact_hash = "b" * 64
    collected.session_hash_override = None
    prepared = PreparedRegisterExecution(
        git_context=MagicMock(commit="cafebabecafebabecafebabecafebabecafebabe"),
        session_id=9,
        session_hash="b" * 64,
        session_url="https://glaas.local/dag/session",
        git_tag_name="roar/cafebabe",
        git_tag_repo_root=tmp_path,
    )

    with (
        patch("roar.application.publish.service.build_publish_runtime", return_value=runtime),
        patch("roar.application.publish.service.get_logger", return_value=logger),
        patch(
            "roar.application.publish.service.resolve_register_lineage_target",
            return_value=ResolvedRegisterTarget(kind="artifact_path", value="model.pt"),
        ),
        patch(
            "roar.application.publish.service.collect_register_lineage",
            return_value=(collected, None),
        ),
        patch(
            "roar.application.publish.service.prepare_register_execution",
            return_value=prepared,
        ),
        patch(
            "roar.application.publish.register_tag_push.ensure_roar_tags_pushed",
            side_effect=TagPushError(
                "git@github.com: Permission denied (publickey).\n"
                "fatal: Could not read from remote repository."
            ),
        ),
        patch("roar.application.publish.service.RegisterService") as mock_cls,
    ):
        register_service = mock_cls.return_value
        register_service.register_prepared_lineage.return_value = expected

        response = register_lineage_target(
            RegisterLineageRequest(
                target="model.pt",
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
                public=True,
                anonymous=True,
            )
        )

    # GLaaS write DID happen — anonymous register is not gated on tag push.
    register_service.register_prepared_lineage.assert_called_once()
    assert response.success is True
    assert response.session_hash == "b" * 64

    # And the user gets a warning that:
    # - names this as a git push failure, not a GLaaS auth failure
    # - mentions GLaaS registration still happened (via "continued")
    # - explains why the missing push matters (reproducibility for
    #   anyone reading the GLaaS record)
    # - points at the real fix (`git push` after configuring auth)
    # - includes the verbatim git error for context
    # - does NOT suggest `git.push_tags_on_register=never`, which would
    #   break reproducibility for every future anonymous register
    assert len(response.warnings) == 1
    warning = response.warnings[0]
    assert "git remote" in warning
    assert "not GLaaS" in warning
    assert "continued" in warning
    assert "git push" in warning
    assert "Permission denied (publickey)" in warning
    assert "git.push_tags_on_register" not in warning
    assert "never" not in warning


def test_put_artifacts_rejects_dirty_repo_before_put(tmp_path: Path) -> None:
    with (
        patch("roar.application.publish.service.bootstrap"),
        patch("roar.application.publish.service.get_logger", return_value=MagicMock()),
        patch(
            "roar.application.publish.service.create_database_context",
            return_value=nullcontext(MagicMock()),
        ),
        patch(
            "roar.application.publish.service.resolve_publish_storage_backend",
            return_value=MagicMock(),
        ),
        patch("roar.application.publish.service.PutService") as mock_put_cls,
        patch(
            "roar.application.publish.service.prepare_put_git",
            side_effect=ValueError("Repository has uncommitted changes"),
        ),
        pytest.raises(ValueError, match="Repository has uncommitted changes"),
    ):
        put_artifacts(
            PutRequest(
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
                repo_root=tmp_path,
                sources=["model.pt"],
                destination="memory://bucket/prefix",
                message="publish",
            )
        )

    mock_put_cls.return_value.put_prepared.assert_not_called()


def test_put_artifacts_continues_when_git_preflight_warns(tmp_path: Path) -> None:
    db_ctx = MagicMock()
    put_result = PutResult(success=True, job_id=3, uploaded_files=[], dry_run=False)
    prepared = MagicMock()
    prepared_git = MagicMock(
        git_commit=None, expected_tag=None, warnings=("Git operation failed: git unavailable",)
    )

    runtime = MagicMock()

    with (
        patch("roar.application.publish.service.bootstrap"),
        patch("roar.application.publish.service.get_logger", return_value=MagicMock()),
        patch(
            "roar.application.publish.service.create_database_context",
            return_value=nullcontext(db_ctx),
        ),
        patch(
            "roar.application.publish.service.resolve_publish_storage_backend",
            return_value=MagicMock(),
        ),
        patch(
            "roar.application.publish.service.build_publish_runtime",
            return_value=runtime,
        ),
        patch(
            "roar.application.publish.service.prepare_put_execution",
            return_value=prepared,
        ),
        patch("roar.application.publish.service.PutService") as mock_put_cls,
        patch(
            "roar.application.publish.service.prepare_put_git",
            return_value=prepared_git,
        ),
        patch(
            "roar.application.publish.service.finalize_put_git",
            return_value=(None, []),
        ),
    ):
        mock_put_cls.return_value.put_prepared.return_value = put_result

        response = put_artifacts(
            PutRequest(
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
                repo_root=tmp_path,
                sources=["model.pt"],
                destination="memory://bucket/prefix",
                message="publish",
            )
        )

    assert response == PutResponse(
        success=True,
        destination="memory://bucket/prefix",
        job_id=3,
        dry_run=False,
        warnings=["Git operation failed: git unavailable"],
    )


def test_put_artifacts_returns_preparation_error_before_service(tmp_path: Path) -> None:
    db_ctx = MagicMock()
    db_ctx.sessions.get_active.return_value = None

    with (
        patch("roar.application.publish.service.bootstrap"),
        patch("roar.application.publish.service.get_logger", return_value=MagicMock()),
        patch(
            "roar.application.publish.service.create_query_database_context",
            return_value=nullcontext(db_ctx),
        ),
        patch(
            "roar.application.publish.service.prepare_put_git",
            return_value=MagicMock(git_commit=None, expected_tag=None, warnings=()),
        ),
        patch("roar.application.publish.service.create_database_context") as create_db_ctx,
        patch(
            "roar.application.publish.service.resolve_publish_storage_backend"
        ) as resolve_backend,
        patch("roar.application.publish.service.build_publish_runtime") as build_runtime,
        patch("roar.application.publish.service.prepare_put_execution") as prepare_put_execution,
        patch("roar.application.publish.service.PutService") as mock_put_cls,
        pytest.raises(ValueError, match="No active session"),
    ):
        put_artifacts(
            PutRequest(
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
                repo_root=tmp_path,
                sources=["model.pt"],
                destination="memory://bucket/prefix",
                message="publish",
                dry_run=True,
            )
        )

    create_db_ctx.assert_not_called()
    resolve_backend.assert_not_called()
    build_runtime.assert_not_called()
    prepare_put_execution.assert_not_called()
    mock_put_cls.return_value.put_prepared.assert_not_called()


def test_put_artifacts_dry_run_skips_backend_runtime_and_service(tmp_path: Path) -> None:
    db_ctx = MagicMock()
    db_ctx.sessions.get_active.return_value = {"id": 7}
    model = tmp_path / "model.pt"
    model.write_bytes(b"model")

    with (
        patch("roar.application.publish.service.bootstrap") as bootstrap,
        patch("roar.application.publish.service.get_logger", return_value=MagicMock()),
        patch(
            "roar.application.publish.service.create_query_database_context",
            return_value=nullcontext(db_ctx),
        ),
        patch("roar.application.publish.service.prepare_put_git") as prepare_put_git,
        patch("roar.application.publish.service.create_database_context") as create_db_ctx,
        patch(
            "roar.application.publish.service.resolve_publish_storage_backend"
        ) as resolve_backend,
        patch("roar.application.publish.service.build_publish_runtime") as build_runtime,
        patch("roar.application.publish.service.prepare_put_execution") as prepare_put_execution,
        patch("roar.application.publish.service.PutService") as mock_put_cls,
        patch("roar.application.publish.service.finalize_put_git") as finalize_put_git,
    ):
        response = put_artifacts(
            PutRequest(
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
                repo_root=tmp_path,
                sources=["model.pt"],
                destination="s3://bucket/prefix",
                message="publish",
                dry_run=True,
            )
        )

    assert response == PutResponse(
        success=True,
        destination="s3://bucket/prefix",
        dry_run=True,
        would_upload=[PutDryRunItem(path=str(model.resolve()), exists=True)],
        warnings=[],
    )
    create_db_ctx.assert_not_called()
    resolve_backend.assert_not_called()
    build_runtime.assert_not_called()
    prepare_put_execution.assert_not_called()
    mock_put_cls.assert_not_called()
    bootstrap.assert_not_called()
    prepare_put_git.assert_not_called()
    finalize_put_git.assert_not_called()


def test_load_remote_job_uid_mapping_reads_persisted_glaas_jobs(tmp_path: Path) -> None:
    """The view-edge push must address GLaaS jobs by their publication-scoped *remote*
    UID, not the local UID; registration persists that map under
    ``roar.remote_publication.glaas.jobs`` and the loader returns it (regression: the
    push previously used the local UID and 404'd)."""
    import json

    from roar.application.publish.service import _load_remote_job_uid_mapping
    from roar.db.context import create_database_context

    roar_dir = tmp_path / ".roar"
    with create_database_context(roar_dir) as db:
        session_id = db.sessions.create(make_active=True)
        db.sessions.update_metadata(
            session_id,
            json.dumps(
                {
                    "roar": {
                        "remote_publication": {
                            "glaas": {"jobs": {"e013c57b": "1ca8de8f29c8ec70c0031a10c3e91e5d"}}
                        }
                    }
                }
            ),
        )

    mapping = _load_remote_job_uid_mapping(roar_dir=roar_dir, session_id=session_id)
    assert mapping == {"e013c57b": "1ca8de8f29c8ec70c0031a10c3e91e5d"}


def test_load_remote_job_uid_mapping_missing_metadata_returns_empty(tmp_path: Path) -> None:
    from roar.application.publish.service import _load_remote_job_uid_mapping
    from roar.db.context import create_database_context

    roar_dir = tmp_path / ".roar"
    with create_database_context(roar_dir) as db:
        session_id = db.sessions.create(make_active=True)

    assert _load_remote_job_uid_mapping(roar_dir=roar_dir, session_id=session_id) == {}
    assert _load_remote_job_uid_mapping(roar_dir=roar_dir, session_id=None) == {}
