"""Mutating admission webhook: zero-touch lineage injection.

Intercepts CREATE of supported training workloads (Job, JobSet,
PyTorchJob, TrainJob, RayJob) in opted-in namespaces and applies the
same manifest rewrite as the CLI path — the platform team installs one
thing and every workload in a labeled namespace gets lineage, with no
`roar run` on the submitting side. Reconstitution happens later via
``roar k8s attach`` (credentials live in the per-workload Secret the
webhook creates).

Design constraints honored:
- never blocks admission: any internal failure returns allowed with a
  warning (pair with ``failurePolicy: Ignore`` in the webhook config);
- idempotent under ``reinvocationPolicy: IfNeeded`` via the
  ``roar.glaas.ai/parent-uid`` annotation;
- side effects (GLaaS session registration, Secret creation) are skipped
  for dry-run admission (declare ``sideEffects: NoneOnDryRun``);
- stdlib only (http.server + ssl + urllib) so the roar-runtime image can
  serve it without extra dependencies.
"""

from __future__ import annotations

import base64
import json
import os
import secrets as secrets_module
import ssl
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

OPT_OUT_LABEL = "roar.glaas.ai/lineage"
ANNOTATION_PARENT_UID = "roar.glaas.ai/parent-uid"
ANNOTATION_SESSION_ID = "roar.glaas.ai/session-id"
ANNOTATION_SECRET = "roar.glaas.ai/fragment-secret"

_SERVICE_ACCOUNT_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"

SecretCreator = Callable[[str, str, dict[str, str]], None]
SessionRegistrar = Callable[[str, str], None]


@dataclass(frozen=True)
class WebhookSettings:
    """Injection settings, sourced from the webhook Deployment's env."""

    glaas_url: str
    cluster_glaas_url: str
    tracer: str = "preload"
    runtime_source: str = "install"
    runtime_image: str = ""
    runtime_install_requirement: str = "roar-cli"
    fragment_session_ttl_seconds: int = 86400
    bundle_dir: str = ""
    mount_map: dict[str, str] = field(default_factory=dict)
    proxy_sidecar: bool = False
    proxy_upstream: str = ""

    @classmethod
    def from_environ(cls, environ: dict[str, str] | None = None) -> WebhookSettings:
        env = os.environ if environ is None else environ
        mount_map: dict[str, str] = {}
        raw_mount_map = env.get("ROAR_WEBHOOK_MOUNT_MAP", "")
        if raw_mount_map:
            try:
                parsed = json.loads(raw_mount_map)
                if isinstance(parsed, dict):
                    mount_map = {str(k): str(v) for k, v in parsed.items()}
            except json.JSONDecodeError:
                pass
        return cls(
            glaas_url=env.get("ROAR_WEBHOOK_GLAAS_URL", "").strip(),
            cluster_glaas_url=env.get("ROAR_WEBHOOK_CLUSTER_GLAAS_URL", "").strip(),
            tracer=env.get("ROAR_WEBHOOK_TRACER", "preload").strip() or "preload",
            runtime_source=env.get("ROAR_WEBHOOK_RUNTIME_SOURCE", "install").strip() or "install",
            runtime_image=env.get("ROAR_WEBHOOK_RUNTIME_IMAGE", "").strip(),
            runtime_install_requirement=env.get(
                "ROAR_WEBHOOK_RUNTIME_REQUIREMENT", "roar-cli"
            ).strip()
            or "roar-cli",
            fragment_session_ttl_seconds=int(env.get("ROAR_WEBHOOK_SESSION_TTL", "86400") or 86400),
            bundle_dir=env.get("ROAR_WEBHOOK_BUNDLE_DIR", "").strip(),
            mount_map=mount_map,
            proxy_sidecar=env.get("ROAR_WEBHOOK_PROXY_SIDECAR", "").strip().lower()
            in ("1", "true", "yes"),
            proxy_upstream=env.get("ROAR_WEBHOOK_PROXY_UPSTREAM", "").strip(),
        )


