from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_core_models_package_lazy_imports_heavy_submodules() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    script = """
import json
import sys


def loaded():
    names = [
        "roar.core.models.artifact",
        "roar.core.models.base",
        "roar.core.models.dag",
        "roar.core.models.glaas",
        "roar.core.models.job",
        "roar.core.models.lineage",
        "roar.core.models.provenance",
        "roar.core.models.run",
        "roar.core.models.session",
        "roar.core.models.telemetry",
        "roar.core.models.vcs",
    ]
    return {name: name in sys.modules for name in names}


import roar.core.models as models
states = {"after_package_import": loaded()}

from roar.core.models import RoarBaseModel

states["after_base_export_import"] = loaded()

import roar.core.models.base
states["after_base_module_import"] = loaded()

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

    assert states["after_package_import"] == {
        "roar.core.models.artifact": False,
        "roar.core.models.base": False,
        "roar.core.models.dag": False,
        "roar.core.models.glaas": False,
        "roar.core.models.job": False,
        "roar.core.models.lineage": False,
        "roar.core.models.provenance": False,
        "roar.core.models.run": False,
        "roar.core.models.session": False,
        "roar.core.models.telemetry": False,
        "roar.core.models.vcs": False,
    }
    assert states["after_base_export_import"] == {
        "roar.core.models.artifact": False,
        "roar.core.models.base": True,
        "roar.core.models.dag": False,
        "roar.core.models.glaas": False,
        "roar.core.models.job": False,
        "roar.core.models.lineage": False,
        "roar.core.models.provenance": False,
        "roar.core.models.run": False,
        "roar.core.models.session": False,
        "roar.core.models.telemetry": False,
        "roar.core.models.vcs": False,
    }
    assert states["after_base_module_import"] == states["after_base_export_import"]
