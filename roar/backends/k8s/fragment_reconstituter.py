"""Fetch, decrypt, and merge k8s fragment sessions from GLaaS."""

from __future__ import annotations

import base64
import json
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from roar.backends.k8s.lineage import collect_k8s_fragments, resolve_active_session_context
from roar.integrations.glaas import renew_fragment_session


def _get_logger():
    from roar.core.logging import get_logger

    return get_logger()


@dataclass(frozen=True)
class K8sReconstitutionResult:
    jobs_merged: int = 0
    artifacts_merged: int = 0
    fragments_processed: int = 0


class K8sFragmentReconstituter:
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

    def reconstitute(self) -> K8sReconstitutionResult:
        batches = self._fetch_batches()
        if not batches:
            return K8sReconstitutionResult()

        try:
            key = bytes.fromhex(self._token)
        except ValueError as exc:
            _get_logger().warning(
                "Invalid fragment token for session %s: %s", self._session_id, exc
            )
            return K8sReconstitutionResult()

        fragments: list[dict[str, Any]] = []
        for batch in batches:
            fragments.extend(self._decrypt_batch(batch, key))
        if not fragments:
            return K8sReconstitutionResult()

        fragments = self._deduplicate_by_task_identity(fragments)
        driver_job_uid = next(
            (
                str(fragment.get("parent_job_uid") or "").strip()
                for fragment in fragments
                if str(fragment.get("parent_job_uid") or "").strip()
            ),
            None,
        )

        jobs_before, artifacts_before = self._count_local_rows()
        session_id, step_number = resolve_active_session_context(str(self._roar_db_path))
        try:
            collect_k8s_fragments(
                fragments,
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
            return K8sReconstitutionResult(fragments_processed=len(fragments))

        jobs_after, artifacts_after = self._count_local_rows()
        return K8sReconstitutionResult(
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
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # An expired session 403s on read; renewal is token-authenticated
            # and allowed after expiry, so attach can still recover lineage.
            payload = None
            if exc.code == 403 and renew_fragment_session(
                self._glaas_url, self._session_id, self._token
            ):
                try:
                    with urllib.request.urlopen(request, timeout=10) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                except Exception as retry_exc:
                    exc = retry_exc  # type: ignore[assignment]
            if payload is None:
                _get_logger().warning(
                    "Failed to fetch fragments for session %s: %s", self._session_id, exc
                )
                return []
        except Exception as exc:
            _get_logger().warning(
                "Failed to fetch fragments for session %s: %s", self._session_id, exc
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
            return []

        try:
            payload = base64.b64decode(encrypted_batch)
            if len(payload) <= 12:
                raise ValueError("payload too short")
            plaintext = AESGCM(key).decrypt(payload[:12], payload[12:], None)
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
    def _deduplicate_by_task_identity(fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Coalesce fragments per task identity (batches are sequence-sorted).

        Retries are already attempt-distinct in the identity contract
        (pod uid + restart attempt), so same-identity fragments are either
        duplicate deliveries (stream + bundle) or parts of an oversized
        fragment the streamer split after an HTTP 413. The last fragment's
        scalar fields win; reads/writes are unioned across parts (per-path,
        last ref wins) so splitting never discards lineage references.
        """
        winners: dict[str, dict[str, Any]] = {}
        for index, fragment in enumerate(fragments):
            identity = str(fragment.get("task_identity") or "").strip() or f"fragment:{index}"
            previous = winners.get(identity)
            if previous is None:
                winners[identity] = dict(fragment)
                continue
            merged = dict(fragment)
            for list_key in ("reads", "writes"):
                merged[list_key] = K8sFragmentReconstituter._merge_refs_by_path(
                    previous.get(list_key), fragment.get(list_key)
                )
            winners[identity] = merged
        return list(winners.values())

    @staticmethod
    def _merge_refs_by_path(earlier: Any, later: Any) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        ordered: list[str] = []
        for refs in (earlier, later):
            if not isinstance(refs, list):
                continue
            for ref in refs:
                if not isinstance(ref, dict):
                    continue
                path = str(ref.get("path") or "")
                if not path:
                    continue
                if path not in merged:
                    ordered.append(path)
                merged[path] = ref
        return [merged[path] for path in ordered]

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

    def _count_local_rows(self) -> tuple[int, int]:
        if not self._roar_db_path.exists():
            return 0, 0

        conn = sqlite3.connect(self._roar_db_path)
        conn.row_factory = sqlite3.Row
        try:
            jobs_row = conn.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE job_type = 'k8s_task'"
            ).fetchone()
            artifacts_row = conn.execute("SELECT COUNT(*) AS count FROM artifacts").fetchone()
            jobs_count = int(jobs_row["count"]) if jobs_row is not None else 0
            artifacts_count = int(artifacts_row["count"]) if artifacts_row is not None else 0
            return jobs_count, artifacts_count
        except sqlite3.Error:
            return 0, 0
        finally:
            conn.close()


def create_k8s_fragment_reconstituter(
    session_id: str,
    token: str,
    glaas_url: str,
    roar_db_path: Path,
) -> K8sFragmentReconstituter:
    return K8sFragmentReconstituter(session_id, token, glaas_url, roar_db_path)


__all__ = [
    "K8sFragmentReconstituter",
    "K8sReconstitutionResult",
    "create_k8s_fragment_reconstituter",
]
