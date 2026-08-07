"""P0-6 / P0-13: the name pass recovers a genuinely-used package the file pass
mis-attributed because the import was ALIASED (e.g. `sys.modules["wandb"] =
trackio`) — and ONLY that case. It must not attribute a normally-loaded import
(file pass's job) nor a merely-probed optional import that happens to be
installed (P0-13: `accelerate` probing `sagemaker` on a SageMaker AMI).

Aliasing is detected via `loaded_files` (name -> the module file actually loaded
for it): a name is attributed only when its loaded module lives in a *different*
site-packages package than the name.
"""

from __future__ import annotations

import importlib.metadata as ilm
import json
import os
import sys
import types

from roar.execution.runtime.inject.tracker import (
    RuntimeInjectionTracker,
    get_used_packages,
)


def _loaded_as(pkg: str) -> str:
    """A site-packages module file for top-level package ``pkg``."""
    return f"/venv/lib/python3.12/site-packages/{pkg}/__init__.py"


def test_aliased_import_attributed_by_name(monkeypatch):
    """`import wandb` aliased to trackio: file pass sees only trackio, but the
    name pass sees wandb was imported yet loaded a *different* package -> records
    wandb."""
    monkeypatch.setattr(ilm, "packages_distributions", lambda: {"wandb": ["wandb"]})
    used = get_used_packages(
        modules_files=[],
        installed_packages={"wandb": "0.16.0", "trackio": "0.1.0"},
        imported_modules=["wandb"],
        loaded_files={"wandb": _loaded_as("trackio")},  # aliased
    )
    assert used == {"wandb": "0.16.0"}


def test_probed_optional_import_is_not_attributed(monkeypatch):
    """P0-13 (#264 regression): an optional import that merely happened to be
    installed (loaded as ITSELF, or not loaded at all) is not aliased, so the
    name pass leaves it out — no unsatisfiable substrate in the freeze."""
    monkeypatch.setattr(ilm, "packages_distributions", lambda: {"sagemaker": ["sagemaker-core"]})
    installed = {"sagemaker-core": "2.9.0"}
    # loaded as itself (a real but merely-probed import) -> file pass's job, not ours
    used_self = get_used_packages(
        modules_files=[],
        installed_packages=installed,
        imported_modules=["sagemaker"],
        loaded_files={"sagemaker": _loaded_as("sagemaker")},
    )
    # probed via find_spec / lazy import -> never in loaded_files at all
    used_absent = get_used_packages(
        modules_files=[],
        installed_packages=installed,
        imported_modules=["sagemaker"],
        loaded_files={},
    )
    assert "sagemaker-core" not in used_self
    assert "sagemaker-core" not in used_absent


def test_never_imported_package_is_not_added(monkeypatch):
    monkeypatch.setattr(ilm, "packages_distributions", lambda: {"wandb": ["wandb"]})
    used = get_used_packages(
        modules_files=[],
        installed_packages={"wandb": "0.16.0"},
        imported_modules=["numpy", "os"],  # wandb never imported
        loaded_files={"wandb": _loaded_as("trackio")},  # even if (somehow) aliased
    )
    assert "wandb" not in used


def test_imported_but_not_installed_and_tracer_never_attributed(monkeypatch):
    """An aliased name that isn't installed is skipped; roar is never attributed."""
    monkeypatch.setattr(
        ilm, "packages_distributions", lambda: {"ghost": ["ghost"], "roar": ["roar-cli"]}
    )
    used = get_used_packages(
        modules_files=[],
        installed_packages={"roar-cli": "0.4.4"},  # 'ghost' not installed
        imported_modules=["ghost", "roar", "roar.execution.runtime"],
        loaded_files={"ghost": _loaded_as("ghost_alias"), "roar": _loaded_as("roar_rt")},
    )
    assert used == {}


