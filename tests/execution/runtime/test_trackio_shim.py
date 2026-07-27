"""Unit tests for the wandb/mlflow -> trackio injection shim ([70])."""

import builtins
import sys
import types

import pytest

from roar.execution.runtime.inject.trackio_shim import install_trackio_shim


@pytest.fixture
def fake_trackio(monkeypatch):
    tk = types.ModuleType("trackio")
    tk.inits = []
    tk.logged = []
    tk.init = lambda *a, **k: tk.inits.append(k)
    tk.log = lambda d, step=None: tk.logged.append((dict(d), step))
    tk.finish = lambda: None
    monkeypatch.setitem(sys.modules, "trackio", tk)
    monkeypatch.delitem(sys.modules, "wandb", raising=False)
    monkeypatch.delitem(sys.modules, "mlflow", raising=False)
    return tk


def test_disabled_by_default(fake_trackio):
    assert install_trackio_shim({}) is False
    assert "wandb" not in sys.modules


def test_installs_wandb_and_mlflow_with_space(fake_trackio):
    ok = install_trackio_shim(
        {"ROAR_CAPTURE_TRACKIO": "1", "ROAR_TRACKIO_SPACE_ID": "reproducible-ai/experiments"}
    )
    assert ok is True
    assert sys.modules["wandb"] is fake_trackio
    assert "mlflow" in sys.modules
    # wandb (== trackio) init injects the configured space_id
    sys.modules["wandb"].init(project="m", config={"lr": 0.01})
    assert fake_trackio.inits[-1].get("space_id") == "reproducible-ai/experiments"


def test_mlflow_shim_funnels_metrics(fake_trackio):
    install_trackio_shim({"ROAR_CAPTURE_TRACKIO": "1", "ROAR_TRACKIO_SPACE_ID": "org/space"})
    mlflow = sys.modules["mlflow"]
    mlflow.set_experiment("exp1")
    with mlflow.start_run():
        mlflow.log_metric("loss", 0.5, step=1)
        mlflow.log_params({"lr": 0.01})  # no-op; must not raise
    assert fake_trackio.inits[-1].get("project") == "exp1"
    assert ({"loss": 0.5}, 1) in fake_trackio.logged


def test_does_not_clobber_a_real_wandb(fake_trackio):
    sentinel = types.ModuleType("wandb")
    sys.modules["wandb"] = sentinel
    install_trackio_shim({"ROAR_CAPTURE_TRACKIO": "1"})
    assert sys.modules["wandb"] is sentinel  # setdefault preserved the prior import


def test_missing_trackio_is_a_noop(monkeypatch):
    monkeypatch.delitem(sys.modules, "trackio", raising=False)
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "trackio":
            raise ImportError("no trackio")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert install_trackio_shim({"ROAR_CAPTURE_TRACKIO": "1"}) is False
