from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_query_package_lazy_imports_heavy_submodules() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    script = """
import json
import sys


def loaded():
    names = [
        "roar.application.query.requests",
        "roar.application.query.results",
        "roar.application.query.status",
        "roar.application.query.show",
        "roar.application.query.log",
        "roar.application.query.dag",
        "roar.application.query.label",
        "roar.application.query.lineage",
    ]
    return {name: name in sys.modules for name in names}


import roar.application.query as query
states = {"after_package_import": loaded()}

from roar.application.query import StatusQueryRequest

states["after_request_import"] = loaded()

from roar.application.query import render_status

states["after_render_import"] = loaded()

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
        "roar.application.query.requests": False,
        "roar.application.query.results": False,
        "roar.application.query.status": False,
        "roar.application.query.show": False,
        "roar.application.query.log": False,
        "roar.application.query.dag": False,
        "roar.application.query.label": False,
        "roar.application.query.lineage": False,
    }
    assert states["after_request_import"] == {
        "roar.application.query.requests": True,
        "roar.application.query.results": False,
        "roar.application.query.status": False,
        "roar.application.query.show": False,
        "roar.application.query.log": False,
        "roar.application.query.dag": False,
        "roar.application.query.label": False,
        "roar.application.query.lineage": False,
    }
    assert states["after_render_import"] == {
        "roar.application.query.requests": True,
        "roar.application.query.results": True,
        "roar.application.query.status": True,
        "roar.application.query.show": False,
        "roar.application.query.log": False,
        "roar.application.query.dag": False,
        "roar.application.query.label": False,
        "roar.application.query.lineage": False,
    }
