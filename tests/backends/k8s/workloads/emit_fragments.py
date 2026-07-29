"""Export the recorded in-pod roar job and stream it to GLaaS.

Phase-0 seam: reuses the OSMO bundle exporter (backend-neutral in
practice) and the shared fragment transport. The future k8s worker
bootstrap replaces this file; the identity contract it encodes
(pod UID + container + node index + attempt) is the piece under test.
"""

import json
import os
import sys
from pathlib import Path

from roar.backends.osmo.export import export_osmo_lineage_bundle
from roar.execution.fragments.transport import emit_fragment_dicts

TASK_NAME = "k8s-smoke-train"


def main() -> int:
    pod_uid = os.environ.get("ROAR_K8S_POD_UID", "unknown-pod")
    completion_index = os.environ.get("JOB_COMPLETION_INDEX", "0")
    restart_attempt = "0"
    task_id = f"{pod_uid}:trainer:{completion_index}:{restart_attempt}"

    bundle_path = Path("/tmp/roar-fragments.json")
    export = export_osmo_lineage_bundle(
        roar_dir=Path.cwd() / ".roar",
        output_path=bundle_path,
        task_id=task_id,
        task_name=TASK_NAME,
        backend_name="k8s",
    )
    print(f"[emit-fragments] exported job {export.exported_job_uid} as task {task_id}")

    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    fragments = payload["fragments"]
    for fragment in fragments:
        metadata = fragment.setdefault("backend_metadata", {})
        metadata["k8s_namespace"] = os.environ.get("ROAR_K8S_NAMESPACE")
        metadata["k8s_pod_name"] = os.environ.get("ROAR_K8S_POD_NAME")
        metadata["k8s_pod_uid"] = pod_uid
        metadata["k8s_node_name"] = os.environ.get("ROAR_K8S_NODE_NAME")
        metadata["k8s_container"] = "trainer"
        metadata["k8s_completion_index"] = completion_index
        metadata["k8s_restart_attempt"] = restart_attempt

    result = emit_fragment_dicts(fragments)
    print(f"[emit-fragments] emit result: {result} ({len(fragments)} fragment(s))")
    return 0 if result == "streamed" else 3


if __name__ == "__main__":
    sys.exit(main())
