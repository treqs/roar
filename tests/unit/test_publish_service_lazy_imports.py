from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_publish_service_import_stays_lightweight() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    script = """
import json
import sys


def loaded():
    names = [
        "roar.application.publish.collection",
        "roar.application.publish.put_execution",
        "roar.application.publish.register_execution",
        "roar.application.publish.runtime",
        "roar.application.publish.targets",
        "roar.db.context",
        "roar.db.engine",
        "roar.db.models",
        "roar.backends.ray.collector",
        "roar.backends.ray.submit",
        "sqlalchemy",
    ]
    return {name: name in sys.modules for name in names}


import roar.application.publish.service as service
states = {"after_module_import": loaded()}

from roar.application.publish.service import put_artifacts, register_lineage_target

states["after_entrypoint_import"] = loaded()

print(json.dumps(states))
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    states = json.loads(proc.stdout)

    expected = {
        "roar.application.publish.collection": False,
        "roar.application.publish.put_execution": False,
        "roar.application.publish.register_execution": False,
        "roar.application.publish.runtime": False,
        "roar.application.publish.targets": False,
        "roar.db.context": False,
        "roar.db.engine": False,
        "roar.db.models": False,
        "roar.backends.ray.collector": False,
        "roar.backends.ray.submit": False,
        "sqlalchemy": False,
    }
    assert states["after_module_import"] == expected
    assert states["after_entrypoint_import"] == expected
