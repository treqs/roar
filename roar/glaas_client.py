"""GLaaS client for communicating with the Graph Lineage-as-a-Service server."""

import base64
import contextlib
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _get_logger():
    from .core.di import resolve_or_default
    from .core.interfaces.logger import ILogger
    from .services.logging import NullLogger

    return resolve_or_default(ILogger, NullLogger)


def get_glaas_url() -> str | None:
    """Get GLaaS server URL from config or environment."""
    from .config import config_get

    url = config_get("glaas.url")
    if not url:
        url = os.environ.get("GLAAS_URL")
    return url


def _detect_key_type(key_path: Path) -> str:
    """Detect SSH key type from filename or content."""
    name = key_path.name
    if "ed25519" in name:
        return "ed25519"
    elif "ecdsa" in name:
        return "ecdsa"
    elif "rsa" in name:
        return "rsa"
    # Fallback: check content
    content = key_path.read_text()
    if "ed25519" in content.lower():
        return "ed25519"
    elif "ecdsa" in content.lower():
        return "ecdsa"
    return "rsa"  # default


def find_ssh_private_key() -> tuple[str, Path] | None:
    """Find SSH private key for signing. Returns (key_type, path) or None.

    Priority: ROAR_SSH_KEY env > glaas.key config > ~/.ssh/ default
    """
    from .config import config_get

    # 1. Environment variable
    env_key = os.environ.get("ROAR_SSH_KEY")
    if env_key:
        path = Path(env_key)
        if path.exists():
            key_type = _detect_key_type(path)
            return (key_type, path)

    # 2. Config file
    config_key = config_get("glaas.key")
    if config_key:
        path = Path(config_key)
        if path.exists():
            key_type = _detect_key_type(path)
            return (key_type, path)

    # 3. Default ~/.ssh/ search
    ssh_dir = Path.home() / ".ssh"
    if not ssh_dir.exists():
        return None

    # Prefer Ed25519, then RSA
    key_prefs = [
        ("ed25519", "id_ed25519"),
        ("rsa", "id_rsa"),
        ("ecdsa", "id_ecdsa"),
    ]

    for key_type, key_name in key_prefs:
        key_path = ssh_dir / key_name
        if key_path.exists():
            return (key_type, key_path)

    return None


def find_ssh_pubkey() -> tuple[str, str, Path] | None:
    """Find SSH public key. Returns (key_type, content, path) or None.

    Priority: ROAR_SSH_KEY env > glaas.key config > ~/.ssh/ default
    Derives pubkey path from private key path by adding .pub extension.
    """
    from .config import config_get

    # 1. Environment variable - derive pubkey from private key path
    env_key = os.environ.get("ROAR_SSH_KEY")
    if env_key:
        pubkey_path = Path(env_key + ".pub")
        if pubkey_path.exists():
            content = pubkey_path.read_text().strip()
            parts = content.split()
            if len(parts) >= 2:
                return (parts[0], content, pubkey_path)

    # 2. Config file - derive pubkey from private key path
    config_key = config_get("glaas.key")
    if config_key:
        pubkey_path = Path(config_key + ".pub")
        if pubkey_path.exists():
            content = pubkey_path.read_text().strip()
            parts = content.split()
            if len(parts) >= 2:
                return (parts[0], content, pubkey_path)

    # 3. Default ~/.ssh/ search
    ssh_dir = Path.home() / ".ssh"
    if not ssh_dir.exists():
        return None

    key_prefs = ["id_ed25519.pub", "id_rsa.pub", "id_ecdsa.pub"]

    for key_name in key_prefs:
        key_path = ssh_dir / key_name
        if key_path.exists():
            content = key_path.read_text().strip()
            parts = content.split()
            if len(parts) >= 2:
                return (parts[0], content, key_path)

    return None


def compute_pubkey_fingerprint(pubkey: str) -> str:
    """Compute SHA256 fingerprint of an SSH public key."""
    parts = pubkey.strip().split()
    if len(parts) < 2:
        raise ValueError("Invalid public key format")

    key_data = base64.b64decode(parts[1])
    digest = hashlib.sha256(key_data).digest()
    fingerprint = base64.b64encode(digest).decode().rstrip("=")
    return f"SHA256:{fingerprint}"


def create_signature_payload(
    method: str,
    path: str,
    timestamp: int,
    body_hash: str | None = None,
) -> bytes:
    """Create the payload that gets signed."""
    payload = f"{timestamp}\n{method}\n{path}"
    if body_hash:
        payload += f"\n{body_hash}"
    return payload.encode()


