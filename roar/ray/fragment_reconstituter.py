from __future__ import annotations

import base64
import json
import sqlite3
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .collector import collect_fragments


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

        jobs_before, artifacts_before = self._count_local_rows()

        try:
            collect_fragments(
                fragments=fragments,
                project_dir=str(self._project_dir()),
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

        rows = payload.get("fragments")
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
