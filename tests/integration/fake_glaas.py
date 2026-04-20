"""Minimal fake GLaaS server for local publish-path integration tests."""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class _FakeGlaasServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _FakeGlaasHandler)
        self.health_checks = 0
        self.session_registrations: list[dict[str, Any]] = []
        self.registration_session_creations: list[dict[str, Any]] = []
        self.registration_session_finalizations: list[dict[str, Any]] = []
        self.job_batches: list[dict[str, Any]] = []
        self.job_creates: list[dict[str, Any]] = []
        self.registration_session_job_batches: list[dict[str, Any]] = []
        self.registration_session_job_creates: list[dict[str, Any]] = []
        self.artifact_batches: list[list[dict[str, Any]]] = []
        self.auth_headers: list[dict[str, Any]] = []
        self.input_links: list[dict[str, Any]] = []
        self.output_links: list[dict[str, Any]] = []
        self.registration_session_input_links: list[dict[str, Any]] = []
        self.registration_session_output_links: list[dict[str, Any]] = []
        self.label_syncs: list[list[dict[str, Any]]] = []
        self.label_mutation_attempts: list[dict[str, Any]] = []
        self.label_mutations: list[dict[str, Any]] = []
        self.current_labels_by_target: dict[str, dict[str, Any]] = {}
        self.composite_registrations: list[dict[str, Any]] = []
        self.artifacts_by_digest: dict[str, dict[str, Any]] = {}
        self.artifact_dags_by_digest: dict[str, dict[str, Any]] = {}
        self.session_reproductions_by_hash: dict[str, dict[str, Any]] = {}
        self.registration_sessions_by_id: dict[str, dict[str, Any]] = {}
        self.registration_session_ids_by_client_session_id: dict[str, str] = {}
        self._next_job_id = 1
        self._next_registration_session_id = 1
        self._next_finalized_hash = 1

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def allocate_job_id(self) -> int:
        job_id = self._next_job_id
        self._next_job_id += 1
        return job_id

    def allocate_registration_session_id(self) -> str:
        registration_session_id = f"reg-session-{self._next_registration_session_id}"
        self._next_registration_session_id += 1
        return registration_session_id

    def allocate_lineage_hash(self) -> str:
        lineage_hash = f"{self._next_finalized_hash:064x}"
        self._next_finalized_hash += 1
        return lineage_hash


