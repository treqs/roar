import importlib
import json

from roar.backends.ray.submit_context import derive_submit_proxy_port


def _module():
    return importlib.import_module("roar.backends.ray.submit")


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
    rewritten = _module().plan_ray_job_submit_command(command)
    assert rewritten.command == command
    assert rewritten.session_id is None


def test_ray_job_submit_injects_pip_with_installed_roar_cli_version(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module, "_resolve_roar_requirement", lambda: "roar-cli==9.9.9")
    monkeypatch.setattr(module, "_resolve_glaas_url", lambda: None)

    rewritten = module.plan_ray_job_submit_command(_base_ray_job_submit_command())

    runtime_env = _runtime_env_json(rewritten.command)
    assert runtime_env["pip"] == ["roar-cli==9.9.9"]
    assert "py_executable" not in runtime_env
    assert (
        runtime_env["worker_process_setup_hook"]
        == "roar.execution.runtime.worker_bootstrap.startup"
    )
    assert runtime_env["env_vars"]["ROAR_JOB_INSTRUMENTED"] == "1"
    assert rewritten.session_id is None


def test_ray_jobs_submit_plural_also_works(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module, "_resolve_roar_requirement", lambda: "roar-cli==3.2.1")
    monkeypatch.setattr(module, "_resolve_glaas_url", lambda: None)

    rewritten = module.plan_ray_job_submit_command(_base_ray_job_submit_command(plural=True))

    runtime_env = _runtime_env_json(rewritten.command)
    assert runtime_env["pip"] == ["roar-cli==3.2.1"]
    assert rewritten.session_id is None


def test_entrypoint_is_wrapped_with_roar_driver_entrypoint(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module, "_resolve_roar_requirement", lambda: "roar-cli==1.2.3")
    monkeypatch.setattr(module, "_resolve_glaas_url", lambda: None)

    rewritten = module.plan_ray_job_submit_command(_base_ray_job_submit_command())

    assert _entrypoint(rewritten.command) == [
        "python",
        "-m",
        "roar.execution.runtime.driver_entrypoint",
        "--",
        "python",
        "main.py",
    ]
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

    rewritten = module.plan_ray_job_submit_command(command)

    runtime_env = _runtime_env_json(rewritten.command)
    assert runtime_env["pip"] == ["numpy==1.26.0", "roar-cli==7.8.9"]
    assert rewritten.session_id is None


def test_existing_runtime_env_json_pip_list_is_untouched_when_ray_pip_install_false(
    monkeypatch, tmp_path
) -> None:
    module = _module()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(module, "_resolve_roar_requirement", lambda: "roar-cli==7.8.9")
    monkeypatch.setattr(module, "_resolve_glaas_url", lambda: None)

    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    (roar_dir / "config.toml").write_text("[ray]\npip_install = false\n", encoding="utf-8")

    command = _base_ray_job_submit_command()
    separator_index = command.index("--")
    command[separator_index:separator_index] = [
        "--runtime-env-json",
        json.dumps({"pip": ["numpy==1.26.0"]}),
    ]

    rewritten = module.plan_ray_job_submit_command(command)

    runtime_env = _runtime_env_json(rewritten.command)
    assert runtime_env["pip"] == ["numpy==1.26.0"]
    assert (
        runtime_env["worker_process_setup_hook"]
        == "roar.execution.runtime.worker_bootstrap.startup"
    )
    assert rewritten.session_id is None


def test_existing_runtime_env_json_env_vars_are_preserved_and_glaas_url_added(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module, "_resolve_roar_requirement", lambda: "roar-cli==1.0.0")
    monkeypatch.setattr(module, "_resolve_glaas_url", lambda: "https://glaas.example.com")

    command = _base_ray_job_submit_command()
    separator_index = command.index("--")
    command[separator_index:separator_index] = [
        "--runtime-env-json",
        json.dumps({"env_vars": {"USER_KEY": "value"}}),
    ]

    rewritten = module.plan_ray_job_submit_command(command)

    runtime_env = _runtime_env_json(rewritten.command)
    assert runtime_env["env_vars"]["USER_KEY"] == "value"
    assert runtime_env["env_vars"]["GLAAS_URL"] == "https://glaas.example.com"


def test_existing_roar_run_entrypoint_is_unchanged(monkeypatch) -> None:
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

    rewritten = module.plan_ray_job_submit_command(command)

    assert _entrypoint(rewritten.command) == [
        "python",
        "-m",
        "roar.execution.runtime.driver_entrypoint",
        "--",
        "roar",
        "run",
        "python",
        "main.py",
    ]
    assert rewritten.session_id is None


def test_existing_driver_entrypoint_wrapper_is_not_duplicated(monkeypatch) -> None:
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
        "python",
        "-m",
        "roar.execution.runtime.driver_entrypoint",
        "--",
        "python",
        "main.py",
    ]

    rewritten = module.plan_ray_job_submit_command(command)

    assert _entrypoint(rewritten.command) == [
        "python",
        "-m",
        "roar.execution.runtime.driver_entrypoint",
        "--",
        "python",
        "main.py",
    ]
    assert rewritten.session_id is None


