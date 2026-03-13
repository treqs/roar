"""Focused unit tests for RegisterService registration mechanics."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from roar.core.interfaces.lineage import LineageData
from roar.core.interfaces.registration import (
    BatchRegistrationResult,
    GitContext,
    SessionRegistrationResult,
)
from roar.services.registration.register_service import RegisterResult, RegisterService


def _git_context(
    repo_root: Path,
    *,
    repo: str | None = "https://github.com/test/repo",
    commit: str = "abc123def456",
    branch: str | None = "main",
) -> GitContext:
    return GitContext(repo=repo, commit=commit, branch=branch)


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


class TestRegisterResult:
    def test_success_result(self) -> None:
        result = RegisterResult(
            success=True,
            session_hash="abc123",
            jobs_registered=3,
            artifacts_registered=5,
            links_created=8,
        )
        assert result.success is True
        assert result.session_hash == "abc123"
        assert result.jobs_registered == 3
        assert result.artifacts_registered == 5
        assert result.links_created == 8
        assert result.error is None

    def test_error_result(self) -> None:
        result = RegisterResult(success=False, error="Something went wrong")
        assert result.success is False
        assert result.error == "Something went wrong"


class TestRegisterService:
    @pytest.fixture
    def mock_glaas_client(self):
        client = MagicMock()
        client.health_check.return_value = (True, None)
        client.is_configured.return_value = True
        return client

    @pytest.fixture
    def mock_coordinator(self):
        coordinator = MagicMock()
        coordinator.register_lineage.return_value = BatchRegistrationResult(
            session_registered=True,
            jobs_created=1,
            jobs_failed=0,
            artifacts_registered=1,
            artifacts_failed=0,
            links_created=2,
            links_failed=0,
            errors=[],
        )
        return coordinator

    @pytest.fixture
    def mock_session_service(self):
        service = MagicMock()
        service.compute_session_hash.return_value = "session-hash-123"
        service.register.return_value = SessionRegistrationResult(
            success=True,
            session_hash="session-hash-123",
            session_url="https://glaas.example/dag/session-hash-123",
        )
        return service

    @pytest.fixture
    def service(self, mock_glaas_client, mock_coordinator, mock_session_service):
        return RegisterService(
            glaas_client=mock_glaas_client,
            coordinator=mock_coordinator,
            session_service=mock_session_service,
        )

    def test_order_jobs_for_registration_puts_parent_before_child(self, service) -> None:
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

        ordered = service._order_jobs_for_registration([child, parent])

        assert [job["job_uid"] for job in ordered] == ["parent-uid", "child-uid"]

    def test_normalize_jobs_for_registration_maps_unresolved_ray_parent_to_submit_job(
        self, service
    ) -> None:
        submit_job = {
            "id": 1,
            "job_uid": "local-submit",
            "step_number": 1,
            "timestamp": 10.0,
            "command": "ray job submit --address http://localhost:8265 -- python main.py",
            "job_type": None,
        }
        phase_job = {
            "id": 2,
            "job_uid": "phase-job",
            "parent_job_uid": "remote-driver",
            "step_number": 4,
            "timestamp": 40.0,
            "command": "ray_task:evaluation",
            "job_type": "ray_task",
        }

        normalized = service._normalize_jobs_for_registration([phase_job, submit_job])

        jobs_by_uid = {job["job_uid"]: job for job in normalized}
        assert jobs_by_uid["phase-job"]["parent_job_uid"] == "local-submit"

    def test_normalize_jobs_for_registration_filters_known_ray_noise_jobs(self, service) -> None:
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

        normalized = service._normalize_jobs_for_registration(
            [submit_job, noise_job, phase_job, shutdown_job]
        )

        assert [job["job_uid"] for job in normalized] == ["local-submit", "phase-job"]
        jobs_by_uid = {job["job_uid"]: job for job in normalized}
        assert jobs_by_uid["phase-job"]["parent_job_uid"] == "local-submit"

    def test_register_result_includes_artifact_hash(self) -> None:
        result = RegisterResult(
            success=True,
            session_hash="session123",
            artifact_hash="artifact456",
            jobs_registered=2,
            artifacts_registered=3,
            links_created=5,
        )
        assert result.artifact_hash == "artifact456"

    def test_register_collected_lineage_dry_run(self, service, tmp_path) -> None:
        with (
            patch.object(service, "_get_git_context", return_value=_git_context(tmp_path)),
            patch("roar.services.registration.register_service.config_get", return_value=False),
        ):
            result = service.register_collected_lineage(
                lineage=_lineage_data(
                    jobs=[{"id": 1, "job_uid": "job1"}, {"id": 2, "job_uid": "job2"}],
                    artifacts=[{"id": "a1"}, {"id": "a2"}, {"id": "a3"}],
                    artifact_hashes={"hash1", "hash2", "hash3"},
                ),
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
                session_id=1,
                artifact_hash="hash1",
                dry_run=True,
                as_blake3=False,
                skip_confirmation=False,
                confirm_callback=None,
            )

        assert result.success is True
        assert result.jobs_registered == 2
        assert result.artifacts_registered == 3
        service._coordinator.register_lineage.assert_not_called()

    def test_register_collected_lineage_dry_run_filters_known_ray_noise_jobs(self, service, tmp_path):
        with (
            patch.object(service, "_get_git_context", return_value=_git_context(tmp_path)),
            patch("roar.services.registration.register_service.config_get", return_value=False),
        ):
            result = service.register_collected_lineage(
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
                cwd=tmp_path,
                session_id=1,
                artifact_hash="hash1",
                dry_run=True,
                as_blake3=False,
                skip_confirmation=False,
                confirm_callback=None,
            )

        assert result.success is True
        assert result.jobs_registered == 2
        assert result.links_created == 3
        service._coordinator.register_lineage.assert_not_called()

    def test_register_collected_lineage_glaas_health_check_fails(
        self, service, tmp_path, mock_glaas_client
    ) -> None:
        from roar.core.exceptions import GlaasConnectionError

        mock_glaas_client.health_check.side_effect = GlaasConnectionError("Connection refused")

        with (
            patch.object(service, "_get_git_context", return_value=_git_context(tmp_path)),
            patch("roar.services.registration.register_service.config_get", return_value=False),
        ):
            result = service.register_collected_lineage(
                lineage=_lineage_data(),
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
                session_id=1,
                artifact_hash="hash1",
                dry_run=False,
                as_blake3=False,
                skip_confirmation=False,
                confirm_callback=None,
            )

        assert result.success is False
        assert "health" in result.error.lower() or "connection" in result.error.lower()

    def test_register_collected_lineage_glaas_not_configured(self, tmp_path) -> None:
        mock_client = MagicMock()
        mock_client.is_configured.return_value = False
        service = RegisterService(glaas_client=mock_client, session_service=MagicMock())
        service._session_service.compute_session_hash.return_value = "session_hash_123"

        with (
            patch.object(service, "_get_git_context", return_value=_git_context(tmp_path)),
            patch("roar.services.registration.register_service.config_get", return_value=False),
        ):
            result = service.register_collected_lineage(
                lineage=_lineage_data(),
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
                session_id=1,
                artifact_hash="hash1",
                dry_run=False,
                as_blake3=False,
                skip_confirmation=False,
                confirm_callback=None,
            )

        assert result.success is False
        assert "not configured" in result.error.lower() or "glaas" in result.error.lower()

    def test_register_collected_lineage_dirty_repo_fails(self, service, tmp_path) -> None:
        with (
            patch.object(service, "_get_git_context", return_value=_git_context(tmp_path)),
            patch(
                "roar.services.registration.register_service.ensure_clean_publish_repo",
                side_effect=ValueError(
                    "Cannot register with uncommitted changes. Commit your changes first."
                ),
            ),
            patch("roar.services.registration.register_service.config_get", return_value=True),
        ):
            result = service.register_collected_lineage(
                lineage=_lineage_data(),
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
                session_id=1,
                artifact_hash="hash1",
                dry_run=False,
                as_blake3=False,
                skip_confirmation=False,
                confirm_callback=None,
            )

        assert result.success is False
        assert "uncommitted" in result.error.lower()

    def test_register_collected_lineage_creates_git_tag(
        self, service, tmp_path, mock_coordinator
    ) -> None:
        with (
            patch.object(service, "_get_git_context", return_value=_git_context(tmp_path)),
            patch(
                "roar.services.registration.register_service.ensure_clean_publish_repo",
                return_value=MagicMock(),
            ),
            patch(
                "roar.services.registration.register_service.create_publish_git_tag",
                return_value=(True, None),
            ) as mock_create_tag,
            patch("roar.services.registration.register_service.create_database_context") as mock_ctx,
            patch("roar.services.registration.register_service.config_get") as mock_config,
        ):
            mock_db = MagicMock()
            mock_db.__enter__ = MagicMock(return_value=mock_db)
            mock_db.__exit__ = MagicMock(return_value=None)
            mock_ctx.return_value = mock_db

            def config_side_effect(key):
                if key == "registration.tagging.enabled":
                    return True
                if key == "registration.omit":
                    return {"enabled": False}
                return None

            mock_config.side_effect = config_side_effect

            result = service.register_collected_lineage(
                lineage=_lineage_data(),
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
                session_id=1,
                artifact_hash="hash1",
                dry_run=False,
                as_blake3=False,
                skip_confirmation=False,
                confirm_callback=None,
            )

        assert result.success is True
        mock_coordinator.register_lineage.assert_called_once()
        mock_create_tag.assert_called_once_with(tmp_path, "roar/abc123de")

    def test_register_collected_lineage_tagging_disabled_skips_dirty_check(self, service, tmp_path):
        with (
            patch.object(service, "_get_git_context", return_value=_git_context(tmp_path)),
            patch(
                "roar.services.registration.register_service.ensure_clean_publish_repo"
            ) as ensure_clean,
            patch("roar.services.registration.register_service.config_get", return_value=False),
        ):
            result = service.register_collected_lineage(
                lineage=_lineage_data(),
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
                session_id=1,
                artifact_hash="hash1",
                dry_run=True,
                as_blake3=False,
                skip_confirmation=False,
                confirm_callback=None,
            )

        assert result.success is True
        ensure_clean.assert_not_called()

    def test_register_collected_lineage_uses_local_repo_uri_when_remote_missing(
        self, service, tmp_path, mock_coordinator
    ) -> None:
        with (
            patch(
                "roar.services.registration.register_service.resolve_publish_git_context",
                return_value=_git_context(
                    tmp_path,
                    repo=tmp_path.resolve().as_uri(),
                ),
            ),
            patch("roar.services.registration.register_service.create_database_context") as mock_ctx,
            patch("roar.services.registration.register_service.config_get") as mock_config,
        ):
            mock_db = MagicMock()
            mock_db.__enter__ = MagicMock(return_value=mock_db)
            mock_db.__exit__ = MagicMock(return_value=None)
            mock_ctx.return_value = mock_db

            def config_side_effect(key):
                if key == "registration.tagging.enabled":
                    return False
                if key == "registration.omit":
                    return {"enabled": False}
                return None

            mock_config.side_effect = config_side_effect

            result = service.register_collected_lineage(
                lineage=_lineage_data(),
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
                session_id=1,
                artifact_hash="hash1",
                dry_run=False,
                as_blake3=False,
                skip_confirmation=False,
                confirm_callback=None,
            )

        assert result.success is True
        assert mock_coordinator.register_lineage.called
        registered_git_context = service._session_service.register.call_args.args[1]
        assert registered_git_context.repo == tmp_path.resolve().as_uri()

    def test_build_lineage_membership_index_payload_rebuilds_full_component_bloom(self, service):
        payload = service._build_lineage_membership_index_payload(
            membership_index={
                "total_components": 2,
                "stored_components": 2,
                "bloom_filter_base64": "AQIDBA==",
                "bloom_bits": 2048,
                "bloom_hashes": 12,
                "bloom_version": 1,
            },
            component_count_total=2,
            components=[
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
            ],
        )

        assert payload["total_components"] == 2
        assert payload["stored_components"] == 2
        assert payload["bloom_filter_base64"] != "AQIDBA=="
        assert payload["bloom_bits"] > 0
        assert payload["bloom_hashes"] > 0
        assert payload["bloom_version"] == 1

    def test_register_collected_lineage_preregisters_composites_before_batch_registration(
        self, service, tmp_path, mock_glaas_client, mock_coordinator
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

        mock_glaas_client.register_composite_artifact.return_value = (
            {"artifact_id": "srv-comp-1", "created": True},
            None,
        )

        with (
            patch("roar.services.registration.register_service.create_database_context") as mock_ctx,
            patch("roar.services.registration.register_service.config_get", return_value=False),
            patch.object(service, "_get_git_context", return_value=_git_context(tmp_path)),
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

            result = service.register_collected_lineage(
                lineage=lineage,
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
                session_id=1,
                artifact_hash=primitive_digest,
                dry_run=False,
                as_blake3=False,
                skip_confirmation=False,
                confirm_callback=None,
            )

        assert result.success is True
        assert result.artifacts_registered == 2

        composite_payload = mock_glaas_client.register_composite_artifact.call_args.args[0]
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

        coordinator_artifacts = mock_coordinator.register_lineage.call_args.kwargs["artifacts"]
        assert coordinator_artifacts == [
            {
                "hashes": [{"algorithm": "blake3", "digest": primitive_digest}],
                "size": 7,
                "source_type": None,
                "session_hash": "session-hash-123",
            }
        ]
