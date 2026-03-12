"""
Job recording helpers for run/build execution.

This module extracts persistence-oriented responsibilities from RunCoordinator:
- metadata and telemetry shaping
- job recording
- stale-step analysis
- proxy S3 artifact registration
- dataset identifier inference from observed paths
"""

from __future__ import annotations

import json
import os
import re
import shlex
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast
from urllib.parse import urlparse

from ...db.context import optional_repo
from ..registration._dataset_label import build_dataset_metadata, find_matching_identifier
from ..transfer import hash_files_blake3
from .dataset_identifier import DatasetIdentifierInferer

if TYPE_CHECKING:
    from ...core.interfaces.run import RunContext
    from ..execution.proxy import S3LogEntry


def _get_logger():
    from ...core.logging import get_logger

    return get_logger()


def collect_telemetry(
    repo_root: str, start_time: float, end_time: float, allow_incomplete: bool = False
) -> dict[str, Any] | None:
    """Collect telemetry from registered providers (best effort)."""
    from ...core.container import get_container

    telemetry_data: dict[str, Any] = {}
    try:
        container = get_container()
        providers = container.get_all_telemetry_providers()
        for name, provider in providers.items():
            if provider.is_available():
                runs = provider.detect_runs(repo_root, start_time, end_time)
                if runs:
                    urls = [run.url for run in runs if run.url]
                    if urls:
                        telemetry_data[name] = urls[0] if len(urls) == 1 else urls
    except Exception as e:
        # Telemetry is best-effort and should never fail the run.
        _get_logger().debug("Failed to collect telemetry: %s", e)

    return telemetry_data if telemetry_data else None


@dataclass(frozen=True)
class RunCompositeMaterializationConfig:
    """Policy for local composite artifact materialization during run recording."""

    enabled: bool = True
    min_confidence: float = 0.80
    min_components: int = 2
    max_roots_per_job: int = 4

    @classmethod
    def from_repo_root(cls, repo_root: str) -> RunCompositeMaterializationConfig:
        try:
            from ...config import load_config

            config = load_config(start_dir=repo_root)
        except Exception:
            return cls()

        composites = config.get("composites", {})
        run_cfg = composites.get("run", {}) if isinstance(composites, dict) else {}
        if not isinstance(run_cfg, dict):
            return cls()

        enabled = bool(run_cfg.get("enabled", True))
        try:
            min_confidence = float(run_cfg.get("min_confidence", 0.80))
        except (TypeError, ValueError):
            min_confidence = 0.80
        min_confidence = max(0.0, min(1.0, min_confidence))

        try:
            min_components = int(run_cfg.get("min_components", 2))
        except (TypeError, ValueError):
            min_components = 2
        min_components = max(2, min_components)

        try:
            max_roots_per_job = int(run_cfg.get("max_roots_per_job", 4))
        except (TypeError, ValueError):
            max_roots_per_job = 4
        max_roots_per_job = max(1, max_roots_per_job)

        return cls(
            enabled=enabled,
            min_confidence=min_confidence,
            min_components=min_components,
            max_roots_per_job=max_roots_per_job,
        )


class StalenessAnalyzer:
    """Compute stale upstream/downstream step numbers for a recorded job."""

    def analyze(self, db_ctx: Any, session_id: int, job_id: int) -> tuple[list[int], list[int]]:
        stale_upstream: list[int] = []
        stale_downstream: list[int] = []

        job = db_ctx.jobs.get(job_id)
        if not job or not job.get("step_number"):
            return stale_upstream, stale_downstream

        step_num = job["step_number"]
        stale = set(db_ctx.session_service.get_stale_steps(session_id))

        # Check stale upstream by walking producer steps for each input artifact.
        job_inputs = db_ctx.jobs.get_inputs(job_id)
        for inp in job_inputs:
            artifact_hash = inp.get("artifact_hash")
            if not artifact_hash:
                continue
            producer_jobs = db_ctx.artifacts.get_jobs(artifact_hash)
            for producer_job in producer_jobs.get("produced_by", []):
                producer_step = db_ctx.sessions.get_step_for_job(session_id, producer_job["id"])
                if (
                    producer_step
                    and producer_step["step_number"] in stale
                    and producer_step["step_number"] not in stale_upstream
                ):
                    stale_upstream.append(producer_step["step_number"])

        # Check stale downstream from current step.
        downstream = db_ctx.session_service.get_downstream_steps(session_id, step_num)
        stale_downstream = [s for s in downstream if s in stale]
        stale_upstream.sort()
        return stale_upstream, stale_downstream


