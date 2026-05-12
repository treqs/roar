from __future__ import annotations

from pathlib import Path

import pytest

from roar.backends.osmo import prepare_osmo_workflow_for_lineage


def test_prepare_osmo_workflow_for_lineage_preserves_template_values(tmp_path: Path) -> None:
    input_path = tmp_path / "ray.yaml"
    output_path = tmp_path / "prepared.yaml"
    input_path.write_text(
        """
workflow:
  name: {{workflow_name}}
  resources: {{resources}}
  tasks:
    - name: master
      image: python:3.11-slim

default-values:
  workflow_name: roar-osmo-ray
  resources:
    default:
      cpu: 1
""".strip()
        + "\n",
        encoding="utf-8",
    )

    prepared = prepare_osmo_workflow_for_lineage(
        input_path=input_path,
        output_path=output_path,
        lineage_dataset_name="roar-lineage",
        lineage_bundle_filename="roar-fragments.json",
    )

    rendered = output_path.read_text(encoding="utf-8")
    assert prepared.selected_tasks == ["master"]
    assert prepared.modified_tasks == ["master"]
    assert "name: {{ workflow_name }}" in rendered
    assert "resources: {{ resources }}" in rendered
    assert "name: roar-lineage" in rendered
    assert "path: roar-fragments.json" in rendered


