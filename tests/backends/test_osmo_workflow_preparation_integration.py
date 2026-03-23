from __future__ import annotations

import tarfile
from pathlib import Path


def test_roar_osmo_prepare_workflow_uses_configured_lineage_contract(
    temp_git_repo: Path,
    roar_cli,
) -> None:
    input_path = temp_git_repo / "workflow.yaml"
    output_path = temp_git_repo / "prepared" / "workflow.yaml"
    input_path.write_text(
        """
workflow:
  name: {{workflow_name}}
  tasks:
    - name: first
      image: python:3.11-slim
    - name: second
      image: python:3.11-slim

default-values:
  workflow_name: roar-osmo-prepare
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config_path = temp_git_repo / ".roar" / "config.toml"
    config_path.write_text(
        "[osmo]\n"
        'lineage_bundle_dataset_name = "custom-lineage"\n'
        'lineage_bundle_filename = "custom-bundle.json"\n',
        encoding="utf-8",
    )

    result = roar_cli(
        "osmo",
        "prepare-workflow",
        "workflow.yaml",
        "prepared/workflow.yaml",
        "--task",
        "second",
    )

    assert result.returncode == 0
    assert "prepared/workflow.yaml" in result.stdout

    rendered = output_path.read_text(encoding="utf-8")
    assert "name: {{ workflow_name }}" in rendered
    first_section, second_section = rendered.split("- name: second", maxsplit=1)
    assert "custom-lineage" not in first_section
    assert "name: custom-lineage" in second_section
    assert "path: custom-bundle.json" in second_section


def test_roar_osmo_prepare_workflow_can_inject_runtime_wrapper(
    temp_git_repo: Path,
    roar_cli,
) -> None:
    input_path = temp_git_repo / "workflow.yaml"
    output_path = temp_git_repo / "prepared" / "workflow.yaml"
    input_path.write_text(
        """
workflow:
  name: sample
  tasks:
    - name: basic
      command: ["python"]
      args: ["task.py", "{{output}}/result.txt"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    result = roar_cli(
        "osmo",
        "prepare-workflow",
        "workflow.yaml",
        "prepared/workflow.yaml",
        "--inject-runtime-wrapper",
    )

    assert result.returncode == 0
    assert "Wrapped tasks: basic" in result.stdout

    rendered = output_path.read_text(encoding="utf-8")
    assert "path: /tmp/roar-osmo-wrapper.sh" in rendered
    assert "contents: |" in rendered
    assert "-m roar run --tracer ptrace --no-tracer-fallback" in rendered
    assert "-m roar osmo export-lineage-bundle" in rendered
    assert "{{output}}/roar-fragments.json" in rendered


def test_roar_osmo_prepare_workflow_can_stage_runtime_bundle(
    temp_git_repo: Path,
    roar_cli,
) -> None:
    input_path = temp_git_repo / "workflow.yaml"
    output_path = temp_git_repo / "prepared" / "workflow.yaml"
    fake_roar = temp_git_repo / "fake-runtime" / "roar"
    fake_site = temp_git_repo / "fake-runtime" / "site-packages"
    fake_tracer = temp_git_repo / "fake-runtime" / "bin" / "roar-tracer"
    input_path.write_text(
        """
workflow:
  name: sample
  tasks:
    - name: basic
      command: ["python"]
      args: ["task.py"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (fake_roar / "cli").mkdir(parents=True)
    (fake_roar / "__main__.py").write_text("print('roar')\n", encoding="utf-8")
    (fake_roar / "cli" / "__init__.py").write_text("", encoding="utf-8")
    (fake_site / "click").mkdir(parents=True)
    (fake_site / "click" / "__init__.py").write_text("", encoding="utf-8")
    fake_tracer.parent.mkdir(parents=True)
    fake_tracer.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    result = roar_cli(
        "osmo",
        "prepare-workflow",
        "workflow.yaml",
        "prepared/workflow.yaml",
        "--inject-runtime-wrapper",
        "--stage-roar-runtime",
        "--runtime-roar-package-dir",
        str(fake_roar),
        "--runtime-python-root",
        str(fake_site),
        "--runtime-tracer",
        str(fake_tracer),
    )

    assert result.returncode == 0
    assert "Staged runtime bundle:" in result.stdout

    bundle_path = output_path.parent / "roar-osmo-runtime.tar.gz"
    rendered = output_path.read_text(encoding="utf-8")
    assert bundle_path.exists()
    assert "localpath: roar-osmo-runtime.tar.gz" in rendered
    assert "path: /tmp/roar-osmo-runtime.tar.gz" in rendered
    assert 'tar -xzf "$runtime_bundle" -C "$runtime_root"' in rendered

    with tarfile.open(bundle_path, "r:gz") as archive:
        members = set(archive.getnames())
    assert "python/roar/__main__.py" in members
    assert "python/site-packages/click/__init__.py" in members
    assert "bin/roar-tracer" in members


def test_roar_osmo_prepare_workflow_can_install_roar_runtime(
    temp_git_repo: Path,
    roar_cli,
) -> None:
    input_path = temp_git_repo / "workflow.yaml"
    output_path = temp_git_repo / "prepared" / "workflow.yaml"
    input_path.write_text(
        """
workflow:
  name: sample
  tasks:
    - name: basic
      command: ["python"]
      args: ["task.py"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    result = roar_cli(
        "osmo",
        "prepare-workflow",
        "workflow.yaml",
        "prepared/workflow.yaml",
        "--inject-runtime-wrapper",
        "--install-roar-runtime",
        "--runtime-install-requirement",
        "roar-cli==9.9.9",
    )

    assert result.returncode == 0

    rendered = output_path.read_text(encoding="utf-8")
    assert 'install_root="/tmp/roar-osmo-python"' in rendered
    assert 'if ! "$python_bin" -m pip --version >/dev/null 2>&1; then' in rendered
    assert 'if ! "$python_bin" -m ensurepip --user >/dev/null 2>&1; then' in rendered
    assert 'pip_command="$PYTHONUSERBASE/bin/pip3"' in rendered
    assert (
        '"$python_bin" -m pip install --disable-pip-version-check --no-input --target "$install_root" "roar-cli==9.9.9"'
        in rendered
    )
    assert 'export PYTHONPATH="$install_root:${PYTHONPATH:-}"' in rendered
    assert "installed roar-cli distribution does not expose roar-tracer" in rendered
    assert "find_ptrace_tracer" in rendered
    assert "urlopen(tracer_url)" not in rendered


def test_roar_osmo_prepare_workflow_can_install_roar_runtime_from_local_artifact(
    temp_git_repo: Path,
    roar_cli,
) -> None:
    input_path = temp_git_repo / "workflow.yaml"
    output_path = temp_git_repo / "prepared" / "workflow.yaml"
    wheel_path = temp_git_repo / "dist" / "roar_cli.whl"
    wheel_path.parent.mkdir(parents=True, exist_ok=True)
    wheel_path.write_bytes(b"\xfcwheel")
    input_path.write_text(
        """
workflow:
  name: sample
  tasks:
    - name: basic
      command: ["python"]
      args: ["task.py"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    result = roar_cli(
        "osmo",
        "prepare-workflow",
        "workflow.yaml",
        "prepared/workflow.yaml",
        "--inject-runtime-wrapper",
        "--install-roar-runtime",
        "--runtime-install-local-path",
        str(wheel_path),
    )

    assert result.returncode == 0

    rendered = output_path.read_text(encoding="utf-8")
    assert "Runtime install artifact:" in result.stdout
    assert "localpath: ../dist/roar_cli.whl" in rendered
    assert "path: /tmp/roar-osmo-install.whl" in rendered
    assert (
        '"$python_bin" -m pip install --disable-pip-version-check --no-input --target "$install_root" "/tmp/roar-osmo-install.whl"'
        in rendered
    )
    assert "installed roar-cli distribution does not expose roar-tracer" in rendered
    assert "base64.b64decode(payload)" not in rendered
