"""Unit tests for the optional wandb -> trackio integration.

Exercises the env-driven mode selection and the aliasing behavior without needing
a GPU, a real wandb account, or a live trackio Space.
"""

from __future__ import annotations

import sys
import types

import pytest

from roar.integrations import wandb_trackio


@pytest.fixture(autouse=True)
def _restore_modules():
    saved = {k: sys.modules.get(k) for k in ("wandb", "trackio")}
    for k in ("wandb", "trackio"):
        sys.modules.pop(k, None)
    yield
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


def test_flag_unset_is_a_noop():
    wandb_trackio.install(environ={})
    assert "wandb" not in sys.modules


def test_off_installs_noop_wandb():
    wandb_trackio.install(environ={"ROAR_WANDB_TO_TRACKIO": "off"})
    import wandb  # now aliased to the no-op

    run = wandb.init(project="p", name="n", config={"lr": 1})
    assert wandb.log({"loss": 0.1}) is None
    run.summary["best"] = 1  # `run.summary[...] = x` must not raise
    assert wandb.run is run
    assert wandb.finish() is None
    assert wandb.watch(object()) is None  # unsupported surface -> no-op
    assert wandb.Image(b"x") is None  # data-type ctor -> no-op


def test_auto_without_space_is_noop():
    wandb_trackio.install(environ={"ROAR_WANDB_TO_TRACKIO": "1"})
    assert sys.modules["wandb"].__name__ == "wandb"
    run = sys.modules["wandb"].init()
    run.summary["x"] = 1  # no-op run still behaves


def test_sync_aliases_to_trackio_and_strips_wandb_only_kwargs():
    calls: dict = {}
    fake = types.ModuleType("trackio")
    fake.init = lambda *a, **k: calls.__setitem__("init", k) or types.SimpleNamespace(summary={})
    fake.log = lambda *a, **k: calls.__setitem__("log", (a, k))
    sys.modules["trackio"] = fake

    wandb_trackio.install(environ={"ROAR_WANDB_TO_TRACKIO": "1", "TRACKIO_SPACE_ID": "org/space"})
    import wandb

    assert wandb is fake  # wandb resolves to trackio
    wandb.init(project="p", entity="drop-me", tags=["a"])
    assert calls["init"].get("space_id") == "org/space"
    assert "entity" not in calls["init"] and "tags" not in calls["init"]
    wandb.log({"loss": 0.5}, commit=True)
    assert "commit" not in calls["log"][1]  # wandb-only log kwarg stripped


def test_off_beats_a_configured_space():
    fake = types.ModuleType("trackio")
    fake.init = lambda *a, **k: None
    fake.log = lambda *a, **k: None
    sys.modules["trackio"] = fake
    wandb_trackio.install(environ={"ROAR_WANDB_TO_TRACKIO": "off", "TRACKIO_SPACE_ID": "org/space"})
    assert sys.modules["wandb"] is not fake  # forced no-op, not the trackio alias


def test_does_not_clobber_existing_wandb():
    sentinel = types.ModuleType("wandb")
    sys.modules["wandb"] = sentinel
    wandb_trackio.install(environ={"ROAR_WANDB_TO_TRACKIO": "off"})
    assert sys.modules["wandb"] is sentinel


def test_noop_wandb_has_a_spec_so_find_spec_does_not_raise():
    """P0-15: a __spec__=None module makes importlib.util.find_spec RAISE, which
    crashes `import accelerate` (is_wandb_available) on any credential-free host.
    The no-op stub must carry a real spec."""
    import importlib.util

    wandb_trackio.install(environ={"ROAR_WANDB_TO_TRACKIO": "off"})
    stub = sys.modules["wandb"]
    assert stub.__spec__ is not None
    # This raised ValueError("wandb.__spec__ is None") before the fix.
    assert importlib.util.find_spec("wandb") is stub.__spec__
