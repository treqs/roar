"""Guard roar's declared runtime dependencies for shipped code paths."""

from __future__ import annotations

from importlib import metadata


def test_huggingface_hub_is_a_runtime_dependency():
    """P0-19: `roar put hf://` imports huggingface_hub, so it must be a RUNTIME
    dependency, not a dev-only extra. When it lived under the `dev` extra,
    `uv tool install <wheel>` (the mandated install path) omitted it and
    `roar put hf://` died with ModuleNotFoundError *after* every step succeeded.
    """
    reqs = metadata.requires("roar-cli") or []
    hf = [r for r in reqs if r.lower().replace("_", "-").startswith("huggingface-hub")]
    assert hf, f"huggingface_hub missing from roar-cli requirements: {reqs}"
    # It must not be gated behind an extra (e.g. `; extra == "dev"`).
    behind_extra = [r for r in hf if "extra ==" in r or "extra==" in r]
    assert not behind_extra, f"huggingface_hub is still behind an extra: {behind_extra}"
