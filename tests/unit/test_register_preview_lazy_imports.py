from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_register_dry_run_avoids_registration_and_full_config_imports() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    script = """
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import roar.application.publish.service as service
from roar.application.publish.requests import RegisterLineageRequest
from roar.application.publish.targets import ResolvedRegisterTarget
from roar.core.interfaces.lineage import LineageData


def loaded():
    names = [
        "roar.application.publish.register_execution",
        "roar.application.publish.register_preparation",
        "roar.application.publish.runtime",
        "roar.integrations.config.access",
        "roar.integrations.config.loader",
        "roar.integrations.config.schema",
    ]
    return {name: name in sys.modules for name in names}


states = {"after_service_import": loaded()}

runtime = SimpleNamespace(
    glaas_client=object(),
    session_service=object(),
    lineage_collector=object(),
)
collected = SimpleNamespace(
    lineage=LineageData(jobs=[], artifacts=[], artifact_hashes=set(), pipeline={"id": 7}),
    session_id=7,
    artifact_hash="a" * 64,
    session_hash_override=None,
)
prepared = SimpleNamespace(
    git_context=SimpleNamespace(repo="https://github.com/test/repo", commit="abc123", branch="main"),
    session_id=7,
    session_hash="a" * 64,
    session_url=None,
    git_tag_name=None,
    git_tag_repo_root=None,
)

service.build_register_preview_runtime = lambda: runtime
service.resolve_register_lineage_target = (
    lambda target, cwd, roar_dir: ResolvedRegisterTarget(kind="artifact_path", value="model.pt")
)
service.collect_register_lineage = lambda **kwargs: (collected, None)
service.prepare_register_preview_execution = lambda **kwargs: prepared

response = service.register_lineage_target(
    RegisterLineageRequest(
        target="model.pt",
        roar_dir=Path.cwd() / ".roar",
        cwd=Path.cwd(),
        dry_run=True,
        skip_confirmation=True,
    )
)
states["after_dry_run"] = loaded()

print(json.dumps({"success": response.success, "states": states}))
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(proc.stdout)

    assert result["success"] is True
    assert result["states"]["after_service_import"] == {
        "roar.application.publish.register_execution": False,
        "roar.application.publish.register_preparation": False,
        "roar.application.publish.runtime": False,
        "roar.integrations.config.access": False,
        "roar.integrations.config.loader": False,
        "roar.integrations.config.schema": False,
    }
    assert result["states"]["after_dry_run"] == result["states"]["after_service_import"]
