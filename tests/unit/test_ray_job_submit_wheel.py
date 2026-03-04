import importlib
import importlib.metadata as importlib_metadata
import json
import os


def _module():
    return importlib.import_module("roar.cli.commands._ray_job_submit")


def _base_ray_job_submit_command() -> list[str]:
    return [
        "ray",
        "job",
        "submit",
        "--address",
        "http://localhost:8265",
        "--working-dir",
        ".",
        "--",
        "python",
        "main.py",
    ]


def _runtime_env_json(command: list[str]) -> dict:
    for index, arg in enumerate(command):
        if arg == "--runtime-env-json" and index + 1 < len(command):
            return json.loads(command[index + 1])
        if arg.startswith("--runtime-env-json="):
            return json.loads(arg.split("=", 1)[1])
    raise AssertionError("expected --runtime-env-json in rewritten command")


def test_resolve_roar_requirement_prefers_vendor_wheel(tmp_path, monkeypatch) -> None:
    module = _module()

    wheel_path = tmp_path / "vendor" / "roar-cli.whl"
    wheel_path.parent.mkdir(parents=True)
    wheel_path.write_bytes(b"wheel")

    monkeypatch.setattr(os, "getcwd", lambda: str(tmp_path))

    requirement = module._resolve_roar_requirement()

    assert requirement == f"roar-cli @ file://{wheel_path.resolve()}"


def test_resolve_roar_requirement_falls_back_to_pypi_when_no_wheel(tmp_path, monkeypatch) -> None:
    module = _module()

    monkeypatch.setattr(os, "getcwd", lambda: str(tmp_path))

    def _fake_version(package_name: str) -> str:
        if package_name == "roar-cli":
            return "9.9.9"
        raise importlib_metadata.PackageNotFoundError(package_name)

    monkeypatch.setattr(importlib_metadata, "version", _fake_version)

    requirement = module._resolve_roar_requirement()

    assert requirement == "roar-cli==9.9.9"


def test_maybe_rewrite_ray_job_submit_uses_vendor_wheel_requirement(tmp_path, monkeypatch) -> None:
    module = _module()

    wheel_path = tmp_path / "vendor" / "roar-cli.whl"
    wheel_path.parent.mkdir(parents=True)
    wheel_path.write_bytes(b"wheel")

    monkeypatch.setattr(os, "getcwd", lambda: str(tmp_path))
    monkeypatch.setattr(module, "_resolve_glaas_url", lambda: None)

    rewritten = module.maybe_rewrite_ray_job_submit(_base_ray_job_submit_command())

    runtime_env = _runtime_env_json(rewritten)
    assert runtime_env["pip"] == [f"roar-cli @ file://{wheel_path.resolve()}"]