def test_shadowed_import_recorded_through_write_log(tmp_path, monkeypatch):
    """End-to-end through the real capture path: tracking_import records the name,
    write_log builds loaded_files from sys.modules and runs get_used_packages, and
    the aliased package lands in the log's used_packages."""
    from roar.execution.runtime.inject import tracker as tmod

    log_path = tmp_path / "log.json"

    class _Ctl:
        def handle_import(self, *args, **kwargs):
            return None

    tracker = RuntimeInjectionTracker(
        {"ROAR_LOG_FILE": str(log_path)},
        _Ctl(),
        log_file=str(log_path),
        inject_dir=str(tmp_path / "inject"),
    )
    # get_installed_packages now takes excluded_paths (#268); accept and ignore it.
    monkeypatch.setattr(tmod, "get_installed_packages", lambda **_: {"wandb": "0.16.0"})
    monkeypatch.setattr(ilm, "packages_distributions", lambda: {"wandb": ["wandb"]})

    # The shim: `wandb` resolves to a stand-in whose __file__ is trackio's, so the
    # file pass records trackio, not wandb — but the name pass detects the alias.
    stand_in = types.ModuleType("trackio_standin")
    stand_in.__file__ = _loaded_as("trackio")
    monkeypatch.setitem(sys.modules, "wandb", stand_in)
    tracker.tracking_import("wandb")

    tracker.write_log()
    shard = log_path.with_name(f"{log_path.name}.{os.getpid()}")
    written = log_path if log_path.exists() else shard  # canonical, or per-PID shard (P0-9)
    payload = json.loads(written.read_text())
    assert "wandb" in payload["imported_modules"]
    assert payload["used_packages"].get("wandb") == "0.16.0"


def test_roar_is_never_recorded_in_the_freeze_via_file_pass(monkeypatch):
    """P0-11: the file pass must skip roar (roar-cli is installed separately and
    unpinned; a dev build would otherwise pin an unresolvable roar-cli==X.Y.dev0)."""
    monkeypatch.setattr(ilm, "packages_distributions", lambda: {"roar": ["roar-cli"]})
    used = get_used_packages(
        modules_files=["/venv/lib/python3.12/site-packages/roar/__init__.py"],
        installed_packages={"roar-cli": "0.4.4.dev0"},
    )
    assert "roar-cli" not in used and "roar" not in used


class _FakeDist:
    def __init__(self, path):
        self._path = path

    def locate_file(self, rel=""):
        return self._path


def test_self_package_from_editable_is_not_pinned(tmp_path, monkeypatch):
    """P0-12: the workload's own `pip install -e .` package loads from the repo,
    not site-packages, so it isn't aliased and the name pass leaves it out."""
    monkeypatch.setattr(ilm, "packages_distributions", lambda: {"mypkg": ["mypkg"]})
    used = get_used_packages(
        modules_files=[],
        installed_packages={"mypkg": "1.0"},
        imported_modules=["mypkg"],
        loaded_files={"mypkg": str(tmp_path / "repo" / "mypkg" / "__init__.py")},
        workload_root=str(tmp_path / "repo"),
    )
    assert "mypkg" not in used


def test_real_aliased_dep_outside_repo_is_still_pinned(tmp_path, monkeypatch):
    """A genuinely aliased dep whose metadata is outside the repo is still
    recorded (the P0-12 repo skip must not drop it)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    dist_info = tmp_path / "site-packages" / "wandb-0.16.0.dist-info"
    dist_info.mkdir(parents=True)
    monkeypatch.setattr(ilm, "packages_distributions", lambda: {"wandb": ["wandb"]})
    monkeypatch.setattr(ilm, "distribution", lambda name: _FakeDist(dist_info))
    used = get_used_packages(
        modules_files=[],
        installed_packages={"wandb": "0.16.0"},
        imported_modules=["wandb"],
        loaded_files={"wandb": _loaded_as("trackio")},  # aliased
        workload_root=str(repo),
    )
    assert used.get("wandb") == "0.16.0"


def test_dist_packages_import_is_recorded(monkeypatch):
    """P0-18: a package loaded from dist-packages (system Python, e.g. the cert
    AMI) must reach the freeze — the file pass previously only matched
    site-packages, so it was dropped entirely."""
    monkeypatch.setattr(
        ilm, "packages_distributions", lambda: {"huggingface_hub": ["huggingface-hub"]}
    )
    used = get_used_packages(
        modules_files=["/usr/lib/python3/dist-packages/huggingface_hub/__init__.py"],
        installed_packages={"huggingface-hub": "1.27.0"},
    )
    assert used.get("huggingface-hub") == "1.27.0"


def test_failed_probe_import_is_not_recorded_even_with_dist_packages(monkeypatch):
    """P0-13 must stay fixed: a probed import that FAILED (never loaded, so not in
    modules_files/loaded_files — only its name is in imported_modules) is not
    recorded, even now that dist-packages is recognized."""
    monkeypatch.setattr(ilm, "packages_distributions", lambda: {"sagemaker": ["sagemaker-core"]})
    used = get_used_packages(
        modules_files=[],  # import sagemaker failed -> not loaded
        installed_packages={"sagemaker-core": "2.9.0"},
        imported_modules=["sagemaker"],  # name recorded before the failed import
        loaded_files={},  # not in sys.modules
    )
    assert "sagemaker-core" not in used
