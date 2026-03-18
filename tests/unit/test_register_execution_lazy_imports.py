from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_register_execution_import_stays_lightweight_until_real_registration() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    script = """
import json
import sys


def loaded():
    names = [
        "roar.application.publish.blake3_upgrade",
        "roar.application.publish.composite_builder",
        "roar.application.publish.lineage_composites",
        "roar.application.publish.registration",
        "roar.db.context",
        "roar.db.engine",
        "roar.db.models",
        "sqlalchemy",
    ]
    return {name: name in sys.modules for name in names}


import roar.application.publish.register_execution as register_execution
states = {"after_module_import": loaded()}

service = register_execution.RegisterService()
states["after_service_init"] = loaded()

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
        "roar.application.publish.blake3_upgrade": False,
        "roar.application.publish.composite_builder": False,
        "roar.application.publish.lineage_composites": False,
        "roar.application.publish.registration": False,
        "roar.db.context": False,
        "roar.db.engine": False,
        "roar.db.models": False,
        "sqlalchemy": False,
    }
    assert states["after_module_import"] == expected
    assert states["after_service_init"] == expected
