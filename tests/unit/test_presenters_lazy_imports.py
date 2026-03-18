from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_presenters_package_lazy_imports_heavy_submodules() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    script = """
import json
import sys


def loaded():
    names = [
        "roar.presenters.console",
        "roar.presenters.dag_data_builder",
        "roar.presenters.dag_renderer",
        "roar.presenters.formatting",
        "roar.presenters.null",
        "roar.presenters.show_renderer",
        "roar.core.models",
    ]
    return {name: name in sys.modules for name in names}


import roar.presenters as presenters
states = {"after_package_import": loaded()}

import roar.presenters.formatting
states["after_formatting_import"] = loaded()

from roar.presenters import ShowRenderer

states["after_show_renderer_import"] = loaded()

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
        "roar.presenters.console": False,
        "roar.presenters.dag_data_builder": False,
        "roar.presenters.dag_renderer": False,
        "roar.presenters.formatting": False,
        "roar.presenters.null": False,
        "roar.presenters.show_renderer": False,
        "roar.core.models": False,
    }
    assert states["after_formatting_import"] == {
        "roar.presenters.console": False,
        "roar.presenters.dag_data_builder": False,
        "roar.presenters.dag_renderer": False,
        "roar.presenters.formatting": True,
        "roar.presenters.null": False,
        "roar.presenters.show_renderer": False,
        "roar.core.models": False,
    }
    assert states["after_show_renderer_import"] == {
        "roar.presenters.console": False,
        "roar.presenters.dag_data_builder": False,
        "roar.presenters.dag_renderer": False,
        "roar.presenters.formatting": True,
        "roar.presenters.null": False,
        "roar.presenters.show_renderer": True,
        "roar.core.models": False,
    }