def test_glaas_url_from_config_is_injected_into_runtime_env(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module, "_resolve_roar_requirement", lambda: "roar-cli==8.0.0")
    monkeypatch.setattr(module, "_resolve_glaas_url", lambda: "http://localhost:3001")

    rewritten = module.plan_ray_job_submit_command(_base_ray_job_submit_command())

    runtime_env = _runtime_env_json(rewritten.command)
    assert runtime_env["env_vars"]["GLAAS_URL"] == "http://localhost:3001"


def test_cluster_glaas_url_override_is_used_for_runtime_env(monkeypatch) -> None:
    module = _module()
    registrations: list[tuple[str, str, str]] = []
    saved_keys: list[tuple[str, dict[str, str]]] = []
    key = {
        "session_id": "11111111-1111-1111-1111-111111111111",
        "token": "ab" * 32,
        "token_hash": "cd" * 32,
    }

    monkeypatch.setattr(module, "_resolve_roar_requirement", lambda: "roar-cli==8.0.0")
    monkeypatch.setattr(module, "_resolve_glaas_url", lambda: "http://localhost:3001")
    monkeypatch.setattr(
        module,
        "_register_fragment_session",
        lambda glaas_url, session_id, token_hash: registrations.append(
            (glaas_url, session_id, token_hash)
        ),
    )
    monkeypatch.setattr(module, "generate_fragment_key", lambda: key)
    monkeypatch.setattr(
        module,
        "save_key",
        lambda roar_dir, payload: saved_keys.append((str(roar_dir), payload)),
    )
    monkeypatch.setenv("ROAR_CLUSTER_GLAAS_URL", "http://host.docker.internal:3001")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:9000")
    monkeypatch.setenv("ROAR_CLUSTER_AWS_ENDPOINT_URL", "http://minio:9000")

    rewritten = module.plan_ray_job_submit_command(_base_ray_job_submit_command())

    runtime_env = _runtime_env_json(rewritten.command)
    expected_port = derive_submit_proxy_port(runtime_env["env_vars"]["ROAR_JOB_ID"])
    assert runtime_env["env_vars"]["GLAAS_URL"] == "http://host.docker.internal:3001"
    assert runtime_env["env_vars"]["ROAR_UPSTREAM_S3_ENDPOINT"] == "http://minio:9000"
    assert runtime_env["env_vars"]["ROAR_PROXY_PORT"] == str(expected_port)
    assert runtime_env["env_vars"]["AWS_ENDPOINT_URL"] == f"http://127.0.0.1:{expected_port}"
    assert registrations == [
        (
            "http://localhost:3001",
            "11111111-1111-1111-1111-111111111111",
            "cd" * 32,
        )
    ]
    assert saved_keys and saved_keys[0][1] == key
    assert rewritten.session_id == key["session_id"]


def test_no_glaas_url_configured_only_instrumentation_env_var_is_injected(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module, "_resolve_roar_requirement", lambda: "roar-cli==8.0.0")
    monkeypatch.setattr(module, "_resolve_glaas_url", lambda: None)

    rewritten = module.plan_ray_job_submit_command(_base_ray_job_submit_command())

    runtime_env = _runtime_env_json(rewritten.command)
    env = runtime_env["env_vars"]
    expected_port = derive_submit_proxy_port(env["ROAR_JOB_ID"])
    assert env["ROAR_JOB_INSTRUMENTED"] == "1"
    assert env["ROAR_WRAP"] == "1"
    assert env["ROAR_RAY_NODE_AGENTS"] == "1"
    assert env["ROAR_PROXY_PORT"] == str(expected_port)
    assert env["AWS_ENDPOINT_URL"] == f"http://127.0.0.1:{expected_port}"
    assert "ROAR_JOB_ID" in env  # stable job_id for node agent name resolution
    assert rewritten.session_id is None


def test_blank_glaas_url_does_not_enable_fragment_session(monkeypatch) -> None:
    module = _module()
    glaas_client = importlib.import_module("roar.glaas_client")

    monkeypatch.setattr(module, "_resolve_roar_requirement", lambda: "roar-cli==8.0.0")
    monkeypatch.setattr(glaas_client, "get_glaas_url", lambda: "")
    monkeypatch.setattr(
        module,
        "_register_fragment_session",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("fragment preregistration should not run without glaas.url")
        ),
    )

    rewritten = module.plan_ray_job_submit_command(_base_ray_job_submit_command())

    runtime_env = _runtime_env_json(rewritten.command)
    env = runtime_env["env_vars"]
    assert "GLAAS_URL" not in env
    assert "ROAR_SESSION_ID" not in env
    assert "ROAR_FRAGMENT_TOKEN" not in env
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
    rewritten = _module().plan_ray_job_submit_command(command)
    assert rewritten.command == command
    assert rewritten.session_id is None
