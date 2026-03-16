"""Ray job that writes both workspace and /tmp artifacts."""

from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path

import ray


@ray.remote
def write_probe_files() -> dict[str, str]:
    suffix = uuid.uuid4().hex[:8]
    workspace_dir = Path.cwd() / "artifacts" / "tmp_filter"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    kept_path = workspace_dir / f"kept_{suffix}.json"
    tmp_path = Path(tempfile.gettempdir()) / f"roar_tmp_filter_probe_{suffix}.json"

    with open(kept_path, "w", encoding="utf-8") as handle:
        json.dump({"kind": "workspace", "suffix": suffix}, handle)
    with open(kept_path, encoding="utf-8") as handle:
        _ = json.load(handle)

    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump({"kind": "tmp", "suffix": suffix}, handle)
    with open(tmp_path, encoding="utf-8") as handle:
        _ = json.load(handle)

    return {"workspace_path": str(kept_path), "tmp_path": str(tmp_path)}


def main() -> None:
    ray.init(address="auto")
    result = ray.get(write_probe_files.remote())
    print(json.dumps(result))


if __name__ == "__main__":
    main()