class ProxyArtifactRegistrar:
    """Attach proxy-captured S3 artifacts as job inputs/outputs."""

    _READ_OPS = frozenset({"GetObject"})
    _WRITE_OPS = frozenset({"PutObject", "CompleteMultipartUpload"})
    _TRACKED_OPS = _READ_OPS | _WRITE_OPS

    def register(self, db_ctx: Any, job_id: int, s3_entries: list[S3LogEntry] | None) -> None:
        if not s3_entries:
            return

        for entry in s3_entries:
            if entry.operation not in self._TRACKED_OPS or not entry.etag:
                continue

            s3_url = f"s3://{entry.bucket}/{entry.key}"
            artifact_id, _ = db_ctx.artifacts.register(
                hashes={"etag": entry.etag},
                size=entry.size_bytes or 0,
                path=s3_url,
                source_type="s3",
                source_url=s3_url,
            )
            if entry.operation in self._READ_OPS:
                db_ctx.jobs.add_input(job_id, artifact_id, s3_url, byte_ranges=entry.byte_ranges)
            else:
                db_ctx.jobs.add_output(job_id, artifact_id, s3_url)


class CompositeOutputMaterializer:
    """Persist local composite artifacts for dataset-like output roots."""

    _EXPLICIT_ROOT_CONFIDENCE_ALLOWANCE = 0.25

    def __init__(self, dataset_identifier_inferer: DatasetIdentifierInferer | None = None):
        self._dataset_identifier_inferer = dataset_identifier_inferer or DatasetIdentifierInferer()

    def materialize(
        self,
        db_ctx: Any,
        job_id: int,
        outputs: list[dict[str, Any]],
        dataset_identifiers: list[dict[str, Any]],
        config: RunCompositeMaterializationConfig,
    ) -> list[dict[str, Any]]:
        output_leaves = self._extract_output_leaves(outputs)
        if len(output_leaves) < config.min_components:
            return []

        grouped_roots = self._detect_composite_roots(output_leaves, dataset_identifiers, config)
        if not grouped_roots:
            return []

        composite_repo = cast(Any, optional_repo(db_ctx, "composites"))
        if composite_repo is None:
            return []

        from ..put.composite_builder import CompositeArtifactBuilder
        from ..put.resolver import ResolvedSource

        builder = CompositeArtifactBuilder()
        materialized: list[dict[str, Any]] = []
        for root in sorted(grouped_roots, key=lambda item: str(item)):
            members = grouped_roots[root]
            hashes_by_path = {str(path): digest for path, digest in members}
            resolved_sources: list[ResolvedSource] = []
            for path, _digest in sorted(members, key=lambda item: str(item[0])):
                try:
                    relative_key = path.relative_to(root).as_posix()
                except ValueError:
                    relative_key = path.name
                resolved_sources.append(
                    ResolvedSource(
                        path=path,
                        exists=path.exists(),
                        relative_key=relative_key,
                        source_root=root,
                    )
                )

            composite = builder.build_for_root(
                root_path=root,
                resolved_sources=resolved_sources,
                hashes_by_path=hashes_by_path,
                session_hash="",
                source_type="file",
            )
            if composite is None:
                continue

            meta_dict: dict[str, Any] = {
                "composite": {
                    "root_path": composite.root_path,
                    "component_count_total": composite.component_count_total,
                    "component_count_stored": composite.component_count_stored,
                }
            }
            matching = find_matching_identifier(str(root), dataset_identifiers)
            if matching is not None:
                meta_dict["dataset"] = build_dataset_metadata(matching)
            metadata = json.dumps(meta_dict)
            artifact_id, _created = db_ctx.artifacts.register(
                hashes={"composite-blake3": composite.digest},
                size=int(composite.payload.get("size") or 0),
                path=composite.root_path,
                source_type=composite.payload.get("source_type"),
                metadata=metadata,
            )
            composite_repo.upsert_details(
                artifact_id=artifact_id,
                components=list(composite.payload.get("components") or []),
                component_count_total=composite.component_count_total,
                membership_index=composite.payload.get("membership_index"),
            )
            db_ctx.jobs.add_output(job_id, artifact_id, composite.root_path)
            materialized.append(
                {
                    "local_artifact_id": artifact_id,
                    "root_path": composite.root_path,
                    "hash": composite.digest,
                    "component_count_total": composite.component_count_total,
                    "component_count_stored": composite.component_count_stored,
                }
            )

        return materialized

    @staticmethod
    def _extract_output_leaves(outputs: list[dict[str, Any]]) -> list[tuple[Path, str]]:
        leaves_by_path: dict[str, tuple[Path, str]] = {}
        pending_blake3_paths: list[Path] = []
        for output in outputs:
            path_value = output.get("path") or output.get("first_seen_path")
            if not isinstance(path_value, str):
                continue

            path = Path(path_value)
            if not path.exists() or (not path.is_file() and not path.is_symlink()):
                continue

            digest = None
            hashes = output.get("hashes")
            if isinstance(hashes, list):
                for item in hashes:
                    if isinstance(item, dict) and item.get("algorithm") == "blake3":
                        raw_digest = item.get("digest")
                        if isinstance(raw_digest, str) and raw_digest:
                            digest = raw_digest
                            break
            if digest:
                leaves_by_path[str(path)] = (path, digest)
                continue

            pending_blake3_paths.append(path)

        if pending_blake3_paths:
            computed = hash_files_blake3(pending_blake3_paths)
            for path in pending_blake3_paths:
                digest = computed.get(str(path))
                if digest:
                    leaves_by_path[str(path)] = (path, digest)

        return list(leaves_by_path.values())

    def _detect_composite_roots(
        self,
        output_leaves: list[tuple[Path, str]],
        dataset_identifiers: list[dict[str, Any]],
        config: RunCompositeMaterializationConfig,
    ) -> dict[Path, list[tuple[Path, str]]]:
        grouped: dict[Path, list[tuple[Path, str]]] = {}
        assigned_paths: set[str] = set()

        ranked_candidates = sorted(
            dataset_identifiers,
            key=lambda item: (
                float(item.get("confidence", 0.0))
                if isinstance(item.get("confidence"), (int, float))
                else 0.0
            ),
            reverse=True,
        )
        for candidate in ranked_candidates:
            confidence = candidate.get("confidence")
            if not isinstance(confidence, (int, float)):
                continue
            evidence = candidate.get("evidence")
            has_explicit_hint = isinstance(evidence, list) and "explicit_root_hint" in evidence
            confidence_floor = config.min_confidence
            if has_explicit_hint:
                confidence_floor = max(
                    0.0,
                    config.min_confidence - self._EXPLICIT_ROOT_CONFIDENCE_ALLOWANCE,
                )
            if float(confidence) < confidence_floor:
                continue

            dataset_id = candidate.get("dataset_id")
            if not isinstance(dataset_id, str):
                continue
            parsed = urlparse(dataset_id)
            if parsed.scheme != "file" or not parsed.path:
                continue

            root = Path(parsed.path)
            matches = [
                leaf
                for leaf in output_leaves
                if str(leaf[0]) not in assigned_paths and self._is_path_under_root(leaf[0], root)
            ]
            if len(matches) < config.min_components:
                continue

            grouped[root] = matches
            assigned_paths.update(str(path) for path, _digest in matches)
            if len(grouped) >= config.max_roots_per_job:
                return grouped

        return grouped

    @staticmethod
    def _is_path_under_root(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False


class ExecutionJobRecorder:
    """Persist a traced execution and return reporting payload pieces."""

    _DATASET_HINT_FLAGS: ClassVar[set[str]] = {
        "--input",
        "--input-dir",
        "--input-path",
        "--inputs",
        "--dataset",
        "--dataset-dir",
        "--data-dir",
        "--data-root",
        "--source",
        "--source-dir",
        "--output-dir",
    }
    _LIST_SEPARATORS = re.compile(r"[,\s]+")

    def __init__(
        self,
        telemetry_collector: Callable[[str, float, float, bool], dict[str, Any] | None]
        | None = None,
        staleness_analyzer: StalenessAnalyzer | None = None,
        proxy_artifact_registrar: ProxyArtifactRegistrar | None = None,
        dataset_identifier_inferer: DatasetIdentifierInferer | None = None,
        run_composite_config: RunCompositeMaterializationConfig | None = None,
        composite_materializer: CompositeOutputMaterializer | None = None,
    ) -> None:
        self._telemetry_collector = telemetry_collector or collect_telemetry
        self._staleness_analyzer = staleness_analyzer or StalenessAnalyzer()
        self._proxy_artifact_registrar = proxy_artifact_registrar or ProxyArtifactRegistrar()
        self._dataset_identifier_inferer = dataset_identifier_inferer or DatasetIdentifierInferer()
        self._run_composite_config = run_composite_config
        self._composite_materializer = composite_materializer or CompositeOutputMaterializer(
            dataset_identifier_inferer=self._dataset_identifier_inferer,
        )

    def record(
        self,
        ctx: RunContext,
        prov: dict[str, Any],
        tracer_result: Any,
        start_time: float,
        is_build: bool,
        s3_entries: list[S3LogEntry] | None = None,
        run_job_uid: str | None = None,
    ) -> tuple[int, str, list[dict[str, Any]], list[dict[str, Any]], list[int], list[int]]:
        """Record job and return tuple expected by RunCoordinator."""
        from ...db.context import create_database_context

        if is_build:
            written_files: list[str] = []
            read_files: list[str] = []
        else:
            written_files = self._normalize_paths(prov.get("data", {}).get("written_files", []))
            read_files = self._normalize_paths(prov.get("data", {}).get("read_files", []))

        git_info = prov.get("executables", {}).get("code", {}).get("git", {})
        git_commit = git_info.get("commit")
        git_branch = git_info.get("branch")
        git_repo = git_info.get("remote_url")

        dataset_hint_paths = self._extract_dataset_hint_paths(ctx.command, ctx.repo_root)
        telemetry_json = self._build_telemetry_json(ctx.repo_root, start_time)

        stale_upstream: list[int] = []
        stale_downstream: list[int] = []
        run_composite_config = (
            self._run_composite_config
            or RunCompositeMaterializationConfig.from_repo_root(ctx.repo_root)
        )
        with create_database_context(ctx.roar_dir) as db_ctx:
            session_window_paths = self._collect_session_window_paths(db_ctx)
            metadata_json = self._build_metadata_json(
                ctx,
                prov,
                git_info,
                read_files,
                written_files,
                session_window_paths=session_window_paths,
                dataset_hint_paths=dataset_hint_paths,
            )
            job_id, job_uid = db_ctx.job_recording.record_job(
                command=shlex.join(ctx.command),
                timestamp=start_time,
                job_uid=run_job_uid,
                step_name=ctx.step_name,
                git_repo=git_repo,
                git_commit=git_commit,
                git_branch=git_branch,
                duration_seconds=tracer_result.duration,
                exit_code=tracer_result.exit_code,
                input_files=read_files,
                output_files=written_files,
                metadata=metadata_json,
                execution_backend=ctx.execution_backend,
                execution_role=ctx.execution_role,
                job_type=ctx.job_type,
                repo_root=ctx.repo_root,
                telemetry=telemetry_json,
                hash_algorithms=list(ctx.hash_algorithms),
            )

            # Register proxy artifacts first so downstream output/input queries include them.
            self._proxy_artifact_registrar.register(db_ctx, job_id, s3_entries)

            written_file_info = db_ctx.jobs.get_outputs(job_id)
            read_file_info = db_ctx.jobs.get_inputs(job_id)
            dataset_identifiers = self._extract_dataset_identifiers_from_metadata(metadata_json)
            materialized_composites: list[dict[str, Any]] = []
            if run_composite_config.enabled:
                materialized_composites = self._composite_materializer.materialize(
                    db_ctx=db_ctx,
                    job_id=job_id,
                    outputs=written_file_info,
                    dataset_identifiers=dataset_identifiers,
                    config=run_composite_config,
                )
            if materialized_composites:
                written_file_info = db_ctx.jobs.get_outputs(job_id)
                metadata_json = self._merge_composites_into_metadata_json(
                    metadata_json,
                    materialized_composites,
                )
                self._update_job_metadata(db_ctx, job_id, metadata_json)

            session = db_ctx.sessions.get_active()
            if session:
                stale_upstream, stale_downstream = self._staleness_analyzer.analyze(
                    db_ctx, session["id"], job_id
                )

        return job_id, job_uid, read_file_info, written_file_info, stale_upstream, stale_downstream

    def _build_metadata_json(
        self,
        ctx: RunContext,
        prov: dict[str, Any],
        git_info: dict[str, Any],
        read_files: list[str],
        written_files: list[str],
        session_window_paths: list[str] | None = None,
        dataset_hint_paths: list[str] | None = None,
    ) -> str | None:
        metadata: dict[str, Any] = {}

        if prov.get("executables", {}).get("packages"):
            metadata["packages"] = prov["executables"]["packages"]
        if prov.get("runtime"):
            metadata["runtime"] = prov["runtime"]
        if prov.get("analysis"):
            metadata["analysis"] = prov["analysis"]
        metadata["git"] = git_info

        cwd_relative = self._compute_cwd_relative(ctx.repo_root)
        if cwd_relative is not None:
            metadata["cwd"] = cwd_relative

        inference_paths = [*read_files, *written_files]
        if session_window_paths:
            inference_paths.extend(session_window_paths)
        if dataset_hint_paths:
            inference_paths.extend(dataset_hint_paths)
        dataset_identifiers = self._dataset_identifier_inferer.infer(
            inference_paths, repo_root=ctx.repo_root
        )
        if dataset_identifiers:
            metadata["dataset_identifiers"] = dataset_identifiers

        # Include persistent env vars in metadata for reproduction.
        try:
            from ...config import load_config

            config = load_config(start_dir=ctx.repo_root)
            env_vars = config.get("env", {})
            if isinstance(env_vars, dict) and env_vars:
                metadata["env_vars"] = env_vars
        except Exception:
            pass

        return json.dumps(metadata) if metadata else None

    def _build_telemetry_json(self, repo_root: str, start_time: float) -> str | None:
        telemetry_data = self._telemetry_collector(repo_root, start_time, time.time(), False)
        return json.dumps(telemetry_data) if telemetry_data else None

    @staticmethod
    def _normalize_paths(raw_files: Any) -> list[str]:
        """Normalize provenance file lists to plain path strings."""
        normalized: list[str] = []
        if not isinstance(raw_files, list):
            return normalized
        for item in raw_files:
            if isinstance(item, str):
                normalized.append(item)
            elif isinstance(item, dict) and isinstance(item.get("path"), str):
                normalized.append(item["path"])
        return normalized

    def _extract_dataset_hint_paths(self, command: list[str], repo_root: str) -> list[str]:
        """Extract likely dataset root paths from command-line flags."""
        hints: list[str] = []
        i = 0
        while i < len(command):
            token = command[i]
            if token.startswith("--"):
                if "=" in token:
                    flag, value = token.split("=", 1)
                    if flag in self._DATASET_HINT_FLAGS:
                        hints.extend(self._normalize_hint_value(value, repo_root))
                elif token in self._DATASET_HINT_FLAGS and i + 1 < len(command):
                    value = command[i + 1]
                    if not value.startswith("-"):
                        hints.extend(self._normalize_hint_value(value, repo_root))
                        i += 1
            i += 1

        # Preserve first-seen order
        return [p for p in dict.fromkeys(hints) if p]

    def _normalize_hint_value(self, value: str, repo_root: str) -> list[str]:
        values = [part for part in self._LIST_SEPARATORS.split(value.strip()) if part]
        normalized: list[str] = []
        for part in values:
            if "://" in part:
                normalized.append(part)
                continue

            path = Path(part)
            if not path.is_absolute():
                path = Path(repo_root) / path
            normalized.append(os.path.abspath(str(path)))
        return normalized

    @staticmethod
    def _collect_session_window_paths(
        db_ctx: Any,
        max_steps: int = 2,
        max_paths: int = 256,
    ) -> list[str]:
        """
        Collect recent session I/O paths to provide cross-step context for inference.

        This helps when a single step observes sparse container internals.
        """
        active = db_ctx.sessions.get_active()
        if not active:
            return []

        steps = db_ctx.sessions.get_steps(active["id"])
        numbered_steps = [step for step in steps if isinstance(step.get("step_number"), int)]
        if not numbered_steps:
            return []

        latest_step = max(step["step_number"] for step in numbered_steps)
        lower_bound = max(1, latest_step - max_steps)
        selected_job_ids = [
            step["id"] for step in numbered_steps if step["step_number"] >= lower_bound
        ]

        collected: list[str] = []
        for job_id in selected_job_ids:
            for io in db_ctx.jobs.get_inputs(job_id):
                path = io.get("path") or io.get("first_seen_path")
                if isinstance(path, str) and path:
                    collected.append(path)
                    if len(collected) >= max_paths:
                        return list(dict.fromkeys(collected))
            for io in db_ctx.jobs.get_outputs(job_id):
                path = io.get("path") or io.get("first_seen_path")
                if isinstance(path, str) and path:
                    collected.append(path)
                    if len(collected) >= max_paths:
                        return list(dict.fromkeys(collected))

        return list(dict.fromkeys(collected))

    @staticmethod
    def _compute_cwd_relative(repo_root: str) -> str | None:
        try:
            relative = str(Path.cwd().relative_to(Path(repo_root)))
            return "" if relative == "." else relative
        except ValueError:
            return None

    @staticmethod
    def _extract_dataset_identifiers_from_metadata(
        metadata_json: str | None,
    ) -> list[dict[str, Any]]:
        if not metadata_json:
            return []
        try:
            payload = json.loads(metadata_json)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, dict):
            return []
        dataset_identifiers = payload.get("dataset_identifiers")
        if not isinstance(dataset_identifiers, list):
            return []
        return [item for item in dataset_identifiers if isinstance(item, dict)]

    @staticmethod
    def _merge_composites_into_metadata_json(
        metadata_json: str | None,
        composites: list[dict[str, Any]],
    ) -> str | None:
        if not composites:
            return metadata_json

        metadata: dict[str, Any]
        if metadata_json:
            try:
                parsed = json.loads(metadata_json)
                metadata = parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                metadata = {}
        else:
            metadata = {}

        metadata["composites"] = composites
        return json.dumps(metadata)

    @staticmethod
    def _update_job_metadata(db_ctx: Any, job_id: int, metadata_json: str | None) -> None:
        if metadata_json is None:
            return
        jobs_repo = getattr(db_ctx, "jobs", None)
        if jobs_repo is None or not hasattr(jobs_repo, "update_metadata"):
            return
        with suppress(Exception):
            jobs_repo.update_metadata(job_id, metadata_json)
