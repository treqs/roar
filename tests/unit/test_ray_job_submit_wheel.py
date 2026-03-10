import importlib
import importlib.metadata as importlib_metadata
import json


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


def _entrypoint(command: list[str]) -> list[str]:
    separator_index = command.index("--")
    return command[separator_index + 1 :]


def test_resolve_roar_requirement_ignores_vendor_wheel_and_uses_installed_version(
    tmp_path, monkeypatch
) -> None:
    module = _module()

    wheel_path = tmp_path / "vendor" / "roar-cli.whl"
    wheel_path.parent.mkdir(parents=True)
    wheel_path.write_bytes(b"wheel")

    monkeypatch.setattr(
        importlib_metadata,
        "version",
        lambda package_name: "9.9.9" if package_name == "roar-cli" else "0.0.0",
    )

    requirement = module._resolve_roar_requirement()

    assert requirement == "roar-cli==9.9.9"


def test_resolve_roar_requirement_falls_back_to_unpinned_package_when_version_missing(
    monkeypatch,
) -> None:
    module = _module()

    def _fake_version(package_name: str) -> str:
        raise importlib_metadata.PackageNotFoundError(package_name)

    monkeypatch.setattr(importlib_metadata, "version", _fake_version)

    requirement = module._resolve_roar_requirement()

    assert requirement == "roar-cli"


def test_maybe_rewrite_injects_pip_even_when_vendor_wheel_exists(tmp_path, monkeypatch) -> None:
    module = _module()

    wheel_path = tmp_path / "vendor" / "roar-cli.whl"
    wheel_path.parent.mkdir(parents=True)
    wheel_path.write_bytes(b"wheel")

    monkeypatch.setattr(module, "_resolve_roar_requirement", lambda: "roar-cli==1.2.3")
    monkeypatch.setattr(module, "_resolve_glaas_url", lambda: None)

    rewritten = module.maybe_rewrite_ray_job_submit(_base_ray_job_submit_command())

    assert _entrypoint(rewritten.command) == [
        "python",
        "-m",
        "roar.ray.driver_entrypoint",
        "--",
        "python",
        "main.py",
    ]
    env = _runtime_env_json(rewritten.command)
    assert env["pip"] == ["roar-cli==1.2.3"]
    assert env["env_vars"]["ROAR_JOB_INSTRUMENTED"] == "1"
    assert rewritten.session_id is None
