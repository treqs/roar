"""GLaaS client for communicating with the Graph Lineage-as-a-Service server."""

import contextlib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ...publish_auth import PublishAuthContext, load_publish_auth_context
from . import auth as _auth
from .transport import parse_json_response, request_json


def _get_logger():
    from ...core.logging import get_logger

    return get_logger()


def get_glaas_url() -> str | None:
    """Get GLaaS server URL from config or environment."""
    return _auth.get_glaas_url()


def find_ssh_private_key():
    """Compatibility wrapper for auth key discovery."""
    return _auth.find_ssh_private_key()


def find_ssh_pubkey():
    """Compatibility wrapper for auth key discovery."""
    return _auth.find_ssh_pubkey()


def compute_pubkey_fingerprint(pubkey: str) -> str:
    """Compatibility wrapper for SSH key fingerprinting."""
    return _auth.compute_pubkey_fingerprint(pubkey)


def create_signature_payload(
    method: str,
    path: str,
    timestamp: int,
    body_hash: str | None = None,
) -> bytes:
    """Compatibility wrapper for signature payload generation."""
    return _auth.create_signature_payload(method, path, timestamp, body_hash)


def sign_payload(payload: bytes, key_path, key_type: str) -> bytes | None:
    """Compatibility wrapper for payload signing."""
    return _auth.sign_payload(payload, key_path, key_type)


def make_auth_header(
    method: str,
    path: str,
    body: bytes | None = None,
) -> str | None:
    """Compatibility wrapper for auth header generation."""
    return _auth.make_auth_header(method, path, body)