def mutate_admission_review(
    review: dict[str, Any],
    *,
    settings: WebhookSettings,
    create_secret: SecretCreator,
    register_session: SessionRegistrar,
) -> dict[str, Any]:
    """Return the AdmissionReview response for one admission request."""
    request = review.get("request") or {}
    uid = str(request.get("uid") or "")

    def _allow(warnings: list[str] | None = None) -> dict[str, Any]:
        response: dict[str, Any] = {"uid": uid, "allowed": True}
        if warnings:
            response["warnings"] = warnings
        return {
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": response,
        }

    try:
        from roar.backends.k8s.manifest import (
            rewrite_manifest_for_lineage,
            workload_kind_for_document,
        )
        from roar.execution.fragments.sessions import generate_fragment_session

        obj = request.get("object")
        if not isinstance(obj, dict) or workload_kind_for_document(obj) is None:
            return _allow()

        metadata = obj.get("metadata") or {}
        labels = metadata.get("labels") or {}
        annotations = metadata.get("annotations") or {}
        if str(labels.get(OPT_OUT_LABEL, "")).lower() == "disabled":
            return _allow()
        if annotations.get(ANNOTATION_PARENT_UID):
            return _allow()  # already instrumented (reinvocation/replay)
        if bool(request.get("dryRun")):
            return _allow(["roar: dry-run admission is not instrumented"])

        namespace = str(request.get("namespace") or metadata.get("namespace") or "default")

        session = generate_fragment_session()
        register_session(session["session_id"], session["token_hash"])

        parent_job_uid = secrets_module.token_hex(4)
        secret_name = f"roar-fragment-{session['session_id'][:8]}"
        create_secret(
            namespace,
            secret_name,
            {"session_id": session["session_id"], "token": session["token"]},
        )

        rewrite = rewrite_manifest_for_lineage(
            [obj],
            secret_name=secret_name,
            session_id=None,  # Secret created via the API, never embedded
            fragment_token=None,
            requirement=settings.runtime_install_requirement,
            cluster_glaas_url=settings.cluster_glaas_url,
            tracer=settings.tracer,
            parent_job_uid=parent_job_uid,
            bundle_dir=settings.bundle_dir,
            mount_map=settings.mount_map,
            runtime_source=settings.runtime_source,
            runtime_image=settings.runtime_image,
            proxy_sidecar=settings.proxy_sidecar,
            proxy_upstream=settings.proxy_upstream,
            namespace_override=namespace,
        )
        rewritten = rewrite.documents[0]

        merged_annotations = dict(annotations)
        merged_annotations[ANNOTATION_PARENT_UID] = parent_job_uid
        merged_annotations[ANNOTATION_SESSION_ID] = session["session_id"]
        merged_annotations[ANNOTATION_SECRET] = secret_name

        patch = [
            {"op": "replace", "path": "/spec", "value": rewritten["spec"]},
            {"op": "add", "path": "/metadata/annotations", "value": merged_annotations},
        ]
        response: dict[str, Any] = {
            "uid": uid,
            "allowed": True,
            "patchType": "JSONPatch",
            "patch": base64.b64encode(json.dumps(patch).encode("utf-8")).decode("ascii"),
        }
        return {
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": response,
        }
    except Exception as exc:  # never block admission on lineage failures
        return _allow([f"roar: lineage injection skipped: {exc}"])


def create_namespaced_secret(namespace: str, name: str, string_data: dict[str, str]) -> None:
    """Create a Secret through the in-cluster API (service-account auth)."""
    with open(f"{_SERVICE_ACCOUNT_DIR}/token", encoding="utf-8") as handle:
        token = handle.read().strip()

    body = json.dumps(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": {"app.kubernetes.io/managed-by": "roar"},
            },
            "type": "Opaque",
            "stringData": string_data,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        url=f"https://kubernetes.default.svc/api/v1/namespaces/{namespace}/secrets",
        data=body,
        headers={
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
        },
        method="POST",
    )
    context = ssl.create_default_context(cafile=f"{_SERVICE_ACCOUNT_DIR}/ca.crt")
    with urllib.request.urlopen(request, timeout=10, context=context) as response:
        if response.status not in (200, 201):
            raise RuntimeError(f"secret creation returned HTTP {response.status}")


def register_glaas_session(settings: WebhookSettings) -> SessionRegistrar:
    def _register(session_id: str, token_hash: str) -> None:
        from roar.integrations.glaas import GlaasClient

        client = GlaasClient(base_url=settings.glaas_url)
        _result, error = client.register_fragment_session(
            session_id=session_id,
            token_hash=token_hash,
            ttl_seconds=settings.fragment_session_ttl_seconds,
        )
        if error:
            raise RuntimeError(f"fragment session registration failed: {error}")

    return _register


class _WebhookHandler(BaseHTTPRequestHandler):
    settings: WebhookSettings

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._respond(200, b"ok", content_type="text/plain")
        else:
            self._respond(404, b"not found", content_type="text/plain")

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path != "/mutate":
            self._respond(404, b"not found", content_type="text/plain")
            return
        length = int(self.headers.get("content-length") or 0)
        try:
            review = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._respond(400, b"invalid AdmissionReview", content_type="text/plain")
            return

        result = mutate_admission_review(
            review,
            settings=self.settings,
            create_secret=create_namespaced_secret,
            register_session=register_glaas_session(self.settings),
        )
        self._respond(200, json.dumps(result).encode("utf-8"))

    def _respond(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[roar-webhook] {self.address_string()} {format % args}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="roar k8s lineage mutating webhook")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    args = parser.parse_args(argv)

    settings = WebhookSettings.from_environ()
    if not settings.glaas_url:
        print("[roar-webhook] ROAR_WEBHOOK_GLAAS_URL is required", file=sys.stderr)
        return 2

    handler = type("BoundHandler", (_WebhookHandler,), {"settings": settings})
    server = ThreadingHTTPServer(("0.0.0.0", args.port), handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=args.cert, keyfile=args.key)
    server.socket = context.wrap_socket(server.socket, server_side=True)

    print(f"[roar-webhook] serving on :{args.port}", file=sys.stderr)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
