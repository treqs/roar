"""Mount-map derivation and path rewriting for mounted object storage.

FUSE CSI mounts (GCS FUSE, Mountpoint-for-S3) surface object-store I/O
as local file syscalls under a mount path — the tracer captures them,
but at `/data/foo` instead of `gs://bucket/foo`. The rewriter derives a
mount map per container at submit time (inline CSI volumes it can see,
plus explicit `k8s.mount_map` config for PVC-backed drivers whose
bucket lives in the cluster-side PV), injects it as
``ROAR_K8S_MOUNT_MAP``, and the pod entrypoint stamps it into fragment
metadata. Reconstitution rewrites ref paths — capture stays raw in the
fragment; the transformation happens at merge time and the mapping used
is preserved in metadata.

PVC mounts get a ``volume`` identity tag (no rewrite): their cross-pod
edges already connect through content hashes.
"""

from __future__ import annotations

import json
from typing import Any

MOUNT_MAP_ENV = "ROAR_K8S_MOUNT_MAP"

# Inline-CSI drivers whose pod spec carries enough to name the remote URI.
_CSI_URI_SCHEMES = {
    "gcsfuse.csi.storage.gke.io": "gs",
    "s3.csi.aws.com": "s3",
}


def build_container_mount_map(
    pod_spec: dict[str, Any],
    container: dict[str, Any],
    config_mount_map: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Return mount-map entries for one container's volume mounts.

    Entry shapes:
    - ``{"mount_path": "/data", "uri": "gs://bucket"}`` — rewrite target
    - ``{"mount_path": "/ckpt", "volume": "pvc://claim"}`` — identity tag
    Explicit ``k8s.mount_map`` config entries win over derived ones.
    """
    volumes_by_name: dict[str, dict[str, Any]] = {}
    for volume in pod_spec.get("volumes") or []:
        if isinstance(volume, dict) and volume.get("name"):
            volumes_by_name[str(volume["name"])] = volume

    entries: list[dict[str, str]] = []
    configured = dict(config_mount_map or {})

    for mount in container.get("volumeMounts") or []:
        if not isinstance(mount, dict):
            continue
        mount_path = str(mount.get("mountPath") or "").rstrip("/")
        if not mount_path:
            continue
        if mount_path in configured:
            continue  # explicit config wins; added below

        volume = volumes_by_name.get(str(mount.get("name") or ""))
        if not isinstance(volume, dict):
            continue

        uri = _inline_csi_uri(volume)
        if uri:
            sub_path = str(mount.get("subPath") or "").strip("/")
            if sub_path:
                uri = f"{uri}/{sub_path}"
            entries.append({"mount_path": mount_path, "uri": uri})
            continue

        claim = volume.get("persistentVolumeClaim")
        if isinstance(claim, dict) and claim.get("claimName"):
            entries.append({"mount_path": mount_path, "volume": f"pvc://{claim['claimName']}"})

    for mount_path, uri in configured.items():
        normalized = str(mount_path or "").rstrip("/")
        target = str(uri or "").rstrip("/")
        if normalized and target:
            entries.append({"mount_path": normalized, "uri": target})

    return entries


def _inline_csi_uri(volume: dict[str, Any]) -> str | None:
    csi = volume.get("csi")
    if not isinstance(csi, dict):
        return None
    scheme = _CSI_URI_SCHEMES.get(str(csi.get("driver") or ""))
    if scheme is None:
        return None
    attributes = csi.get("volumeAttributes")
    bucket = ""
    if isinstance(attributes, dict):
        bucket = str(attributes.get("bucketName") or "").strip()
    if not bucket:
        return None
    return f"{scheme}://{bucket}"


def dump_mount_map(entries: list[dict[str, str]]) -> str:
    return json.dumps(entries, separators=(",", ":"))


def parse_mount_map(raw: str | None) -> list[dict[str, str]]:
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    entries: list[dict[str, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        mount_path = str(item.get("mount_path") or "").rstrip("/")
        if not mount_path:
            continue
        entry = {"mount_path": mount_path}
        if item.get("uri"):
            entry["uri"] = str(item["uri"]).rstrip("/")
        if item.get("volume"):
            entry["volume"] = str(item["volume"])
        entries.append(entry)
    return entries


def rewrite_fragment_paths(fragment: dict[str, Any]) -> None:
    """Rewrite mounted-object paths in a fragment dict, in place.

    Uses the ``k8s_mount_map`` recorded in the fragment's backend
    metadata; longest mount-path prefix wins. Entries without a ``uri``
    (PVC identity tags) never rewrite.
    """
    metadata = fragment.get("backend_metadata")
    if not isinstance(metadata, dict):
        return
    entries = [
        entry
        for entry in metadata.get("k8s_mount_map") or []
        if isinstance(entry, dict) and entry.get("uri") and entry.get("mount_path")
    ]
    if not entries:
        return
    entries.sort(key=lambda entry: len(str(entry["mount_path"])), reverse=True)

    for list_key in ("reads", "writes"):
        refs = fragment.get(list_key)
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            path = str(ref.get("path") or "")
            rewritten = _rewrite_path(path, entries)
            if rewritten is not None:
                ref["path"] = rewritten


def _rewrite_path(path: str, entries: list[dict[str, Any]]) -> str | None:
    if not path.startswith("/"):
        return None
    for entry in entries:
        mount_path = str(entry["mount_path"])
        uri = str(entry["uri"])
        if path == mount_path:
            return uri
        if path.startswith(mount_path + "/"):
            return uri + path[len(mount_path) :]
    return None


__all__ = [
    "MOUNT_MAP_ENV",
    "build_container_mount_map",
    "dump_mount_map",
    "parse_mount_map",
    "rewrite_fragment_paths",
]
