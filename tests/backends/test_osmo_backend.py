from __future__ import annotations

import importlib

from roar.cli.commands.init import build_default_config_template
from roar.execution.framework.planning import plan_execution_command
from roar.execution.framework.registry import (
    iter_execution_backend_configurable_keys,
    iter_execution_backends,
)


def _module():
    return importlib.import_module("roar.backends.osmo.submit")


def test_matches_osmo_workflow_submit_command() -> None:
    assert _module().matches_osmo_workflow_submit_command(
        ["osmo", "workflow", "submit", "workflow.yaml"]
    )


def test_non_submit_osmo_commands_do_not_match_osmo_backend() -> None:
    assert not _module().matches_osmo_workflow_submit_command(
        ["osmo", "workflow", "logs", "workflow-123"]
    )
    assert not _module().matches_osmo_workflow_submit_command(
        ["osmo", "dataset", "download", "dataset:latest", "./downloads"]
    )


def test_osmo_workflow_submit_command_is_planned_as_submit_role() -> None:
    command = ["osmo", "workflow", "submit", "workflow.yaml", "--format-type", "json"]

    planned = _module().plan_osmo_workflow_submit_command(command)

    assert planned.backend_name == "osmo"
    assert planned.command == command
    assert planned.execution_role == "submit"
    assert planned.session_id is None
    assert planned.finalize_run is None


def test_osmo_workflow_submit_command_appends_json_output_flag_by_default() -> None:
    command = ["osmo", "workflow", "submit", "workflow.yaml"]

    planned = _module().plan_osmo_workflow_submit_command(command)

    assert planned.command == [
        "osmo",
        "workflow",
        "submit",
        "workflow.yaml",
        "--format-type",
        "json",
    ]


def test_osmo_workflow_submit_preserves_explicit_format_type() -> None:
    command = ["osmo", "workflow", "submit", "workflow.yaml", "--format-type", "yaml"]

    planned = _module().plan_osmo_workflow_submit_command(command)

    assert planned.command == command


def test_plan_execution_command_prefers_osmo_backend_for_osmo_workflow_submit() -> None:
    planned = plan_execution_command(["osmo", "workflow", "submit", "workflow.yaml"])

    assert planned.backend_name == "osmo"
    assert planned.execution_role == "submit"
    assert planned.command[-2:] == ["--format-type", "json"]


def test_non_submit_osmo_commands_fall_back_to_local_backend() -> None:
    planned = plan_execution_command(["osmo", "workflow", "logs", "workflow-123"])

    assert planned.backend_name == "local"
    assert planned.execution_role == "host"


def test_disabled_osmo_backend_falls_back_to_local_backend(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    (roar_dir / "config.toml").write_text("[osmo]\nenabled = false\n", encoding="utf-8")

    planned = plan_execution_command(["osmo", "workflow", "submit", "workflow.yaml"])

    assert planned.backend_name == "local"
    assert planned.execution_role == "host"


def test_force_json_output_can_be_disabled(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    (roar_dir / "config.toml").write_text(
        "[osmo]\nforce_json_output = false\n",
        encoding="utf-8",
    )

    planned = plan_execution_command(["osmo", "workflow", "submit", "workflow.yaml"])

    assert planned.backend_name == "osmo"
    assert planned.command == ["osmo", "workflow", "submit", "workflow.yaml"]


def test_osmo_backend_is_registered_with_framework_config() -> None:
    backend_names = [backend.name for backend in iter_execution_backends()]

    assert "osmo" in backend_names
    assert "osmo.enabled" in iter_execution_backend_configurable_keys()
    assert "osmo.auto_prepare_submissions" in iter_execution_backend_configurable_keys()
    assert "osmo.force_json_output" in iter_execution_backend_configurable_keys()
    assert "osmo.wait_for_completion" in iter_execution_backend_configurable_keys()
    assert "osmo.download_declared_outputs" in iter_execution_backend_configurable_keys()
    assert "osmo.download_directory" in iter_execution_backend_configurable_keys()
    assert "osmo.ingest_lineage_bundles" in iter_execution_backend_configurable_keys()
    assert "osmo.lineage_bundle_dataset_name" in iter_execution_backend_configurable_keys()
    assert "osmo.lineage_bundle_filename" in iter_execution_backend_configurable_keys()
    assert "osmo.runtime_install_requirement" in iter_execution_backend_configurable_keys()
    assert "osmo.runtime_install_local_path" in iter_execution_backend_configurable_keys()
    assert "osmo.runtime_install_remote_path" in iter_execution_backend_configurable_keys()


def test_init_template_includes_osmo_section() -> None:
    template = build_default_config_template()

    assert "[osmo]" in template
    assert "enabled = true" in template
    assert "auto_prepare_submissions = true" in template
    assert "force_json_output = true" in template
    assert "wait_for_completion = false" in template
    assert "download_declared_outputs = false" in template
    assert "ingest_lineage_bundles = false" in template
    assert 'lineage_bundle_dataset_name = "roar-lineage"' in template
    assert 'runtime_install_requirement = ""' in template
    assert 'runtime_install_local_path = ""' in template