def sign_payload(payload: bytes, key_path: Path, key_type: str) -> bytes | None:
    """
    Sign payload with SSH private key.

    Uses ssh-keygen for signing (available on most systems).
    Returns base64-encoded signature or None on failure.
    """
    import subprocess
    import tempfile

    # Write payload to temp file
    with tempfile.NamedTemporaryFile(mode="wb", delete=False, suffix=".data") as f:
        f.write(payload)
        payload_path = f.name

    sig_path = payload_path + ".sig"

    try:
        # Use ssh-keygen to sign
        # -Y sign: create signature
        # -f: identity file
        # -n: namespace (we use "glaas")
        result = subprocess.run(
            [
                "ssh-keygen",
                "-Y",
                "sign",
                "-f",
                str(key_path),
                "-n",
                "glaas",
                payload_path,
            ],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            return None

        # Read signature file
        if not Path(sig_path).exists():
            return None

        sig_content = Path(sig_path).read_text()

        # Parse SSH signature format
        # Format: -----BEGIN SSH SIGNATURE-----\n<base64>\n-----END SSH SIGNATURE-----
        lines = sig_content.strip().split("\n")
        sig_lines = []
        in_sig = False
        for line in lines:
            if line.startswith("-----BEGIN"):
                in_sig = True
                continue
            if line.startswith("-----END"):
                break
            if in_sig:
                sig_lines.append(line)

        if not sig_lines:
            return None

        # Return the base64 signature data
        sig_b64 = "".join(sig_lines)
        return base64.b64decode(sig_b64)

    except Exception as e:
        _get_logger().debug("Failed to sign payload: %s", e)
        return None
    finally:
        # Cleanup temp files
        with contextlib.suppress(Exception):
            Path(payload_path).unlink()
        with contextlib.suppress(Exception):
            Path(sig_path).unlink()


def make_auth_header(
    method: str,
    path: str,
    body: bytes | None = None,
) -> str | None:
    """Create Authorization header with SSH signature."""
    # Find keys
    pubkey_info = find_ssh_pubkey()
    privkey_info = find_ssh_private_key()

    if not pubkey_info or not privkey_info:
        return None

    _, pubkey_content, _ = pubkey_info
    key_type, privkey_path = privkey_info

    # Compute fingerprint
    fingerprint = compute_pubkey_fingerprint(pubkey_content)

    # Create timestamp
    timestamp = int(time.time())

    # Compute body hash if body present
    body_hash = None
    if body:
        body_hash = hashlib.sha256(body).hexdigest()

    # Create payload
    payload = create_signature_payload(method, path, timestamp, body_hash)

    # Sign
    signature = sign_payload(payload, privkey_path, key_type)
    if not signature:
        return None

    # Encode signature
    sig_b64 = base64.b64encode(signature).decode()

    # Build header
    header = f'Signature keyid="{fingerprint}" ts="{timestamp}" sig="{sig_b64}"'
    return header


class GlaasClient:
    """Client for interacting with GLaaS server."""

    def __init__(self, base_url: str | None = None):
        self.base_url = base_url or get_glaas_url()
        if self.base_url:
            self.base_url = self.base_url.rstrip("/")

    def is_configured(self) -> bool:
        """Check if GLaaS is configured."""
        return self.base_url is not None

    def _parse_json_response(
        self, response_body: str, http_status: int
    ) -> tuple[dict | None, str | None]:
        """Parse JSON response with descriptive error messages.

        Returns (parsed_dict, error_message).
        """
        # Handle empty responses
        if not response_body:
            return None, f"Server returned empty response (HTTP {http_status})"

        # Handle whitespace-only responses
        if not response_body.strip():
            return None, f"Server returned whitespace-only response (HTTP {http_status})"

        # Detect HTML responses (common from misconfigured proxies)
        stripped = response_body.strip()
        if stripped.startswith("<!") or stripped.lower().startswith("<html"):
            preview = response_body[:100].replace("\n", " ")
            return None, f"Server returned HTML instead of JSON: '{preview}...'"

        # Attempt JSON parsing
        try:
            return json.loads(response_body), None
        except json.JSONDecodeError as e:
            preview = response_body[:100].replace("\n", " ")
            return None, (
                f"Invalid JSON in response (HTTP {http_status}) at position {e.pos}: '{preview}...'"
            )

    def health_check(self) -> tuple[bool, str | None]:
        """Check server health. Returns (ok, error_message)."""
        if not self.base_url:
            return False, "GLaaS URL not configured"

        try:
            url = f"{self.base_url}/api/v1/health"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    return True, None
                return False, f"Server returned status {resp.status}"
        except urllib.error.URLError as e:
            _get_logger().debug("GLaaS health check connection error: %s", e)
            return False, f"Connection error: {e}"
        except Exception as e:
            _get_logger().debug("GLaaS health check failed: %s", e)
            return False, str(e)

    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
    ) -> tuple[dict | None, str | None]:
        """Make authenticated request. Returns (response_dict, error_message)."""
        if not self.base_url:
            return None, "GLaaS URL not configured"

        url = f"{self.base_url}{path}"
        body_bytes = json.dumps(body).encode() if body else None

        _get_logger().debug(
            "API request: %s %s (body: %d bytes)",
            method,
            url,
            len(body_bytes) if body_bytes else 0,
        )

        # Create auth header
        auth_header = make_auth_header(method, path, body_bytes)
        if not auth_header:
            return None, "Failed to create authentication signature"

        # Build request
        req = urllib.request.Request(
            url,
            data=body_bytes,
            method=method,
        )
        req.add_header("Authorization", auth_header)
        if body_bytes:
            req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                http_status = resp.status
                response_body = resp.read().decode()

                _get_logger().debug(
                    "API response: %s %s -> HTTP %d (%d bytes)",
                    method,
                    path,
                    http_status,
                    len(response_body),
                )

                # Handle empty/whitespace responses (return {} for backward compatibility)
                if not response_body or not response_body.strip():
                    return {}, None

                # Parse JSON with descriptive errors
                result, error = self._parse_json_response(response_body, http_status)
                if error:
                    return None, error

                # Unwrap ApiResponse format: {"success": true, "data": {...}}
                if isinstance(result, dict) and result.get("success") and "data" in result:
                    return result["data"], None
                return result, None
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else ""
            # Try to parse error body as JSON
            error_data, _ = self._parse_json_response(error_body, e.code)
            if error_data and isinstance(error_data, dict):
                # Check for both "detail" (FastAPI) and "message" (Flask) keys
                detail = error_data.get("detail") or error_data.get("message") or str(e)
            elif error_body:
                # Detect proxy/firewall 403 (HTML response)
                stripped = error_body.strip()
                if e.code == 403 and (
                    stripped.startswith("<!") or stripped.lower().startswith("<html")
                ):
                    detail = (
                        "Access denied by proxy or firewall (received HTML 403). "
                        "Check network configuration."
                    )
                else:
                    # Include truncated preview of non-JSON error body
                    preview = error_body[:100].replace("\n", " ")
                    detail = (
                        f"Non-JSON response: '{preview}...'"
                        if len(error_body) > 100
                        else error_body
                    )
            else:
                detail = str(e)
            _get_logger().debug(
                "API error: %s %s -> HTTP %d: %s", method, path, e.code, detail[:200]
            )
            return None, f"HTTP {e.code}: {detail}"
        except urllib.error.URLError as e:
            _get_logger().debug("GLaaS connection error to %s: %s", url, e)
            return None, f"Connection error: {e}"
        except json.JSONDecodeError as e:
            _get_logger().debug(
                "GLaaS invalid JSON response from %s at position %d: %s", url, e.pos, e.msg
            )
            return None, f"Invalid JSON response at position {e.pos}: {e.msg}"
        except Exception as e:
            _get_logger().debug("GLaaS request to %s failed: %s", url, e)
            return None, str(e)

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

    def get_artifact(self, hash_prefix: str) -> tuple[dict | None, str | None]:
        """
        Look up artifact by hash prefix.

        Returns (artifact_dict, error_message).
        """
        result, error = self._request("GET", f"/api/v1/artifacts/{hash_prefix}")
        return result, error

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

        body = {"jobs": jobs}
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
        body = {
            "hash": session_hash,
            "git_repo": git_repo,
            "git_commit": git_commit,
            "git_branch": git_branch,
        }

        result, error = self._request("POST", "/api/v1/sessions", body)
        return result, error

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
            artifacts: List of dicts with {hash, size, path, source_type, metadata}

        Returns (result, error_message).
        result contains: job_uid, inputs_linked
        """
        body: dict[str, Any] = {"artifacts": artifacts}
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
            artifacts: List of dicts with {hash, size, path, source_type, metadata}

        Returns (result, error_message).
        result contains: job_uid, outputs_linked
        """
        body: dict[str, Any] = {"artifacts": artifacts}
        result, error = self._request(
            "POST",
            f"/api/v1/sessions/{session_hash}/jobs/{job_uid}/outputs",
            body,
        )
        return result, error

    def get_session(self, session_hash: str) -> tuple[dict | None, str | None]:
        """
        Get session details including jobs.

        Returns (session_info, error_message).
        """
        result, error = self._request("GET", f"/api/v1/sessions/{session_hash}")
        return result, error