def test_prepare_osmo_workflow_for_lineage_requires_task_selection_for_multi_task_workflow(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "multi.yaml"
    output_path = tmp_path / "prepared.yaml"
    input_path.write_text(
        """
workflow:
  name: sample
  tasks:
    - name: first
    - name: second
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="specify --task"):
        prepare_osmo_workflow_for_lineage(
            input_path=input_path,
            output_path=output_path,
            lineage_dataset_name="roar-lineage",
            lineage_bundle_filename="roar-fragments.json",
        )


def test_prepare_osmo_workflow_for_lineage_only_updates_requested_tasks(tmp_path: Path) -> None:
    input_path = tmp_path / "multi.yaml"
    output_path = tmp_path / "prepared.yaml"
    input_path.write_text(
        """
workflow:
  name: sample
  tasks:
    - name: first
    - name: second
      outputs:
        - dataset:
            name: existing
            path: existing.txt
""".strip()
        + "\n",
        encoding="utf-8",
    )

    prepared = prepare_osmo_workflow_for_lineage(
        input_path=input_path,
        output_path=output_path,
        lineage_dataset_name="roar-lineage",
        lineage_bundle_filename="roar-fragments.json",
        task_names=["second"],
    )

    rendered = output_path.read_text(encoding="utf-8")
    assert prepared.selected_tasks == ["second"]
    assert prepared.modified_tasks == ["second"]
    assert "name: first" in rendered
    first_section, second_section = rendered.split("- name: second", maxsplit=1)
    assert "roar-lineage" not in first_section
    assert "name: existing" in second_section
    assert "name: roar-lineage" in second_section


def test_prepare_osmo_workflow_for_lineage_can_wrap_all_tasks_by_default(tmp_path: Path) -> None:
    input_path = tmp_path / "multi.yaml"
    output_path = tmp_path / "prepared.yaml"
    input_path.write_text(
        """
workflow:
  name: sample
  tasks:
    - name: first
      command: ["python"]
      args: ["first.py"]
    - name: second
      command: ["python"]
      args: ["second.py"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    prepared = prepare_osmo_workflow_for_lineage(
        input_path=input_path,
        output_path=output_path,
        lineage_dataset_name="roar-lineage",
        lineage_bundle_filename="roar-fragments.json",
        inject_runtime_wrapper=True,
        runtime_install_requirement="roar-cli==9.9.9",
        default_to_all_tasks=True,
    )

    rendered = output_path.read_text(encoding="utf-8")
    assert prepared.selected_tasks == ["first", "second"]
    assert prepared.wrapped_tasks == ["first", "second"]
    assert rendered.count("path: /tmp/roar-osmo-wrapper.sh") == 2
    assert rendered.count("name: roar-lineage") == 2


def test_prepare_osmo_workflow_for_lineage_can_inject_runtime_wrapper(tmp_path: Path) -> None:
    input_path = tmp_path / "basic.yaml"
    output_path = tmp_path / "prepared.yaml"
    input_path.write_text(
        """
workflow:
  name: sample
  tasks:
    - name: basic
      command: ["python"]
      args: ["task.py", "{{output}}/result.txt"]
      files:
        - localpath: ./task.py
          path: /workspace/task.py
""".strip()
        + "\n",
        encoding="utf-8",
    )

    prepared = prepare_osmo_workflow_for_lineage(
        input_path=input_path,
        output_path=output_path,
        lineage_dataset_name="roar-lineage",
        lineage_bundle_filename="roar-fragments.json",
        inject_runtime_wrapper=True,
    )

    rendered = output_path.read_text(encoding="utf-8")
    assert prepared.modified_tasks == ["basic"]
    assert prepared.wrapped_tasks == ["basic"]
    assert "command:\n    - bash\n    - /tmp/roar-osmo-wrapper.sh" in rendered
    assert (
        "    - basic\n"
        "    - '{{output}}/roar-fragments.json'\n"
        "    - python\n"
        "    - task.py\n"
        "    - '{{output}}/result.txt'"
    ) in rendered
    assert "path: /tmp/roar-osmo-wrapper.sh" in rendered
    assert "export ROAR_NO_TELEMETRY=1" in rendered
    assert "-m roar run --tracer ptrace --no-tracer-fallback" in rendered
    assert "-m roar osmo export-lineage-bundle" in rendered


def test_prepare_osmo_workflow_for_lineage_can_stage_runtime_bundle(tmp_path: Path) -> None:
    input_path = tmp_path / "basic.yaml"
    output_path = tmp_path / "prepared.yaml"
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

    prepared = prepare_osmo_workflow_for_lineage(
        input_path=input_path,
        output_path=output_path,
        lineage_dataset_name="roar-lineage",
        lineage_bundle_filename="roar-fragments.json",
        inject_runtime_wrapper=True,
        runtime_bundle_local_path="./roar-osmo-runtime.tar.gz",
    )

    rendered = output_path.read_text(encoding="utf-8")
    assert prepared.runtime_bundle_local_path == "./roar-osmo-runtime.tar.gz"
    assert prepared.runtime_bundle_remote_path == "/tmp/roar-osmo-runtime.tar.gz"
    assert "localpath: ./roar-osmo-runtime.tar.gz" in rendered
    assert "path: /tmp/roar-osmo-runtime.tar.gz" in rendered
    assert 'tar -xzf "$runtime_bundle" -C "$runtime_root"' in rendered
    assert 'export PATH="$runtime_root/bin:${PATH:-}"' in rendered
    assert (
        'export PYTHONPATH="$runtime_root/python:$runtime_root/python/site-packages:${PYTHONPATH:-}"'
        in rendered
    )


def test_prepare_osmo_workflow_for_lineage_can_install_roar_runtime(tmp_path: Path) -> None:
    input_path = tmp_path / "basic.yaml"
    output_path = tmp_path / "prepared.yaml"
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

    prepared = prepare_osmo_workflow_for_lineage(
        input_path=input_path,
        output_path=output_path,
        lineage_dataset_name="roar-lineage",
        lineage_bundle_filename="roar-fragments.json",
        inject_runtime_wrapper=True,
        runtime_install_requirement="roar-cli==9.9.9",
    )

    rendered = output_path.read_text(encoding="utf-8")
    assert prepared.runtime_install_requirement == "roar-cli==9.9.9"
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


def test_prepare_osmo_workflow_for_lineage_can_install_roar_runtime_from_local_artifact(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "basic.yaml"
    output_path = tmp_path / "prepared.yaml"
    wheel_path = tmp_path / "dist" / "roar_cli.whl"
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

    prepared = prepare_osmo_workflow_for_lineage(
        input_path=input_path,
        output_path=output_path,
        lineage_dataset_name="roar-lineage",
        lineage_bundle_filename="roar-fragments.json",
        inject_runtime_wrapper=True,
        runtime_install_local_path=str(wheel_path),
    )

    rendered = output_path.read_text(encoding="utf-8")
    assert prepared.runtime_install_requirement is None
    assert prepared.runtime_install_local_path == str(wheel_path)
    assert prepared.runtime_install_remote_path == "/tmp/roar-osmo-install.whl"
    assert f"localpath: {wheel_path}" in rendered
    assert "path: /tmp/roar-osmo-install.whl" in rendered
    assert (
        '"$python_bin" -m pip install --disable-pip-version-check --no-input --target "$install_root" "/tmp/roar-osmo-install.whl"'
        in rendered
    )
    assert "installed roar-cli distribution does not expose roar-tracer" in rendered
    assert "base64.b64decode(payload)" not in rendered
