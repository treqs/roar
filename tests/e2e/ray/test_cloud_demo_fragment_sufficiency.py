"""Cloud-demo-shaped fragment sufficiency contract through Ray submit."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest

from tests.e2e.ray.conftest import (
    decrypt_fragment_batches,
    fetch_fragment_batches,
    init_host_project,
    load_fragment_key,
    make_host_project_dir,
    query_roar_db,
    run_roar_cli_from_host,
    run_roar_ray_job_from_host,
)

pytestmark = [pytest.mark.e2e, pytest.mark.ray_contract, pytest.mark.timeout(300)]

EXPECTED_TASK_COUNTS = {
    "cloud_demo_emulated.workload.extraction.generate_sensor_shard": 25,
    "cloud_demo_emulated.workload.training.train_on_shard": 25,
    "cloud_demo_emulated.workload.evaluation.evaluate_shard": 20,
}
PROXY_FUNCTIONS = {
    "unknown",
    "s3_proxy",
    "s3_driver_proxy",
    "roar.ray.node_agent.RoarNodeAgent.__init__",
}
EXPECTED_PHASE_COMMANDS = (
    "ray_task:extraction",
    "ray_task:training",
    "ray_task:evaluation",
)


def _parse_payload(stdout: str) -> dict[str, object]:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("script") == "cloud_demo_emulated":
            return payload
    raise AssertionError(f"Unable to parse cloud-demo-emulated payload from output:\n{stdout}")


@pytest.fixture(scope="module")
def cloud_demo_emulated_fragments(ray_cluster: dict[str, str]) -> dict[str, object]:
    project_dir = make_host_project_dir("cloud-demo-emulated")
    init_host_project(project_dir)

    result = run_roar_ray_job_from_host(
        project_dir,
        ray_cluster,
        "cloud_demo_emulated/main.py",
        use_fragment_store=True,
        extra_env={
            "S3_DATA_BUCKET": "test-bucket",
            "S3_MODELS_BUCKET": "output-bucket",
            "S3_RESULTS_BUCKET": "output-bucket",
        },
        timeout=300,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    payload = _parse_payload(result.stdout)
    key_payload = load_fragment_key(project_dir)
    batches = fetch_fragment_batches(key_payload["session_id"], key_payload["token"])
    fragments = decrypt_fragment_batches(batches, key_payload["token"])
    return {
        "project_dir": project_dir,
        "payload": payload,
        "fragments": fragments,
    }


def _unique_task_counts(fragments: list[dict[str, object]]) -> dict[str, int]:
    job_uids_by_name: dict[str, set[str]] = defaultdict(set)
    for fragment in fragments:
        name = str(fragment.get("function_name") or "unknown")
        job_uid = str(fragment.get("job_uid") or "")
        if job_uid:
            job_uids_by_name[name].add(job_uid)
    return {name: len(job_uids) for name, job_uids in job_uids_by_name.items()}


def _paths_for_function(
    fragments: list[dict[str, object]],
    function_name: str,
    field: str,
) -> set[str]:
    paths: set[str] = set()
    for fragment in fragments:
        if str(fragment.get("function_name") or "unknown") != function_name:
            continue
        refs = fragment.get(field)
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            path = str(ref.get("path") or "")
            if path:
                paths.add(path)
    return paths


def _paths_for_non_proxy_fragments(fragments: list[dict[str, object]], field: str) -> set[str]:
    paths: set[str] = set()
    for fragment in fragments:
        function_name = str(fragment.get("function_name") or "unknown")
        if function_name in PROXY_FUNCTIONS:
            continue
        refs = fragment.get(field)
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            path = str(ref.get("path") or "")
            if path:
                paths.add(path)
    return paths


def _phase_jobs(project_dir: Path) -> list[dict[str, object]]:
    return query_roar_db(
        project_dir,
        """
        SELECT id, step_number, command, script, job_uid
        FROM jobs
        WHERE job_type = 'ray_task'
          AND command IN ('ray_task:extraction', 'ray_task:training', 'ray_task:evaluation')
        ORDER BY step_number, id
        """,
    )


def _step_numbers_for_command(project_dir: Path, command: str) -> set[int]:
    return {
        int(row["step_number"])
        for row in query_roar_db(
            project_dir,
            """
            SELECT step_number
            FROM jobs
            WHERE job_type = 'ray_task' AND command = ?
            """,
            (command,),
        )
    }


def _dag_payload(project_dir: Path, *args: str) -> dict[str, object]:
    result = run_roar_cli_from_host(project_dir, "dag", *args, timeout=30)
    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict), result.stdout
    return payload


def test_cloud_demo_fragments_are_sufficient_for_phase_lineage(
    cloud_demo_emulated_fragments: dict[str, object],
) -> None:
    payload = cloud_demo_emulated_fragments["payload"]
    assert isinstance(payload, dict)
    fragments = cloud_demo_emulated_fragments["fragments"]
    assert isinstance(fragments, list)

    task_counts = _unique_task_counts(fragments)
    for function_name, expected_count in EXPECTED_TASK_COUNTS.items():
        assert task_counts.get(function_name, 0) == expected_count, (
            "Expected the fragment session to contain one named task lineage per real pipeline task. "
            f"function_name={function_name!r}, expected_count={expected_count}, observed={task_counts.get(function_name, 0)}"
        )

    extraction_writes = _paths_for_function(
        fragments,
        "cloud_demo_emulated.workload.extraction.generate_sensor_shard",
        "writes",
    )
    assert len([path for path in extraction_writes if "sensor_data/shard_" in path]) == 25, (
        "Expected extraction task fragments to own the shard parquet writes needed for replayable lineage, "
        f"observed_paths={sorted(extraction_writes)}"
    )

    training_reads = _paths_for_function(
        fragments,
        "cloud_demo_emulated.workload.training.train_on_shard",
        "reads",
    )
    assert len([path for path in training_reads if "sensor_data/shard_" in path]) == 25, (
        "Expected training task fragments to record shard parquet reads, "
        f"observed_paths={sorted(training_reads)}"
    )

    evaluation_reads = _paths_for_function(
        fragments,
        "cloud_demo_emulated.workload.evaluation.evaluate_shard",
        "reads",
    )
    shard_reads = [path for path in evaluation_reads if "sensor_data/shard_" in path]
    model_reads = [path for path in evaluation_reads if "sensor_predictor_final.json" in path]
    assert len(shard_reads) == 20, (
        "Expected evaluation task fragments to read the evaluated shard set, "
        f"observed_paths={sorted(evaluation_reads)}"
    )
    assert model_reads, (
        "Expected evaluation task fragments to read the trained model artifact, "
        f"observed_paths={sorted(evaluation_reads)}"
    )

    non_proxy_writes = _paths_for_non_proxy_fragments(fragments, "writes")
    model_key = str(payload.get("model_key") or "")
    metrics_key = str(payload.get("metrics_key") or "")
    assert model_key, payload
    assert metrics_key, payload
    assert any(model_key in path for path in non_proxy_writes), (
        "Expected a named non-proxy fragment to own the final model write. "
        f"model_key={model_key!r}, observed_non_proxy_writes={sorted(non_proxy_writes)}"
    )
    assert any(metrics_key in path for path in non_proxy_writes), (
        "Expected a named non-proxy fragment to own the evaluation metrics write. "
        f"metrics_key={metrics_key!r}, observed_non_proxy_writes={sorted(non_proxy_writes)}"
    )


def test_cloud_demo_reconstitution_keeps_phase_outputs_on_named_jobs(
    cloud_demo_emulated_fragments: dict[str, object],
) -> None:
    project_dir = cloud_demo_emulated_fragments["project_dir"]
    assert isinstance(project_dir, Path)
    payload = cloud_demo_emulated_fragments["payload"]
    assert isinstance(payload, dict)

    phase_jobs = _phase_jobs(project_dir)
    observed_commands = {str(row["command"]) for row in phase_jobs}
    missing_commands = [command for command in EXPECTED_PHASE_COMMANDS if command not in observed_commands]
    assert not missing_commands, (
        "Expected all phase task families in the reconstituted DB, "
        f"missing={missing_commands}, observed={sorted(observed_commands)}"
    )

    extract_steps = _step_numbers_for_command(project_dir, "ray_task:extraction")
    train_steps = _step_numbers_for_command(project_dir, "ray_task:training")
    evaluate_steps = _step_numbers_for_command(project_dir, "ray_task:evaluation")
    assert extract_steps == {2}, phase_jobs
    assert train_steps == {3}, phase_jobs
    assert evaluate_steps == {4}, phase_jobs

    model_key = str(payload.get("model_key") or "")
    metrics_key = str(payload.get("metrics_key") or "")
    assert model_key, payload
    assert metrics_key, payload

    output_rows = query_roar_db(
        project_dir,
        """
        SELECT j.command, jo.path
        FROM job_outputs jo
        JOIN jobs j ON j.id = jo.job_id
        WHERE jo.path LIKE ? OR jo.path LIKE ?
        ORDER BY jo.path, j.command
        """,
        (f"%{model_key}", f"%{metrics_key}"),
    )
    assert output_rows, (
        "Expected final pipeline outputs in the host lineage DB, "
        f"model_key={model_key!r}, metrics_key={metrics_key!r}"
    )
    by_path = {str(row["path"]): str(row["command"]) for row in output_rows}
    assert by_path.get(f"s3://output-bucket/{model_key}") == "ray_task:training", output_rows
    assert by_path.get(f"s3://output-bucket/{metrics_key}") == "ray_task:evaluation", output_rows


def test_cloud_demo_compact_dag_surfaces_phase_story(
    cloud_demo_emulated_fragments: dict[str, object],
) -> None:
    project_dir = cloud_demo_emulated_fragments["project_dir"]
    assert isinstance(project_dir, Path)

    dag_payload = _dag_payload(project_dir, "--json")
    nodes = dag_payload.get("nodes", [])
    assert isinstance(nodes, list), dag_payload

    nodes_by_command = {
        str(node.get("command")): node
        for node in nodes
        if isinstance(node, dict) and str(node.get("command", "")).startswith("ray_task:")
    }
    missing_nodes = [command for command in EXPECTED_PHASE_COMMANDS if command not in nodes_by_command]
    assert not missing_nodes, (
        "Expected compact `roar dag` to show the user-facing extraction/training/evaluation phases, "
        f"missing={missing_nodes}, observed={sorted(nodes_by_command)}"
    )

    extract_step = int(nodes_by_command["ray_task:extraction"]["step_number"])
    train_step = int(nodes_by_command["ray_task:training"]["step_number"])
    evaluate_step = int(nodes_by_command["ray_task:evaluation"]["step_number"])
    assert extract_step < train_step < evaluate_step, dag_payload
    assert train_step in nodes_by_command["ray_task:evaluation"]["dependencies"], dag_payload
    assert extract_step in nodes_by_command["ray_task:training"]["dependencies"], dag_payload


def test_cloud_demo_show_resolves_phase_steps(
    cloud_demo_emulated_fragments: dict[str, object],
) -> None:
    project_dir = cloud_demo_emulated_fragments["project_dir"]
    assert isinstance(project_dir, Path)

    step_expectations = {
        2: "ray_task:extraction",
        3: "ray_task:training",
        4: "ray_task:evaluation",
    }
    for step_number, expected_command in step_expectations.items():
        result = run_roar_cli_from_host(project_dir, "show", f"@{step_number}", timeout=30)
        assert result.returncode == 0, result.stderr or result.stdout
        assert expected_command in result.stdout, result.stdout
        assert "Job not found" not in result.stdout, result.stdout
