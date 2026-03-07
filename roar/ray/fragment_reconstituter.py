from __future__ import annotations

import base64
import json
import os
import sqlite3
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .collector import _resolve_active_session_context, collect_fragments


def _get_logger():
    from roar.core.logging import get_logger

    return get_logger()


@dataclass(frozen=True)
class ReconstitutionResult:
    jobs_merged: int = 0
    artifacts_merged: int = 0
    fragments_processed: int = 0


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
    def _deduplicate_fragments(fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Deduplicate artifacts across capture methods within each task.

        Priority: proxy > native > python.
        For the same (task, path, kind) tuple, keep the highest-priority capture.
        """
        priority = {"proxy": 3, "native": 2, "python": 1, "tracer": 1}
        winners: dict[tuple[str, str, str], tuple[int, dict[str, Any]]] = {}

        for fragment_index, fragment in enumerate(fragments):
            task_key = str(
                fragment.get("job_uid")
                or fragment.get("ray_task_id")
                or f"fragment:{fragment_index}"
            )
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
                    if existing is None or current_priority > existing[0]:
                        winners[dedup_key] = (current_priority, item)

        for fragment_index, fragment in enumerate(fragments):
            task_key = str(
                fragment.get("job_uid")
                or fragment.get("ray_task_id")
                or f"fragment:{fragment_index}"
            )
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