class GlaasClient:
    """Client for interacting with GLaaS server."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        start_dir: str | Path | None = None,
        publish_auth: PublishAuthContext | None = None,
        allow_public_without_binding: bool = False,
    ):
        resolved_base_url = get_glaas_url() if base_url is None else base_url
        self.base_url = resolved_base_url.rstrip("/") if resolved_base_url else None
        self._publish_auth = publish_auth or load_publish_auth_context(
            start_dir,
            allow_public_without_binding=allow_public_without_binding,
        )

    def is_configured(self) -> bool:
        """Check if GLaaS is configured."""
        return self.base_url is not None

    def _parse_json_response(
        self, response_body: str, http_status: int
    ) -> tuple[Any | None, str | None]:
        """Parse JSON response with descriptive error messages."""
        return parse_json_response(response_body, http_status)

    def health_check(self) -> bool:
        """
        Check server health.

        Returns:
            True if server is healthy

        Raises:
            GlaasNotConfiguredError: If GLaaS URL is not configured
            GlaasConnectionError: If connection to server fails
            GlaasApiError: If server returns non-200 status
        """
        from ...core.exceptions import (
            GlaasApiError,
            GlaasConnectionError,
            GlaasNotConfiguredError,
        )

        if not self.base_url:
            raise GlaasNotConfiguredError("GLaaS URL not configured")

        try:
            url = f"{self.base_url}/api/v1/health"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    return True
                raise GlaasApiError(
                    f"Server returned status {resp.status}",
                    status_code=resp.status,
                )
        except urllib.error.URLError as e:
            _get_logger().debug("GLaaS health check connection error: %s", e)
            raise GlaasConnectionError(f"Connection error: {e}") from e

    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
    ) -> tuple[Any | None, str | None]:
        """Make authenticated request. Returns (response_dict, error_message)."""
        if not self.base_url:
            return None, "GLaaS URL not configured"
        return request_json(
            base_url=self.base_url,
            method=method,
            path=path,
            body=body,
            auth_header_factory=self._make_auth_header,
        )

    def _make_auth_header(self, method: str, path: str, body: bytes | None = None) -> str | None:
        if self._publish_auth.access_token:
            return f"Bearer {self._publish_auth.access_token}"
        return make_auth_header(method, path, body)

    def register_artifact(
        self,
        hashes: list,
        size: int,
        source_type: str,
        session_hash: str,
        source_url: str | None = None,
        metadata: str | None = None,
    ) -> tuple[bool, str | None]:
        """
        Register an artifact with GLaaS.

        Args:
            hashes: List of dicts with 'algorithm' and 'digest' keys
                    e.g., [{"algorithm": "blake3", "digest": "abc123..."}]
            size: File size in bytes
            source_type: Source type (e.g., 's3', 'gs', 'https', 'output')
            session_hash: Session hash this artifact belongs to
            source_url: Optional source URL
            metadata: Optional JSON metadata

        Returns (success, error_message).
        """
        body = {
            "hashes": hashes,
            "size": size,
            "source_type": source_type,
            "session_hash": session_hash,
        }
        if source_url:
            body["source_url"] = source_url
        if metadata:
            body["metadata"] = metadata

        _result, error = self._request("POST", "/api/v1/artifacts", body)
        if error:
            return False, error
        return True, None

    def register_artifacts_batch(
        self,
        artifacts: list,
    ) -> tuple[int, int, str | None]:
        """
        Register multiple artifacts with GLaaS in a single request.

        Args:
            artifacts: List of dicts with keys:
                - hashes: List of {algorithm, digest} dicts (required)
                - size: File size in bytes (required)
                - source_type: Source type (required)
                - session_hash: Session hash (required)
                - source_url: Optional source URL
                - metadata: Optional JSON metadata

        Returns (success_count, error_count, error_message).
        """
        if not artifacts:
            return 0, 0, None

        body = {"artifacts": artifacts}
        result, error = self._request("POST", "/api/v1/artifacts/batch", body)
        if error:
            return 0, len(artifacts), error
        if result is None:
            return 0, 0, None
        return result.get("created", 0) + result.get("existing", 0), 0, None

    def register_composite_artifact(
        self,
        payload: dict[str, Any],
    ) -> tuple[dict | None, str | None]:
        """
        Register a composite artifact and its stored component subset.

        Args:
            payload: Composite registration payload for /api/v1/artifacts/composites

        Returns (result, error_message).
        """
        result, error = self._request("POST", "/api/v1/artifacts/composites", payload)
        return result, error

    def get_composite_components(self, hash_prefix: str) -> tuple[dict | None, str | None]:
        """
        Fetch stored component membership rows for a composite artifact.

        Returns (result, error_message).
        """
        result, error = self._request("GET", f"/api/v1/artifacts/{hash_prefix}/components")
        return result, error

    def get_artifact(self, hash_prefix: str) -> dict:
        """
        Look up artifact by hash prefix.

        Args:
            hash_prefix: Hash or hash prefix to look up

        Returns:
            Artifact dict from server

        Raises:
            GlaasError: If request fails (connection, auth, or API error)
        """
        from ...core.exceptions import GlaasApiError

        result, error = self._request("GET", f"/api/v1/artifacts/{hash_prefix}")
        if error:
            # Parse status code from error message if present
            status_code = None
            if error.startswith("HTTP "):
                with contextlib.suppress(IndexError, ValueError):
                    status_code = int(error.split(":")[0].split()[1])
            raise GlaasApiError(error, status_code=status_code)
        return result  # type: ignore[return-value]

    def get_artifact_lineage(
        self, hash_prefix: str, depth: int = 1
    ) -> tuple[dict | None, str | None]:
        """
        Get lineage for an artifact.

        Args:
            hash_prefix: Hash or hash prefix (min 8 chars)
            depth: How many levels to recurse into inputs (default 1, max 10)

        Returns (lineage_dict, error_message).
        """
        path = f"/api/v1/artifacts/{hash_prefix}/lineage"
        if depth > 1:
            path += f"?depth={depth}"
        result, error = self._request("GET", path)
        return result, error

    def register_job(
        self,
        session_hash: str,
        command: str,
        timestamp: float,
        job_uid: str,
        git_commit: str,
        git_branch: str,
        duration_seconds: float,
        exit_code: int,
        job_type: str | None,
        step_number: int,
        metadata: str | None = None,
        parent_job_uid: str | None = None,
    ) -> tuple[int | None, str | None]:
        """
        Register a completed job with GLaaS using session-scoped endpoint.

        Args:
            session_hash: Session this job belongs to (used in URL)
            command: Command that was executed
            timestamp: Unix timestamp of job start
            job_uid: Unique job identifier
            git_commit: Git commit SHA
            git_branch: Git branch name
            duration_seconds: Job duration
            exit_code: Process exit code
            job_type: Type of job (None for run, "build" for build)
            step_number: Step number in session
            metadata: Optional JSON metadata

        Returns (job_id, error_message).
        """
        body = {
            "command": command,
            "timestamp": timestamp,
            "job_uid": job_uid,
            "git_commit": git_commit,
            "git_branch": git_branch,
            "duration_seconds": duration_seconds,
            "exit_code": exit_code,
            "job_type": job_type,
            "step_number": step_number,
        }
        if metadata:
            body["metadata"] = metadata
        if parent_job_uid is not None:
            body["parent_job_uid"] = parent_job_uid

        result, error = self._request("POST", f"/api/v1/sessions/{session_hash}/jobs", body)
        if error:
            return None, error
        if result is None:
            return None, None
        return result.get("id"), None

    def register_jobs_batch(
        self,
        session_hash: str,
        jobs: list,
    ) -> tuple[list, list, str | None]:
        """
        Register multiple jobs with GLaaS in a single request using session-scoped endpoint.

        Args:
            session_hash: Session this batch belongs to (used in URL)
            jobs: List of dicts with keys:
                Required:
                  - command: Command string
                  - timestamp: Unix timestamp
                  - job_uid: Unique job identifier
                  - git_commit: Git commit hash
                  - git_branch: Git branch name
                  - duration_seconds: Job duration
                  - exit_code: Process exit code
                  - job_type: Job type (e.g., 'run', 'build')
                  - step_number: Step order within session
                Optional:
                  - metadata: JSON metadata string

        Returns (job_ids, errors, error_message).
            job_ids: List of server job IDs for successful registrations
            errors: List of error messages for failed registrations
            error_message: Overall error if the request failed entirely
        """
        if not jobs:
            return [], [], None

        body_jobs: list[dict[str, Any]] = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            payload = dict(job)
            if payload.get("parent_job_uid") is None:
                payload.pop("parent_job_uid", None)
            body_jobs.append(payload)

        body = {"jobs": body_jobs}
        result, error = self._request("POST", f"/api/v1/sessions/{session_hash}/jobs/batch", body)
        if error:
            return [], [error] * len(jobs), error
        if result is None:
            return [], [], None
        return result.get("job_ids", []), result.get("errors", []), None

    def get_artifact_dag(self, hash_prefix: str) -> tuple[dict | None, str | None]:
        """
        Get the DAG needed to reproduce an artifact.

        Returns dict with:
            - artifact: the artifact info
            - dag: the DAG info (or None if external)
            - jobs: ordered list of jobs in the DAG
            - external_deps: list of external dependency artifacts
            - is_external: True if artifact has no producing DAG

        Returns (result, error_message).
        """
        result, error = self._request("GET", f"/api/v1/artifacts/{hash_prefix}/dag")
        return result, error

    # -------------------------------------------------------------------------
    # Session Methods
    # -------------------------------------------------------------------------

    def register_fragment_session(
        self,
        session_id: str,
        token_hash: str,
        ttl_seconds: int = 86400,
    ) -> tuple[dict | None, str | None]:
        """Register a temporary fragment-store session."""
        body = {
            "session_id": session_id,
            "token_hash": token_hash,
            "ttl_seconds": ttl_seconds,
        }
        return self._request("POST", "/api/v1/fragments/sessions", body)

    def register_session(
        self,
        session_hash: str,
        git_repo: str,
        git_commit: str,
        git_branch: str,
    ) -> tuple[dict | None, str | None]:
        """
        Register or update a session with GLaaS.

        Returns (session_info, error_message).
        session_info contains: hash, url, created (bool)
        """
        body: dict[str, Any] = {
            "hash": session_hash,
            "git_repo": git_repo,
            "git_commit": git_commit,
            "git_branch": git_branch,
        }
        if self._publish_auth.scope_request:
            body["scope_request"] = dict(self._publish_auth.scope_request)

        result, error = self._request("POST", "/api/v1/sessions", body)
        return result, error

    def sync_labels(
        self,
        labels: list[dict[str, Any]],
    ) -> tuple[dict | None, str | None]:
        """Sync current local label documents for lineage entities."""
        if not labels:
            return {"created": 0, "updated": 0, "unchanged": 0}, None
        return self._request("POST", "/api/v1/labels/sync", {"labels": labels})

    def register_job_inputs(
        self,
        session_hash: str,
        job_uid: str,
        artifacts: list[dict],
    ) -> tuple[dict | None, str | None]:
        """
        Register input artifacts for a job.

        Args:
            session_hash: Session this job belongs to
            job_uid: The job's unique identifier
            artifacts: List of dicts with {artifact_hash, path, byte_ranges?}

        Returns (result, error_message).
        result contains: job_uid, inputs_linked
        """
        body: dict[str, Any] = {"artifacts": self._map_artifacts_for_api(artifacts)}
        result, error = self._request(
            "POST",
            f"/api/v1/sessions/{session_hash}/jobs/{job_uid}/inputs",
            body,
        )
        return result, error

    def register_job_outputs(
        self,
        session_hash: str,
        job_uid: str,
        artifacts: list[dict],
    ) -> tuple[dict | None, str | None]:
        """
        Register output artifacts for a job.

        Args:
            session_hash: Session this job belongs to
            job_uid: The job's unique identifier
            artifacts: List of dicts with {artifact_hash, path, byte_ranges?}

        Returns (result, error_message).
        result contains: job_uid, outputs_linked
        """
        body: dict[str, Any] = {"artifacts": self._map_artifacts_for_api(artifacts)}
        result, error = self._request(
            "POST",
            f"/api/v1/sessions/{session_hash}/jobs/{job_uid}/outputs",
            body,
        )
        return result, error

    @staticmethod
    def _map_artifacts_for_api(artifacts: list[dict]) -> list[dict]:
        """Map internal artifact dicts to the API schema.

        Internally we use ``artifact_hash`` for content identity. Legacy callers
        may still send ``artifact_id`` (historical misnomer). The GLaaS API
        ``jobArtifactBatchSchema`` expects ``hash``.
        """
        mapped: list[dict[str, Any]] = []
        for a in artifacts:
            artifact_hash = a.get("artifact_hash", a.get("artifact_id", a.get("hash", "")))
            entry: dict[str, Any] = {
                "hash": artifact_hash,
                "path": a["path"],
            }
            if "byte_ranges" in a and a["byte_ranges"] is not None:
                entry["byte_ranges"] = a["byte_ranges"]
            mapped.append(entry)
        return mapped

    def get_session(self, session_hash: str) -> tuple[dict | None, str | None]:
        """
        Get session details including jobs.

        Returns (session_info, error_message).
        """
        result, error = self._request("GET", f"/api/v1/sessions/{session_hash}")
        return result, error
