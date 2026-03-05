import importlib
import json


def _module():
    return importlib.import_module("roar.cli.commands._ray_job_submit")


def _base_ray_job_submit_command(*, plural: bool = False) -> list[str]:
    noun = "jobs" if plural else "job"
    return [
        "ray",
        noun,
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


def test_non_ray_command_is_unchanged() -> None:
    command = ["python", "main.py"]
    rewritten = _module().maybe_rewrite_ray_job_submit(command)
    assert rewritten.command == command
    assert rewritten.session_id is None


def test_ray_job_submit_injects_pip_with_installed_roar_cli_version(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module, "_resolve_roar_requirement", lambda: "roar-cli==9.9.9")
    monkeypatch.setattr(module, "_resolve_glaas_url", lambda: None)

    rewritten = module.maybe_rewrite_ray_job_submit(_base_ray_job_submit_command())

    runtime_env = _runtime_env_json(rewritten.command)
    assert runtime_env["pip"] == ["roar-cli==9.9.9"]
    assert runtime_env["py_executable"] == "roar-worker"
    assert runtime_env["worker_process_setup_hook"] == "roar.ray.roar_worker._startup"
    assert runtime_env["env_vars"]["ROAR_JOB_INSTRUMENTED"] == "1"
    assert rewritten.session_id is None


def test_ray_jobs_submit_plural_also_works(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module, "_resolve_roar_requirement", lambda: "roar-cli==3.2.1")
    monkeypatch.setattr(module, "_resolve_glaas_url", lambda: None)

    rewritten = module.maybe_rewrite_ray_job_submit(_base_ray_job_submit_command(plural=True))

    runtime_env = _runtime_env_json(rewritten.command)
    assert runtime_env["pip"] == ["roar-cli==3.2.1"]
    assert rewritten.session_id is None


def test_entrypoint_is_wrapped_with_roar_run(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module, "_resolve_roar_requirement", lambda: "roar-cli==1.2.3")
    monkeypatch.setattr(module, "_resolve_glaas_url", lambda: None)

    rewritten = module.maybe_rewrite_ray_job_submit(_base_ray_job_submit_command())

    assert _entrypoint(rewritten.command) == ["roar", "run", "python", "main.py"]
    assert rewritten.session_id is None


def test_existing_runtime_env_json_pip_list_is_merged(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module, "_resolve_roar_requirement", lambda: "roar-cli==7.8.9")
    monkeypatch.setattr(module, "_resolve_glaas_url", lambda: None)

    command = _base_ray_job_submit_command()
    separator_index = command.index("--")
    command[separator_index:separator_index] = [
        "--runtime-env-json",
        json.dumps({"pip": ["numpy==1.26.0", "roar==0.0.1", "roar-cli==0.0.2"]}),
    ]

    rewritten = module.maybe_rewrite_ray_job_submit(command)

    runtime_env = _runtime_env_json(rewritten.command)
    assert runtime_env["pip"] == ["numpy==1.26.0", "roar-cli==7.8.9"]
    assert rewritten.session_id is None


def test_existing_runtime_env_json_env_vars_are_preserved_and_glaas_added(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module, "_resolve_roar_requirement", lambda: "roar-cli==1.0.0")
    monkeypatch.setattr(module, "_resolve_glaas_url", lambda: "https://glaas.example.com")

    command = _base_ray_job_submit_command()
    separator_index = command.index("--")
    command[separator_index:separator_index] = [
        "--runtime-env-json",
        json.dumps({"env_vars": {"USER_KEY": "value"}}),
    ]

    rewritten = module.maybe_rewrite_ray_job_submit(command)

    runtime_env = _runtime_env_json(rewritten.command)
    assert runtime_env["env_vars"]["USER_KEY"] == "value"
    assert runtime_env["env_vars"]["GLAAS_URL"] == "https://glaas.example.com"
    assert runtime_env["env_vars"]["GLAAS_API_URL"] == "https://glaas.example.com"


def test_already_wrapped_entrypoint_is_not_double_wrapped(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module, "_resolve_roar_requirement", lambda: "roar-cli==4.5.6")
    monkeypatch.setattr(module, "_resolve_glaas_url", lambda: None)

    command = [
        "ray",
        "job",
        "submit",
        "--address",
        "http://localhost:8265",
        "--working-dir",
        ".",
        "--",
        "roar",
        "run",
        "python",
        "main.py",
    ]

    rewritten = module.maybe_rewrite_ray_job_submit(command)

    assert _entrypoint(rewritten.command) == ["roar", "run", "python", "main.py"]
    assert rewritten.session_id is None


def test_glaas_url_from_config_is_injected_as_both_env_vars(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module, "_resolve_roar_requirement", lambda: "roar-cli==8.0.0")
    monkeypatch.setattr(module, "_resolve_glaas_url", lambda: "http://localhost:3001")

    rewritten = module.maybe_rewrite_ray_job_submit(_base_ray_job_submit_command())

    runtime_env = _runtime_env_json(rewritten.command)
    assert runtime_env["env_vars"]["GLAAS_URL"] == "http://localhost:3001"
    assert runtime_env["env_vars"]["GLAAS_API_URL"] == "http://localhost:3001"


def test_no_glaas_url_configured_only_instrumentation_env_var_is_injected(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module, "_resolve_roar_requirement", lambda: "roar-cli==8.0.0")
    monkeypatch.setattr(module, "_resolve_glaas_url", lambda: None)

    rewritten = module.maybe_rewrite_ray_job_submit(_base_ray_job_submit_command())

    runtime_env = _runtime_env_json(rewritten.command)
    assert runtime_env["env_vars"] == {"ROAR_JOB_INSTRUMENTED": "1"}
    assert rewritten.session_id is None


def test_ray_job_submit_without_separator_is_unchanged() -> None:
    command = [
        "ray",
        "job",
        "submit",
        "--address",
        "http://localhost:8265",
        "--working-dir",
        ".",
    ]
    rewritten = _module().maybe_rewrite_ray_job_submit(command)
    assert rewritten.command == command
    assert rewritten.session_id is None
