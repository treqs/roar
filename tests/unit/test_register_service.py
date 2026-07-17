"""Focused unit tests for RegisterService registration mechanics."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from roar.application.publish.job_preparation import (
    normalize_jobs_for_registration,
    order_jobs_for_registration,
)
from roar.application.publish.register_execution import RegisterService
from roar.application.publish.register_preparation import PreparedRegisterExecution
from roar.core.interfaces.lineage import LineageData
from roar.core.interfaces.registration import BatchRegistrationResult, GitContext


def _git_context(
    repo_root: Path,
    *,
    repo: str | None = "https://github.com/test/repo",
    commit: str = "abc123def456",
    branch: str | None = "main",
) -> GitContext:
    return GitContext(repo=repo, commit=commit, branch=branch)


def _prepared_execution(tmp_path: Path, *, session_hash: str = "session-hash-123"):
    return PreparedRegisterExecution(
        git_context=_git_context(tmp_path),
        session_id=1,
        session_hash=session_hash,
        session_url="https://glaas.example/dag/session-hash-123",
        git_tag_name="roar/abc123de",
        git_tag_repo_root=tmp_path,
    )


def _lineage_data(
    *,
    jobs: list[dict] | None = None,
    artifacts: list[dict] | None = None,
    artifact_hashes: set[str] | None = None,
    pipeline: dict | None = None,
) -> LineageData:
    return LineageData(
        jobs=jobs or [{"id": 1, "job_uid": "job-1"}],
        artifacts=artifacts or [{"id": "a1"}],
        artifact_hashes=artifact_hashes or {"hash1"},
        pipeline=pipeline or {"id": 1},
    )


class TestRegisterService:
    def setup_method(self) -> None:
        self.mock_glaas_client = MagicMock()
        self.mock_glaas_client.register_composite_artifact.return_value = (
            {"artifact_id": "srv-comp-1", "created": True},
            None,
        )
        self.mock_coordinator = MagicMock()
        self.mock_coordinator.register_lineage.return_value = BatchRegistrationResult(
            session_registered=True,
            jobs_created=1,
            jobs_failed=0,
            artifacts_registered=1,
            artifacts_failed=0,
            links_created=2,
            links_failed=0,
            errors=[],
        )
        self.service = RegisterService(
            glaas_client=self.mock_glaas_client,
            coordinator=self.mock_coordinator,
        )

    def test_order_jobs_for_registration_puts_parent_before_child(self) -> None:
        parent = {
            "id": 2,
            "job_uid": "parent-uid",
            "step_number": 1,
            "timestamp": 20.0,
        }
        child = {
            "id": 1,
            "job_uid": "child-uid",
            "parent_job_uid": "parent-uid",
            "step_number": 1,
            "timestamp": 10.0,
        }

        ordered = order_jobs_for_registration([child, parent])

        assert [job["job_uid"] for job in ordered] == ["parent-uid", "child-uid"]

    def test_normalize_jobs_for_registration_filters_known_ray_noise_jobs(self) -> None:
        submit_job = {
            "id": 1,
            "job_uid": "local-submit",
            "step_number": 1,
            "timestamp": 10.0,
            "command": "ray job submit --address http://localhost:8265 -- python main.py",
            "job_type": None,
        }
        noise_job = {
            "id": 2,
            "job_uid": "noise-job",
            "parent_job_uid": "local-submit",
            "step_number": 2,
            "timestamp": 20.0,
            "command": "ray_task:unknown",
            "job_type": "ray_task",
        }
        phase_job = {
            "id": 3,
            "job_uid": "phase-job",
            "parent_job_uid": "noise-job",
            "step_number": 3,
            "timestamp": 30.0,
            "command": "ray_task:process_shard",
            "job_type": "ray_task",
        }
        shutdown_job = {
            "id": 4,
            "job_uid": "shutdown-job",
            "parent_job_uid": "local-submit",
            "step_number": 4,
            "timestamp": 40.0,
            "command": "ray_task:shutdown",
            "job_type": "ray_task",
        }

        normalized = normalize_jobs_for_registration(
            [submit_job, noise_job, phase_job, shutdown_job]
        )

        assert [job["job_uid"] for job in normalized] == ["local-submit", "phase-job"]
        jobs_by_uid = {job["job_uid"]: job for job in normalized}
        assert jobs_by_uid["phase-job"]["parent_job_uid"] == "local-submit"

    def test_register_prepared_lineage_dry_run_filters_known_ray_noise_jobs(
        self, tmp_path: Path
    ) -> None:
        with patch("roar.application.publish.register_execution.config_get", return_value=False):
            result = self.service.register_prepared_lineage(
                lineage=_lineage_data(
                    jobs=[
                        {
                            "id": 1,
                            "job_uid": "local-submit",
                            "step_number": 1,
                            "timestamp": 10.0,
                            "command": "ray job submit --address http://localhost:8265 -- python main.py",
                            "job_type": None,
                            "_outputs": [{"artifact_id": "driver-output"}],
                        },
                        {
                            "id": 2,
                            "job_uid": "noise-job",
                            "parent_job_uid": "local-submit",
                            "step_number": 2,
                            "timestamp": 20.0,
                            "command": "ray_task:unknown",
                            "job_type": "ray_task",
                            "_outputs": [{"artifact_id": "noise-output"}],
                        },
                        {
                            "id": 3,
                            "job_uid": "phase-job",
                            "parent_job_uid": "noise-job",
                            "step_number": 3,
                            "timestamp": 30.0,
                            "command": "ray_task:process_shard",
                            "job_type": "ray_task",
                            "_inputs": [{"artifact_id": "driver-output"}],
                            "_outputs": [{"artifact_id": "task-output"}],
                        },
                    ],
                    artifacts=[{"id": "a1"}],
                    artifact_hashes={"hash1"},
                ),
                roar_dir=tmp_path / ".roar",
                artifact_hash="hash1",
                dry_run=True,
                as_blake3=False,
                skip_confirmation=False,
                confirm_callback=None,
                prepared=_prepared_execution(tmp_path),
            )

        assert result.success is True
        assert result.jobs_registered == 2
        assert result.links_created == 3
        self.mock_coordinator.register_lineage.assert_not_called()

    def test_register_prepared_lineage_preregisters_composites_before_batch_registration(
        self, tmp_path: Path
    ) -> None:
        primitive_digest = "a" * 64
        composite_digest = "c" * 64
        composite_root = tmp_path / "exports" / "bundle"

        lineage = _lineage_data(
            jobs=[
                {
                    "id": 1,
                    "job_uid": "job-1",
                    "step_number": 1,
                    "timestamp": 1.0,
                }
            ],
            artifacts=[
                {
                    "id": "primitive-1",
                    "hashes": [{"algorithm": "blake3", "digest": primitive_digest}],
                    "size": 7,
                    "source_type": "local",
                },
                {
                    "id": "composite-1",
                    "kind": "composite",
                    "first_seen_path": str(composite_root),
                    "hashes": [{"algorithm": "composite-blake3", "digest": composite_digest}],
                    "size": 13,
                    "component_count": 2,
                    "source_type": "local",
                },
            ],
            artifact_hashes={primitive_digest, composite_digest},
        )

        with (
            patch(
                "roar.application.publish.register_execution.create_database_context"
            ) as mock_ctx,
            patch("roar.application.publish.register_execution.config_get", return_value=False),
        ):
            mock_db = MagicMock()
            mock_db.__enter__ = MagicMock(return_value=mock_db)
            mock_db.__exit__ = MagicMock(return_value=None)
            mock_db.composites = MagicMock()
            mock_db.composites.get_components.return_value = [
                {
                    "relative_path": "part-000.json",
                    "leaf_kind": "file",
                    "component_algorithm": "blake3",
                    "component_digest": "d" * 64,
                    "component_size": 5,
                    "component_type": "application/json",
                },
                {
                    "relative_path": "part-001.json",
                    "leaf_kind": "file",
                    "component_algorithm": "blake3",
                    "component_digest": "e" * 64,
                    "component_size": 8,
                    "component_type": "application/json",
                },
            ]
            mock_db.composites.get_membership_index.return_value = None
            mock_ctx.return_value = mock_db

            result = self.service.register_prepared_lineage(
                lineage=lineage,
                roar_dir=tmp_path / ".roar",
                artifact_hash=primitive_digest,
                dry_run=False,
                as_blake3=False,
                skip_confirmation=False,
                confirm_callback=None,
                prepared=_prepared_execution(tmp_path),
            )

        assert result.success is True
        assert result.artifacts_registered == 2

        composite_payload = self.mock_glaas_client.register_composite_artifact.call_args.args[0]
        assert composite_payload["hash"] == composite_digest
        assert composite_payload["source_type"] is None
        assert composite_payload["component_count_total"] == 2
        assert len(composite_payload["components"]) == 2
        assert composite_payload["membership_index"]["total_components"] == 2
        assert composite_payload["membership_index"]["stored_components"] == 2
        assert composite_payload["membership_index"]["bloom_filter_base64"]
        assert composite_payload["membership_index"]["bloom_bits"] > 0
        assert composite_payload["membership_index"]["bloom_hashes"] > 0
        assert composite_payload["membership_index"]["bloom_version"] == 1

        coordinator_artifacts = self.mock_coordinator.register_lineage.call_args.kwargs["artifacts"]
        assert coordinator_artifacts == [
            {
                "hashes": [{"algorithm": "blake3", "digest": primitive_digest}],
                "size": 7,
                "source_type": None,
                "session_hash": "session-hash-123",
            }
        ]

    def test_register_prepared_lineage_excludes_composite_leaf_hashes_from_label_sync(
        self, tmp_path: Path
    ) -> None:
        """A composite leaf/component has no session-scoped edge on GLaaS (see
        ``view_edges.resolve_view_edges_for_job``), so syncing labels for it 404s
        (``Artifact not found in session``). ``composite_leaf_hashes`` must be excluded
        from the label sync payload while remaining in ordinary artifact registration.
        """
        plain_digest = "a" * 64
        leaf_digest = "b" * 64

        lineage = _lineage_data(
            jobs=[{"id": 1, "job_uid": "job-1", "step_number": 1, "timestamp": 1.0}],
            artifacts=[
                {
                    "id": "plain-1",
                    "hash": plain_digest,
                    "hashes": [{"algorithm": "blake3", "digest": plain_digest}],
                    "size": 7,
                    "source_type": "local",
                },
                {
                    "id": "leaf-1",
                    "hash": leaf_digest,
                    "hashes": [{"algorithm": "blake3", "digest": leaf_digest}],
                    "size": 3,
                    "source_type": "local",
                },
            ],
            artifact_hashes={plain_digest, leaf_digest},
        )

        with (
            patch(
                "roar.application.publish.register_execution.create_database_context"
            ) as mock_ctx,
            patch("roar.application.publish.register_execution.config_get", return_value=False),
            patch(
                "roar.application.publish.register_execution.register_publish_lineage"
            ) as mock_register_publish_lineage,
        ):
            mock_db = MagicMock()
            mock_db.__enter__ = MagicMock(return_value=mock_db)
            mock_db.__exit__ = MagicMock(return_value=None)
            mock_ctx.return_value = mock_db
            mock_register_publish_lineage.return_value = BatchRegistrationResult(
                session_registered=True,
                jobs_created=1,
                jobs_failed=0,
                artifacts_registered=2,
                artifacts_failed=0,
                links_created=1,
                links_failed=0,
                errors=[],
            )

            result = self.service.register_prepared_lineage(
                lineage=lineage,
                roar_dir=tmp_path / ".roar",
                artifact_hash=plain_digest,
                dry_run=False,
                as_blake3=False,
                skip_confirmation=False,
                confirm_callback=None,
                prepared=_prepared_execution(tmp_path),
                composite_leaf_hashes=frozenset({leaf_digest}),
            )

        assert result.success is True
        mock_register_publish_lineage.assert_called_once()
        call_kwargs = mock_register_publish_lineage.call_args.kwargs

        label_hashes = {a["hash"] for a in call_kwargs["label_artifacts"]}
        assert label_hashes == {plain_digest}

        staged_digests = {
            hash_row["digest"] for a in call_kwargs["artifacts"] for hash_row in a["hashes"]
        }
        assert staged_digests == {plain_digest, leaf_digest}


class TestRegisterServiceGitUrlRedaction:
    """The session-level git URL must be redacted before it reaches GLaaS."""

    def test_register_prepared_lineage_sends_redacted_git_context(self, tmp_path: Path) -> None:
        from roar.core.interfaces.registration import SessionRegistrationResult
        from roar.filters.omit import OmitFilter

        mock_coordinator = MagicMock()
        mock_coordinator.register_lineage_under_registration_session.return_value = (
            BatchRegistrationResult(
                session_registered=True,
                jobs_created=1,
                jobs_failed=0,
                artifacts_registered=1,
                artifacts_failed=0,
                links_created=0,
                links_failed=0,
                errors=[],
            )
        )
        mock_coordinator.session_service.finalize_registration_session.return_value = (
            SessionRegistrationResult(
                success=True,
                session_hash="final-hash",
                session_url="https://glaas.example/dag/final-hash",
            )
        )

        service = RegisterService(
            glaas_client=MagicMock(),
            coordinator=mock_coordinator,
            omit_filter=OmitFilter({}),
        )

        prepared = PreparedRegisterExecution(
            git_context=GitContext(
                repo="https://user:supersecrettoken123@github.com/org/repo.git",
                commit="abc123def456",
                branch="main",
            ),
            session_id=None,
            session_hash="session-hash-123",
            session_url=None,
            git_tag_name=None,
            git_tag_repo_root=None,
            registration_session_id="rs-123",
        )

        with patch("roar.application.publish.register_execution.config_get", return_value=False):
            result = service.register_prepared_lineage(
                lineage=LineageData(
                    jobs=[
                        {
                            "id": 1,
                            "job_uid": "job-1",
                            "step_number": 1,
                            "timestamp": 10.0,
                            "command": "python train.py",
                        }
                    ],
                    artifacts=[{"id": "a1", "hash": "f" * 64}],
                    artifact_hashes={"f" * 64},
                    pipeline={"id": 1},
                ),
                roar_dir=tmp_path / ".roar",
                artifact_hash="f" * 64,
                dry_run=False,
                as_blake3=False,
                skip_confirmation=True,
                confirm_callback=None,
                prepared=prepared,
            )

        assert result.success is True
        assert "git_url_creds" in result.secrets_detected

        batch_call = mock_coordinator.register_lineage_under_registration_session.call_args
        assert (
            batch_call.kwargs["git_context"].repo
            == "https://user:[REDACTED]@github.com/org/repo.git"
        )

        finalize_call = mock_coordinator.session_service.finalize_registration_session.call_args
        assert (
            finalize_call.kwargs["git_context"].repo
            == "https://user:[REDACTED]@github.com/org/repo.git"
        )
        assert "supersecrettoken123" not in str(finalize_call)