class _FakeGlaasHandler(BaseHTTPRequestHandler):
    server: _FakeGlaasServer

    def _read_json(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            return {}
        raw = self.rfile.read(content_length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _write_json(self, status_code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _resolve_authenticated_user(self, authorization: str | None) -> dict[str, str] | None:
        if authorization and authorization.startswith("Bearer "):
            return {
                "id": "user-123",
                "email": "trevor@example.com",
                "username": "trevor",
                "auth_mode": "bearer",
            }
        if authorization and authorization.startswith("Signature "):
            return {
                "id": "ssh-user-123",
                "email": "ssh-user@example.com",
                "username": "ssh-user",
                "auth_mode": "ssh",
            }
        return None

    def _record_artifacts(self, artifacts: list[dict[str, Any]]) -> None:
        for artifact in artifacts:
            artifact_hash = artifact.get("hash")
            if isinstance(artifact_hash, str) and artifact_hash:
                self.server.artifacts_by_digest[artifact_hash] = artifact
                continue

            hashes = artifact.get("hashes", [])
            if not isinstance(hashes, list):
                continue
            for entry in hashes:
                if not isinstance(entry, dict):
                    continue
                digest = entry.get("digest")
                if isinstance(digest, str) and digest:
                    self.server.artifacts_by_digest[digest] = artifact

    def _resolve_creator_identity(self, authenticated_user: dict[str, str] | None) -> str:
        if not isinstance(authenticated_user, dict):
            return "anonymous"
        if authenticated_user.get("auth_mode") == "bearer":
            return "treqs:user:treqs-user-123"
        user_id = authenticated_user.get("id")
        if isinstance(user_id, str) and user_id:
            return f"glaas:user:{user_id}"
        return "anonymous"

    def _compute_registration_session_hash(
        self,
        session_state: dict[str, Any],
        finalize_payload: dict[str, Any],
        authenticated_user: dict[str, str] | None,
    ) -> str | None:
        from roar.core.canonical_session import compute_canonical_session_hash

        jobs_by_uid = session_state.get("jobs")
        if not isinstance(jobs_by_uid, dict) or not jobs_by_uid:
            return None

        step_by_uid: dict[str, int] = {}
        for job_uid, job in jobs_by_uid.items():
            if isinstance(job, dict) and isinstance(job.get("step_number"), int):
                step_by_uid[str(job_uid)] = int(job["step_number"])

        jobs_payload: list[dict[str, Any]] = []
        for _job_uid, job in jobs_by_uid.items():
            if not isinstance(job, dict):
                continue
            inputs = sorted(
                [
                    {"hash": artifact.get("hash"), "path": artifact.get("path")}
                    for artifact in job.get("inputs", [])
                    if isinstance(artifact, dict)
                ],
                key=lambda artifact: (
                    str(artifact.get("hash") or ""),
                    str(artifact.get("path") or ""),
                ),
            )
            outputs = sorted(
                [
                    {"hash": artifact.get("hash"), "path": artifact.get("path")}
                    for artifact in job.get("outputs", [])
                    if isinstance(artifact, dict)
                ],
                key=lambda artifact: (
                    str(artifact.get("hash") or ""),
                    str(artifact.get("path") or ""),
                ),
            )
            metadata = job.get("metadata")
            parent_job_uid = job.get("parent_job_uid")
            parent_step_number = (
                step_by_uid.get(str(parent_job_uid)) if isinstance(parent_job_uid, str) else None
            )
            jobs_payload.append(
                {
                    "command": job.get("command"),
                    "job_type": job.get("job_type"),
                    "step_number": job.get("step_number"),
                    "parent_step_number": parent_step_number,
                    "inputs": inputs,
                    "outputs": outputs,
                    "metadata": dict(sorted(metadata.items()))
                    if isinstance(metadata, dict)
                    else {},
                }
            )

        if not jobs_payload:
            return None

        jobs_payload.sort(
            key=lambda job: (
                int(job.get("step_number") or 0),
                str(job.get("command") or ""),
            )
        )
        return compute_canonical_session_hash(
            {
                "canonical_version": 1,
                "creator_identity": self._resolve_creator_identity(authenticated_user),
                "git": {
                    "repo": finalize_payload.get("git_repo"),
                    "commit": finalize_payload.get("git_commit"),
                    "branch": finalize_payload.get("git_branch"),
                },
                "jobs": jobs_payload,
            }
        )

    def do_GET(self) -> None:
        authorization = self.headers.get("Authorization")
        if self.path == "/api/v1/auth/access-context":
            self.server.auth_headers.append({"path": self.path, "authorization": authorization})
            if not authorization or not authorization.startswith("Bearer "):
                self._write_json(401, {"error": "Missing or invalid bearer auth"})
                return
            self._write_json(
                200,
                {
                    "success": True,
                    "data": {
                        "user": {
                            "id": "user-123",
                            "sub": "treqs-user-123",
                            "username": "trevor",
                            "email": "trevor@example.com",
                        },
                        "owners": [
                            {
                                "id": "owner-test",
                                "type": "organization",
                                "username": "acme",
                                "display_name": "Acme AI",
                                "role": "admin",
                            }
                        ],
                        "projects_by_owner": {
                            "owner-test": [
                                {
                                    "id": "proj-test",
                                    "name": "test-project",
                                    "visibility": "private",
                                    "can_write": True,
                                }
                            ]
                        },
                    },
                },
            )
            return

        if self.path == "/api/v1/auth/me":
            self.server.auth_headers.append({"path": self.path, "authorization": authorization})
            user = self._resolve_authenticated_user(authorization)
            if user is None:
                self._write_json(401, {"error": "Missing or invalid auth"})
                return
            self._write_json(
                200,
                {
                    "success": True,
                    "data": {
                        "user": {
                            "id": user["id"],
                            "email": user["email"],
                            "githubUsername": user["username"],
                            "treqsUserId": None,
                        },
                        "creatorIdentity": f"glaas:user:{user['id']}",
                    },
                    "meta": {"authMode": user["auth_mode"]},
                },
            )
            return

        if self.path == "/api/v1/health":
            self.server.health_checks += 1
            self._write_json(200, {"success": True, "status": "healthy"})
            return

        artifact_match = re.fullmatch(r"/api/v1/artifacts/([0-9a-f]+)", self.path)
        if artifact_match:
            prefix = artifact_match.group(1)
            for digest in sorted(self.server.artifacts_by_digest):
                if digest.startswith(prefix):
                    self._write_json(200, {"hash": digest})
                    return
            self._write_json(404, {"error": f"Artifact not found: {prefix}"})
            return

        artifact_dag_match = re.fullmatch(r"/api/v1/artifacts/([0-9a-f]+)/dag", self.path)
        if artifact_dag_match:
            prefix = artifact_dag_match.group(1)
            for digest in sorted(self.server.artifact_dags_by_digest):
                if digest.startswith(prefix):
                    self._write_json(200, self.server.artifact_dags_by_digest[digest])
                    return
            self._write_json(404, {"error": f"Artifact DAG not found: {prefix}"})
            return

        session_reproduction_match = re.fullmatch(
            r"/api/v1/public/sessions/([0-9a-f]{64})/reproduction",
            self.path,
        )
        if session_reproduction_match:
            session_hash = session_reproduction_match.group(1)
            payload = self.server.session_reproductions_by_hash.get(session_hash)
            if payload is not None:
                self._write_json(200, payload)
                return
            self._write_json(404, {"error": f"Session reproduction not found: {session_hash}"})
            return

        self._write_json(404, {"error": f"Unhandled GET path: {self.path}"})

    def do_PATCH(self) -> None:
        payload = self._read_json()
        authorization = self.headers.get("Authorization")
        self.server.auth_headers.append({"path": self.path, "authorization": authorization})
        if not authorization or not authorization.startswith(("Bearer ", "Signature ")):
            self._write_json(401, {"error": "Missing or invalid authenticated auth"})
            return

        if self.path == "/api/v1/labels/current":
            metadata = payload.get("metadata")
            target_key = _label_target_key(payload)
            self.server.label_mutation_attempts.append(payload)
            current = self.server.current_labels_by_target.get(target_key)
            if not current and payload.get("entity_type") == "job":
                requested_job_uid = payload.get("job_uid")
                if isinstance(requested_job_uid, str) and requested_job_uid:
                    current = next(
                        (
                            candidate
                            for candidate in self.server.current_labels_by_target.values()
                            if candidate.get("entityType") == "job"
                            and candidate.get("jobUid") == requested_job_uid
                        ),
                        None,
                    )
            if not current:
                self._write_json(404, {"error": {"message": "Label not found"}})
                return
            self.server.label_mutations.append(payload)
            merged_metadata = _deep_merge(
                current["metadata"], metadata if isinstance(metadata, dict) else {}
            )
            version = int(current.get("version", 0)) + 1
            updated = {
                **current,
                "version": version,
                "metadata": merged_metadata,
            }
            self.server.current_labels_by_target[target_key] = updated
            self._write_json(200, updated)
            return

        self._write_json(404, {"error": f"Unhandled PATCH path: {self.path}"})

    def do_POST(self) -> None:
        payload = self._read_json()
        authorization = self.headers.get("Authorization")
        self.server.auth_headers.append({"path": self.path, "authorization": authorization})
        authenticated_user = self._resolve_authenticated_user(authorization)
        if authenticated_user is None:
            self._write_json(401, {"error": "Missing or invalid auth"})
            return

        if self.path == "/api/v1/registration-sessions":
            self.server.registration_session_creations.append(payload)
            client_session_id = payload.get("client_session_id")
            if isinstance(client_session_id, str) and client_session_id:
                registration_session_id = (
                    self.server.registration_session_ids_by_client_session_id.get(client_session_id)
                )
            else:
                registration_session_id = None

            created = registration_session_id is None
            if registration_session_id is None:
                registration_session_id = self.server.allocate_registration_session_id()
                self.server.registration_sessions_by_id[registration_session_id] = {
                    "jobs": {},
                    "hash": None,
                    "status": "active",
                }
                if isinstance(client_session_id, str) and client_session_id:
                    self.server.registration_session_ids_by_client_session_id[client_session_id] = (
                        registration_session_id
                    )

            session_state = self.server.registration_sessions_by_id[registration_session_id]
            self._write_json(
                201,
                {
                    "registration_session_id": registration_session_id,
                    "created": created,
                    "status": session_state["status"],
                },
            )
            return

        if self.path == "/api/v1/sessions":
            self.server.session_registrations.append(
                {
                    **payload,
                    "_authenticated_user_id": authenticated_user["id"],
                    "_auth_mode": authenticated_user["auth_mode"],
                }
            )
            session_hash = str(payload.get("hash", ""))
            self._write_json(
                200,
                {
                    "hash": session_hash,
                    "url": f"{self.server.base_url}/dag/{session_hash}",
                    "created": True,
                },
            )
            return

        if self.path == "/api/v1/artifacts/batch":
            artifacts = payload.get("artifacts", [])
            if isinstance(artifacts, list):
                self.server.artifact_batches.append(artifacts)
                self._record_artifacts(artifacts)
            self._write_json(200, {"created": len(artifacts), "existing": 0})
            return

        if self.path == "/api/v1/labels/sync":
            labels = payload.get("labels", [])
            if isinstance(labels, list):
                self.server.label_syncs.append(labels)
                for label in labels:
                    if not isinstance(label, dict):
                        continue
                    target_key = _label_target_key(label)
                    current = self.server.current_labels_by_target.get(target_key)
                    version = int(current.get("version", 0)) + 1 if isinstance(current, dict) else 1
                    current_label = {
                        "id": f"label-{len(self.server.current_labels_by_target) + 1}",
                        "entityType": label.get("entity_type"),
                        "version": version,
                        "metadata": label.get("metadata")
                        if isinstance(label.get("metadata"), dict)
                        else {},
                        "createdAt": "2026-01-01T00:00:00Z",
                    }
                    if label.get("entity_type") == "dag":
                        current_label["sessionHash"] = label.get("session_hash")
                    elif label.get("entity_type") == "job":
                        current_label["sessionHash"] = label.get("session_hash")
                        current_label["jobUid"] = label.get("job_uid")
                    elif label.get("entity_type") == "artifact":
                        current_label["artifactHash"] = label.get("artifact_hash")
                    self.server.current_labels_by_target[target_key] = current_label
                self._write_json(
                    200,
                    {"created": 0, "updated": 0, "unchanged": len(labels)},
                )
                return

        if self.path == "/api/v1/artifacts/composites":
            self.server.composite_registrations.append(payload)
            self._record_artifacts([payload])
            self._write_json(
                200,
                {
                    "artifact_id": f"composite-{len(self.server.composite_registrations)}",
                    "created": True,
                },
            )
            return

        finalize_match = re.fullmatch(
            r"/api/v1/registration-sessions/([^/]+)/finalize",
            self.path,
        )
        if finalize_match:
            registration_session_id = finalize_match.group(1)
            session_state = self.server.registration_sessions_by_id.setdefault(
                registration_session_id,
                {"jobs": {}, "hash": None, "status": "active"},
            )
            lineage_hash = session_state.get("hash") or self._compute_registration_session_hash(
                session_state,
                payload,
                authenticated_user,
            )
            if not lineage_hash:
                lineage_hash = self.server.allocate_lineage_hash()
            session_state["hash"] = lineage_hash
            session_state["status"] = "closed"
            self.server.registration_session_finalizations.append(
                {
                    **payload,
                    "registration_session_id": registration_session_id,
                    "hash": lineage_hash,
                    "status": "closed",
                }
            )
            self._write_json(
                200,
                {
                    "hash": lineage_hash,
                    "url": f"{self.server.base_url}/dag/{lineage_hash}",
                    "created": True,
                    "registration_session_id": registration_session_id,
                    "status": "closed",
                },
            )
            return

        reg_batch_match = re.fullmatch(
            r"/api/v1/registration-sessions/([^/]+)/jobs/batch",
            self.path,
        )
        if reg_batch_match:
            registration_session_id = reg_batch_match.group(1)
            jobs = payload.get("jobs", [])
            if not isinstance(jobs, list):
                jobs = []
            self.server.registration_session_job_batches.append(
                {"registration_session_id": registration_session_id, "jobs": jobs}
            )
            session_state = self.server.registration_sessions_by_id.setdefault(
                registration_session_id,
                {"jobs": {}, "hash": None, "status": "active"},
            )
            job_ids = []
            for job in jobs:
                job_uid = job.get("job_uid")
                if isinstance(job_uid, str) and job_uid:
                    stored_job = session_state["jobs"].setdefault(
                        job_uid,
                        {"inputs": [], "outputs": []},
                    )
                    if isinstance(job, dict):
                        stored_job.update(
                            {
                                "command": job.get("command"),
                                "job_type": job.get("job_type"),
                                "step_number": job.get("step_number"),
                                "parent_job_uid": job.get("parent_job_uid"),
                                "metadata": job.get("metadata"),
                            }
                        )
                job_ids.append(self.server.allocate_job_id())
            self._write_json(200, {"job_ids": job_ids, "errors": []})
            return

        reg_job_match = re.fullmatch(r"/api/v1/registration-sessions/([^/]+)/jobs", self.path)
        if reg_job_match:
            registration_session_id = reg_job_match.group(1)
            self.server.registration_session_job_creates.append(
                {"registration_session_id": registration_session_id, "job": payload}
            )
            session_state = self.server.registration_sessions_by_id.setdefault(
                registration_session_id,
                {"jobs": {}, "hash": None, "status": "active"},
            )
            job_uid = payload.get("job_uid")
            if isinstance(job_uid, str) and job_uid:
                stored_job = session_state["jobs"].setdefault(
                    job_uid, {"inputs": [], "outputs": []}
                )
                stored_job.update(
                    {
                        "command": payload.get("command"),
                        "job_type": payload.get("job_type"),
                        "step_number": payload.get("step_number"),
                        "parent_job_uid": payload.get("parent_job_uid"),
                        "metadata": payload.get("metadata"),
                    }
                )
            self._write_json(200, {"id": self.server.allocate_job_id()})
            return

        reg_input_match = re.fullmatch(
            r"/api/v1/registration-sessions/([^/]+)/jobs/([^/]+)/inputs",
            self.path,
        )
        if reg_input_match:
            registration_session_id, job_uid = reg_input_match.groups()
            artifacts = payload.get("artifacts", [])
            if not isinstance(artifacts, list):
                artifacts = []
            self.server.registration_session_input_links.append(
                {
                    "registration_session_id": registration_session_id,
                    "job_uid": job_uid,
                    "artifacts": artifacts,
                }
            )
            self._record_artifacts(artifacts)
            session_state = self.server.registration_sessions_by_id.setdefault(
                registration_session_id,
                {"jobs": {}, "hash": None, "status": "active"},
            )
            session_state["jobs"].setdefault(job_uid, {"inputs": [], "outputs": []})[
                "inputs"
            ].extend(artifacts)
            self._write_json(
                200,
                {
                    "job_uid": job_uid,
                    "artifacts_registered": len(artifacts),
                    "inputs_linked": len(artifacts),
                },
            )
            return

        reg_output_match = re.fullmatch(
            r"/api/v1/registration-sessions/([^/]+)/jobs/([^/]+)/outputs",
            self.path,
        )
        if reg_output_match:
            registration_session_id, job_uid = reg_output_match.groups()
            artifacts = payload.get("artifacts", [])
            if not isinstance(artifacts, list):
                artifacts = []
            self.server.registration_session_output_links.append(
                {
                    "registration_session_id": registration_session_id,
                    "job_uid": job_uid,
                    "artifacts": artifacts,
                }
            )
            self._record_artifacts(artifacts)
            session_state = self.server.registration_sessions_by_id.setdefault(
                registration_session_id,
                {"jobs": {}, "hash": None, "status": "active"},
            )
            session_state["jobs"].setdefault(job_uid, {"inputs": [], "outputs": []})[
                "outputs"
            ].extend(artifacts)
            self._write_json(
                200,
                {
                    "job_uid": job_uid,
                    "artifacts_registered": len(artifacts),
                    "outputs_linked": len(artifacts),
                },
            )
            return

        batch_match = re.fullmatch(r"/api/v1/sessions/([0-9a-f]+)/jobs/batch", self.path)
        if batch_match:
            session_hash = batch_match.group(1)
            jobs = payload.get("jobs", [])
            if not isinstance(jobs, list):
                jobs = []
            self.server.job_batches.append({"session_hash": session_hash, "jobs": jobs})
            job_ids = [self.server.allocate_job_id() for _job in jobs]
            self._write_json(200, {"job_ids": job_ids, "errors": []})
            return

        job_match = re.fullmatch(r"/api/v1/sessions/([0-9a-f]+)/jobs", self.path)
        if job_match:
            session_hash = job_match.group(1)
            self.server.job_creates.append({"session_hash": session_hash, "job": payload})
            self._write_json(200, {"id": self.server.allocate_job_id()})
            return

        input_match = re.fullmatch(r"/api/v1/sessions/([0-9a-f]+)/jobs/([^/]+)/inputs", self.path)
        if input_match:
            session_hash, job_uid = input_match.groups()
            artifacts = payload.get("artifacts", [])
            if not isinstance(artifacts, list):
                artifacts = []
            self.server.input_links.append(
                {"session_hash": session_hash, "job_uid": job_uid, "artifacts": artifacts}
            )
            self._record_artifacts(artifacts)
            self._write_json(200, {"job_uid": job_uid, "inputs_linked": len(artifacts)})
            return

        output_match = re.fullmatch(
            r"/api/v1/sessions/([0-9a-f]+)/jobs/([^/]+)/outputs",
            self.path,
        )
        if output_match:
            session_hash, job_uid = output_match.groups()
            artifacts = payload.get("artifacts", [])
            if not isinstance(artifacts, list):
                artifacts = []
            self.server.output_links.append(
                {"session_hash": session_hash, "job_uid": job_uid, "artifacts": artifacts}
            )
            self._record_artifacts(artifacts)
            self._write_json(200, {"job_uid": job_uid, "outputs_linked": len(artifacts)})
            return

        self._write_json(404, {"error": f"Unhandled POST path: {self.path}"})

    def log_message(self, format: str, *args: object) -> None:
        """Suppress default stderr logging for integration tests."""


def _label_target_key(payload: dict[str, Any]) -> str:
    entity_type = str(payload.get("entity_type") or "")
    if entity_type == "dag":
        return f"dag:{payload.get('session_hash', '')}"
    if entity_type == "job":
        return f"job:{payload.get('session_hash', '')}:{payload.get('job_uid', '')}"
    return f"artifact:{payload.get('artifact_hash', '')}"


def _deep_merge(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(current))
    for key, value in patch.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


class FakeGlaasServer:
    """Context-managed fake GLaaS server."""

    def __init__(self) -> None:
        self._server = _FakeGlaasServer()
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return self._server.base_url

    @property
    def health_checks(self) -> int:
        return self._server.health_checks

    @property
    def session_registrations(self) -> list[dict[str, Any]]:
        return self._server.session_registrations

    @property
    def registration_session_creations(self) -> list[dict[str, Any]]:
        return self._server.registration_session_creations

    @property
    def registration_session_finalizations(self) -> list[dict[str, Any]]:
        return self._server.registration_session_finalizations

    @property
    def job_batches(self) -> list[dict[str, Any]]:
        return self._server.job_batches

    @property
    def job_creates(self) -> list[dict[str, Any]]:
        return self._server.job_creates

    @property
    def registration_session_job_batches(self) -> list[dict[str, Any]]:
        return self._server.registration_session_job_batches

    @property
    def registration_session_job_creates(self) -> list[dict[str, Any]]:
        return self._server.registration_session_job_creates

    @property
    def artifact_batches(self) -> list[list[dict[str, Any]]]:
        return self._server.artifact_batches

    @property
    def auth_headers(self) -> list[dict[str, Any]]:
        return self._server.auth_headers

    @property
    def input_links(self) -> list[dict[str, Any]]:
        return self._server.input_links

    @property
    def output_links(self) -> list[dict[str, Any]]:
        return self._server.output_links

    @property
    def registration_session_input_links(self) -> list[dict[str, Any]]:
        return self._server.registration_session_input_links

    @property
    def registration_session_output_links(self) -> list[dict[str, Any]]:
        return self._server.registration_session_output_links

    @property
    def label_syncs(self) -> list[list[dict[str, Any]]]:
        return self._server.label_syncs

    @property
    def label_mutation_attempts(self) -> list[dict[str, Any]]:
        return self._server.label_mutation_attempts

    @property
    def label_mutations(self) -> list[dict[str, Any]]:
        return self._server.label_mutations

    def set_artifact_dag(self, digest: str, dag: dict[str, Any]) -> None:
        self._server.artifact_dags_by_digest[digest] = dag

    def set_session_reproduction(self, session_hash: str, payload: dict[str, Any]) -> None:
        self._server.session_reproductions_by_hash[session_hash] = payload

    def __enter__(self) -> FakeGlaasServer:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
