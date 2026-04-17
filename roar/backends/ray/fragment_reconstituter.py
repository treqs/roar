from __future__ import annotations

import base64
import json
import mimetypes
import os
import sqlite3
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from roar.application.system_labels import refresh_job_system_labels
from roar.db.context import create_database_context

from .collector import _resolve_active_session_context, collect_fragments
from .fragment import derive_fragment_fallback_identity, derive_task_identity
from .s3_key_paths import parse_s3_key_placeholder, s3_object_key


def _get_logger():
    from roar.core.logging import get_logger

    return get_logger()


@dataclass(frozen=True)
class ReconstitutionResult:
    jobs_merged: int = 0
    artifacts_merged: int = 0
    fragments_processed: int = 0


@dataclass(frozen=True)
class _CompositeOutputLeaf:
    path: str
    artifact_id: str
    digest: str
    algorithm: str
    size: int


class FragmentReconstituter:
    def __init__(
        self,
        session_id: str,
        token: str,
        glaas_url: str,
        roar_db_path: Path,
    ) -> None:
        self._session_id = session_id
        self._token = token
        self._glaas_url = glaas_url.rstrip("/")
        self._roar_db_path = roar_db_path

    def reconstitute(self) -> ReconstitutionResult:
        batches = self._fetch_batches()
        if not batches:
            return ReconstitutionResult()

        try:
            key = bytes.fromhex(self._token)
        except ValueError as exc:
            _get_logger().warning(
                "Invalid fragment token for session %s: %s",
                self._session_id,
                exc,
            )
            return ReconstitutionResult()

        fragments: list[dict[str, Any]] = []
        for batch in batches:
            fragments.extend(self._decrypt_batch(batch, key))

        if not fragments:
            return ReconstitutionResult()

        fragments = self._resolve_s3_key_placeholders(fragments)
        fragments = self._drop_proxy_fallback_duplicates(fragments)
        fragments = self._deduplicate_fragments(fragments)
        jobs_before, artifacts_before = self._count_local_rows()
        session_id, step_number = _resolve_active_session_context(str(self._roar_db_path))
        driver_job_uid = str(os.environ.get("ROAR_JOB_ID", "")).strip() or None

        try:
            collect_fragments(
                fragments=fragments,
                project_dir=str(self._project_dir()),
                driver_job_uid=driver_job_uid,
                session_id=session_id,
                step_number=step_number,
            )
        except Exception as exc:
            _get_logger().warning(
                "Failed to merge reconstituted fragments for session %s: %s",
                self._session_id,
                exc,
            )
            return ReconstitutionResult(fragments_processed=len(fragments))

        self._materialize_reconstituted_composites(fragments)
        jobs_after, artifacts_after = self._count_local_rows()
        return ReconstitutionResult(
            jobs_merged=max(0, jobs_after - jobs_before),
            artifacts_merged=max(0, artifacts_after - artifacts_before),
            fragments_processed=len(fragments),
        )

    def _fetch_batches(self) -> list[dict[str, Any]]:
        request = urllib.request.Request(
            url=f"{self._glaas_url}/api/v1/fragments/sessions/{self._session_id}/fragments",
            headers={"x-roar-fragment-token": self._token},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            _get_logger().warning(
                "Failed to fetch fragments for session %s: %s",
                self._session_id,
                exc,
            )
            return []

        rows = payload.get("data", {}).get("fragments", payload.get("fragments"))
        if not isinstance(rows, list):
            _get_logger().warning(
                "Invalid fragment response for session %s: missing fragments list",
                self._session_id,
            )
            return []

        batches = [item for item in rows if isinstance(item, dict)]
        return sorted(batches, key=self._sequence_key)

    def _decrypt_batch(self, batch: dict[str, Any], key: bytes) -> list[dict[str, Any]]:
        encrypted_batch = batch.get("encrypted_batch")
        sequence = batch.get("sequence")
        if not isinstance(encrypted_batch, str) or not encrypted_batch:
            _get_logger().warning(
                "Skipping fragment batch for session %s sequence %s: missing encrypted batch",
                self._session_id,
                sequence,
            )
            return []

        try:
            payload = base64.b64decode(encrypted_batch)
            if len(payload) <= 12:
                raise ValueError("payload too short")
            nonce = payload[:12]
            ciphertext = payload[12:]
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
            decoded = json.loads(plaintext.decode("utf-8"))
            if not isinstance(decoded, list):
                raise ValueError("decrypted payload is not a list")
        except Exception as exc:
            _get_logger().warning(
                "Skipping undecryptable fragment batch for session %s sequence %s: %s",
                self._session_id,
                sequence,
                exc,
            )
            return []

        return [item for item in decoded if isinstance(item, dict)]

    @staticmethod
    def _resolve_s3_key_placeholders(fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        concrete_paths_by_key: dict[str, set[str]] = {}

        for fragment in fragments:
            for list_key in ("reads", "writes"):
                items = fragment.get(list_key, [])
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    path = item.get("path", "")
                    if not isinstance(path, str) or not path.startswith("s3://"):
                        continue
                    object_key = s3_object_key(path)
                    if object_key:
                        concrete_paths_by_key.setdefault(object_key, set()).add(path)

        if not concrete_paths_by_key:
            return fragments

        for fragment in fragments:
            for list_key in ("reads", "writes"):
                items = fragment.get(list_key, [])
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    path = item.get("path", "")
                    if not isinstance(path, str):
                        continue
                    placeholder = parse_s3_key_placeholder(path)
                    if placeholder is None:
                        continue
                    _bucket_hint, object_key = placeholder
                    matches = concrete_paths_by_key.get(object_key, set())
                    if len(matches) == 1:
                        item["path"] = next(iter(matches))

        return fragments

    @staticmethod
    def _deduplicate_fragments(fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Deduplicate artifacts across capture methods within each task.

        Priority: proxy > native > python.
        For the same (task, path, kind) tuple, keep the highest-priority capture,
        but merge richer metadata from lower-priority duplicates when the winner
        does not carry it.
        """
        priority = {"proxy": 3, "native": 2, "python": 1, "tracer": 1}
        winners: dict[tuple[str, str, str], tuple[int, dict[str, Any]]] = {}

        for fragment_index, fragment in enumerate(fragments):
            task_key = FragmentReconstituter._fragment_task_key(fragment, fragment_index)
            for list_key in ("reads", "writes"):
                items = fragment.get(list_key, [])
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    path = item.get("path", "")
                    if not isinstance(path, str) or not path:
                        continue
                    method = str(item.get("capture_method", "python"))
                    current_priority = priority.get(method, 0)
                    dedup_key = (task_key, list_key, path)
                    existing = winners.get(dedup_key)
                    if existing is None:
                        winners[dedup_key] = (current_priority, item)
                    elif current_priority > existing[0]:
                        FragmentReconstituter._merge_ref_metadata(item, existing[1])
                        winners[dedup_key] = (current_priority, item)
                    else:
                        FragmentReconstituter._merge_ref_metadata(existing[1], item)

        for fragment_index, fragment in enumerate(fragments):
            task_key = FragmentReconstituter._fragment_task_key(fragment, fragment_index)
            for list_key in ("reads", "writes"):
                if list_key not in fragment:
                    continue
                items = fragment[list_key]
                if not isinstance(items, list):
                    continue

                deduplicated_items: list[dict[str, Any]] = []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    path = item.get("path", "")
                    if not isinstance(path, str) or not path:
                        deduplicated_items.append(item)
                        continue
                    winner = winners.get((task_key, list_key, path))
                    if winner is not None and item is winner[1]:
                        deduplicated_items.append(item)
                fragment[list_key] = deduplicated_items

        return fragments

    @staticmethod
    def _fragment_task_key(fragment: dict[str, Any], fragment_index: int) -> str:
        task_identity = str(fragment.get("task_identity") or "").strip()
        if task_identity:
            return task_identity

        derived = derive_task_identity(
            str(fragment.get("parent_job_uid") or ""),
            str(fragment.get("ray_task_id") or ""),
            str(fragment.get("job_uid") or ""),
        )
        if derived:
            return derived

        fallback = derive_fragment_fallback_identity(fragment)
        if fallback:
            return fallback

        return f"fragment:{fragment_index}"

    @staticmethod
    def _drop_proxy_fallback_duplicates(fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        proxy_function_names = {"s3_proxy", "s3_driver_proxy"}
        task_scoped_s3_refs: set[tuple[str, str]] = set()

        for fragment in fragments:
            function_name = str(fragment.get("function_name") or "")
            if function_name in proxy_function_names:
                continue
            for list_key in ("reads", "writes"):
                items = fragment.get(list_key, [])
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    path = item.get("path", "")
                    if isinstance(path, str) and path.startswith("s3://"):
                        task_scoped_s3_refs.add((list_key, path))

        if not task_scoped_s3_refs:
            return fragments

        for fragment in fragments:
            if str(fragment.get("function_name") or "") not in proxy_function_names:
                continue
            for list_key in ("reads", "writes"):
                items = fragment.get(list_key, [])
                if not isinstance(items, list):
                    continue
                fragment[list_key] = [
                    item
                    for item in items
                    if not (
                        isinstance(item, dict)
                        and isinstance(item.get("path"), str)
                        and item["path"].startswith("s3://")
                        and (list_key, item["path"]) in task_scoped_s3_refs
                    )
                ]

        return fragments

    @staticmethod
    def _merge_ref_metadata(winner: dict[str, Any], candidate: dict[str, Any]) -> None:
        winner_hash = str(winner.get("hash") or "").strip()
        candidate_hash = str(candidate.get("hash") or "").strip()
        if not winner_hash and candidate_hash:
            winner["hash"] = candidate_hash

        winner_algorithm = str(winner.get("hash_algorithm") or "").strip()
        candidate_algorithm = str(candidate.get("hash_algorithm") or "").strip()
        if not winner_algorithm and candidate_algorithm:
            winner["hash_algorithm"] = candidate_algorithm

        winner_size = winner.get("size")
        candidate_size = candidate.get("size")
        if (
            (not isinstance(winner_size, int) or winner_size <= 0)
            and isinstance(candidate_size, int)
            and candidate_size > 0
        ):
            winner["size"] = candidate_size

    @staticmethod
    def _sequence_key(batch: dict[str, Any]) -> int:
        sequence = batch.get("sequence")
        if sequence is None:
            return 2**31 - 1
        try:
            return int(sequence)
        except (TypeError, ValueError):
            return 2**31 - 1

    def _project_dir(self) -> Path:
        parent = self._roar_db_path.parent
        if parent.name == ".roar":
            return parent.parent
        return parent

    def _materialize_reconstituted_composites(self, fragments: list[dict[str, Any]]) -> None:
        job_uids = sorted(
            {
                str(fragment.get("job_uid") or "").strip()
                for fragment in fragments
                if isinstance(fragment, dict)
            }
            - {""}
        )
        if not job_uids:
            return

        try:
            from roar.application.publish.composite_builder import (
                CompositeArtifactBuilder,
                CompositeLeaf,
            )
            from roar.execution.recording import (
                DatasetIdentifierInferer,
                ExecutionJobRecorder,
                RunCompositeMaterializationConfig,
            )
        except Exception:
            return

        config = RunCompositeMaterializationConfig.from_repo_root(str(self._project_dir()))
        if not config.enabled:
            return

        inferer = DatasetIdentifierInferer()
        builder = CompositeArtifactBuilder()

        try:
            with create_database_context(self._roar_db_path.parent) as db_ctx:
                for job_uid in job_uids:
                    job = db_ctx.jobs.get_by_uid(job_uid)
                    if not isinstance(job, dict):
                        continue

                    job_id = int(job["id"])
                    outputs = db_ctx.jobs.get_outputs(job_id)
                    leaves = self._extract_composite_output_leaves(outputs)
                    if len(leaves) < config.min_components:
                        continue

                    dataset_identifiers = inferer.infer(
                        [leaf.path for leaf in leaves],
                        repo_root=str(self._project_dir()),
                        min_confidence=0.0,
                    )
                    grouped_roots = self._group_composite_roots(
                        leaves=leaves,
                        dataset_identifiers=dataset_identifiers,
                        config=config,
                    )
                    if not grouped_roots:
                        continue

                    materialized: list[dict[str, Any]] = []
                    for root_path, members in grouped_roots.items():
                        artifact_id_by_path = {
                            member.path: member.artifact_id for member in members
                        }
                        composite_leaves = [
                            CompositeLeaf(
                                relative_path=self._relative_path_for_root(member.path, root_path),
                                digest=member.digest,
                                size=member.size,
                                component_type=mimetypes.guess_type(member.path)[0],
                                leaf_kind="file",
                                component_algorithm=member.algorithm,
                            )
                            for member in sorted(members, key=lambda item: item.path)
                        ]
                        if len(composite_leaves) < config.min_components:
                            continue

                        composite = builder.build_for_leaves(
                            root_path=root_path,
                            leaves=composite_leaves,
                            session_hash="",
                            source_type=self._source_type_for_root(root_path),
                        )
                        if composite is None:
                            continue

                        metadata = json.dumps(
                            {
                                "composite": {
                                    "root_path": composite.root_path,
                                    "component_count_total": composite.component_count_total,
                                    "component_count_stored": composite.component_count_stored,
                                }
                            }
                        )
                        artifact_id, _created = db_ctx.artifacts.register(
                            hashes={"composite-blake3": composite.digest},
                            size=int(composite.payload.get("size") or 0),
                            path=composite.root_path,
                            source_type=composite.payload.get("source_type"),
                            metadata=metadata,
                        )
                        component_payload = []
                        for component in list(composite.payload.get("components") or []):
                            relative_path = str(component.get("relative_path") or "")
                            component_path = self._path_for_relative_member(
                                root_path, relative_path
                            )
                            component_payload.append(
                                {
                                    **component,
                                    "artifact_id": artifact_id_by_path.get(component_path),
                                }
                            )
                        db_ctx.composites.upsert_details(
                            artifact_id=artifact_id,
                            components=component_payload,
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

                    if materialized:
                        metadata_json = ExecutionJobRecorder._merge_composites_into_metadata_json(
                            job.get("metadata"),
                            materialized,
                        )
                        db_ctx.jobs.update_metadata(job_id, metadata_json)
                        refresh_job_system_labels(db_ctx, job_id=job_id)
        except Exception as exc:
            _get_logger().warning(
                "Failed to materialize composite artifacts during fragment reconstitution for session %s: %s",
                self._session_id,
                exc,
            )

    @staticmethod
    def _extract_composite_output_leaves(
        outputs: list[dict[str, Any]],
    ) -> list[_CompositeOutputLeaf]:
        leaves: list[_CompositeOutputLeaf] = []
        seen_paths: set[str] = set()
        for output in outputs:
            if not isinstance(output, dict):
                continue
            if str(output.get("kind") or "").strip().lower() == "composite":
                continue

            path = str(output.get("path") or output.get("first_seen_path") or "").strip()
            artifact_id = str(output.get("artifact_id") or "").strip()
            if not path or not artifact_id or path in seen_paths:
                continue

            algorithm = ""
            digest = ""
            hashes = output.get("hashes")
            if isinstance(hashes, list):
                for item in hashes:
                    if not isinstance(item, dict):
                        continue
                    raw_algorithm = str(item.get("algorithm") or "").strip().lower()
                    raw_digest = str(item.get("digest") or "").strip().lower()
                    if not raw_algorithm or not FragmentReconstituter._is_hex_digest(raw_digest):
                        continue
                    algorithm = raw_algorithm
                    digest = raw_digest
                    break
            if not algorithm or not digest:
                continue

            try:
                size = max(0, int(output.get("size") or 0))
            except (TypeError, ValueError):
                size = 0

            leaves.append(
                _CompositeOutputLeaf(
                    path=path,
                    artifact_id=artifact_id,
                    digest=digest,
                    algorithm=algorithm,
                    size=size,
                )
            )
            seen_paths.add(path)

        return leaves

    @staticmethod
    def _group_composite_roots(
        *,
        leaves: list[_CompositeOutputLeaf],
        dataset_identifiers: list[dict[str, Any]],
        config: Any,
    ) -> dict[str, list[_CompositeOutputLeaf]]:
        grouped: dict[str, list[_CompositeOutputLeaf]] = {}
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

            dataset_id = str(candidate.get("dataset_id") or "").strip()
            if not dataset_id:
                continue

            confidence_floor = FragmentReconstituter._composite_confidence_floor(candidate, config)
            if float(confidence) < confidence_floor:
                continue

            matches = [
                leaf
                for leaf in leaves
                if leaf.path not in assigned_paths
                and FragmentReconstituter._is_path_under_root(leaf.path, dataset_id)
            ]
            if len(matches) < config.min_components:
                continue

            grouped[dataset_id] = matches
            assigned_paths.update(leaf.path for leaf in matches)
            if len(grouped) >= int(config.max_roots_per_job):
                break

        return grouped

    @staticmethod
    def _is_path_under_root(path: str, root: str) -> bool:
        normalized_root = root.rstrip("/")
        return path == normalized_root or path.startswith(normalized_root + "/")

    @staticmethod
    def _relative_path_for_root(path: str, root: str) -> str:
        normalized_root = root.rstrip("/")
        prefix = normalized_root + "/"
        if path.startswith(prefix):
            return path[len(prefix) :]
        return Path(path).name

    @staticmethod
    def _path_for_relative_member(root: str, relative_path: str) -> str:
        normalized_root = root.rstrip("/")
        if not relative_path:
            return normalized_root
        return f"{normalized_root}/{relative_path.lstrip('/')}"

    @staticmethod
    def _source_type_for_root(root: str) -> str | None:
        if root.startswith("s3://"):
            return "s3"
        if root.startswith("gs://"):
            return "gs"
        return None

    @staticmethod
    def _composite_confidence_floor(candidate: dict[str, Any], config: Any) -> float:
        try:
            configured_floor = float(config.min_confidence)
        except (TypeError, ValueError):
            configured_floor = 0.8

        evidence = candidate.get("evidence")
        if isinstance(evidence, list):
            evidence_set = {str(item) for item in evidence}
            if {"high_cardinality", "shard_cluster"}.issubset(evidence_set):
                return min(configured_floor, 0.5)

        return configured_floor

    @staticmethod
    def _is_hex_digest(value: str) -> bool:
        if not value or len(value) % 2 != 0:
            return False
        try:
            bytes.fromhex(value)
        except ValueError:
            return False
        return True

    def _count_local_rows(self) -> tuple[int, int]:
        if not self._roar_db_path.exists():
            return 0, 0

        conn = sqlite3.connect(self._roar_db_path)
        conn.row_factory = sqlite3.Row
        try:
            jobs_row = conn.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE job_type = 'ray_task'"
            ).fetchone()
            artifacts_row = conn.execute("SELECT COUNT(*) AS count FROM artifacts").fetchone()
            jobs_count = int(jobs_row["count"]) if jobs_row is not None else 0
            artifacts_count = int(artifacts_row["count"]) if artifacts_row is not None else 0
            return jobs_count, artifacts_count
        except Exception as exc:
            _get_logger().warning(
                "Failed to read local merge counters from %s: %s",
                self._roar_db_path,
                exc,
            )
            return 0, 0
        finally:
            conn.close()
