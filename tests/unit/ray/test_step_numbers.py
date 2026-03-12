from __future__ import annotations

from roar.backends.ray.collector import _assign_step_numbers
from roar.backends.ray.fragment import ArtifactRef, TaskFragment


def _artifact(hash_value: str | None) -> ArtifactRef:
    return ArtifactRef(
        path="",
        hash=hash_value,
        hash_algorithm="blake3",
        size=0,
        capture_method="python",
    )


def _artifact_with_path(path: str, hash_value: str | None = None) -> ArtifactRef:
    return ArtifactRef(
        path=path,
        hash=hash_value,
        hash_algorithm="blake3" if hash_value else "",
        size=0,
        capture_method="python",
    )


def _frag(
    uid: str,
    reads: tuple[str, ...] = (),
    writes: tuple[str, ...] = (),
    t: float = 0.0,
    function_name: str | None = None,
) -> TaskFragment:
    return TaskFragment(
        job_uid=uid,
        parent_job_uid="driver",
        ray_task_id=uid,
        ray_worker_id="w",
        ray_node_id="n",
        ray_actor_id=None,
        function_name=function_name or uid,
        started_at=t,
        ended_at=t + 0.1,
        exit_code=0,
        reads=[_artifact(hash_value) for hash_value in reads],
        writes=[_artifact(hash_value) for hash_value in writes],
    )


def test_assign_step_numbers_linear_chain() -> None:
    fragments = [
        _frag("a", writes=("h1",)),
        _frag("b", reads=("h1",), writes=("h2",)),
        _frag("c", reads=("h2",)),
    ]

    assert _assign_step_numbers(fragments) == {"a": 2, "b": 3, "c": 4}


def test_assign_step_numbers_parallel_same_stage() -> None:
    fragments = [
        _frag("a", writes=("h1",)),
        _frag("b", writes=("h2",)),
        _frag("c", reads=("h1",)),
        _frag("d", reads=("h2",)),
    ]

    assert _assign_step_numbers(fragments) == {"a": 2, "b": 2, "c": 3, "d": 3}


def test_assign_step_numbers_three_stage_pipeline_with_equal_timestamps() -> None:
    fragments = [
        _frag("ingest_0", writes=("proc_0",), t=1.0),
        _frag("ingest_1", writes=("proc_1",), t=1.0),
        _frag("ingest_2", writes=("proc_2",), t=1.0),
        _frag("train_0", reads=("proc_0",), writes=("model_0",), t=1.0),
        _frag("train_1", reads=("proc_1",), writes=("model_1",), t=1.0),
        _frag("eval_0", reads=("model_0",), t=1.0),
        _frag("eval_1", reads=("model_1",), t=1.0),
    ]

    step_map = _assign_step_numbers(fragments)
    assert step_map["ingest_0"] == 2
    assert step_map["ingest_1"] == 2
    assert step_map["ingest_2"] == 2
    assert step_map["train_0"] == 3
    assert step_map["train_1"] == 3
    assert step_map["eval_0"] == 4
    assert step_map["eval_1"] == 4


def test_assign_step_numbers_no_dependencies() -> None:
    fragments = [
        _frag("a", t=1.0),
        _frag("b", t=1.0),
        _frag("c", t=1.0),
    ]

    assert _assign_step_numbers(fragments) == {"a": 2, "b": 2, "c": 2}


def test_assign_step_numbers_single_fragment() -> None:
    assert _assign_step_numbers([_frag("single", t=7.0)]) == {"single": 2}


def test_assign_step_numbers_diamond_dag() -> None:
    fragments = [
        _frag("a", writes=("h1",)),
        _frag("b", reads=("h1",), writes=("h2",)),
        _frag("c", reads=("h1",), writes=("h3",)),
        _frag("d", reads=("h2", "h3")),
    ]

    assert _assign_step_numbers(fragments) == {"a": 2, "b": 3, "c": 3, "d": 4}


def test_assign_step_numbers_etag_hashes() -> None:
    etag_a = "11111111111111111111111111111111"
    etag_b = "22222222222222222222222222222222"
    fragments = [
        _frag("a", writes=(etag_a,)),
        _frag("b", reads=(etag_a,), writes=(etag_b,)),
        _frag("c", reads=(etag_b,)),
    ]

    assert _assign_step_numbers(fragments) == {"a": 2, "b": 3, "c": 4}


def test_assign_step_numbers_ignores_incremental_snapshots_of_same_job() -> None:
    # Worker fragments are emitted incrementally during execution; repeated
    # snapshots for the same job_uid must not create synthetic self-dependencies.
    fragments = [
        _frag("ingest", writes=("proc",)),
        _frag("train", reads=("proc",)),
        _frag("train", reads=("proc",), writes=("model",)),
        _frag("train", reads=("proc", "model"), writes=("model",)),
        _frag("eval", reads=("model",)),
    ]

    assert _assign_step_numbers(fragments) == {"ingest": 2, "train": 3, "eval": 4}


def test_same_function_cross_job_edge_preserved() -> None:
    """
    eval_0 depends on eval_1 and eval_2 (same function, different jobs).
    eval_1 and eval_2 depend on train tasks (cross-function).
    eval_0 must be one step higher than eval_1 and eval_2.
    """
    train_0 = _frag("train_0", writes=("model_0",), function_name="s3_pipeline.train_shard")
    train_1 = _frag("train_1", writes=("model_1",), function_name="s3_pipeline.train_shard")
    eval_1 = _frag(
        "eval_1",
        reads=("model_1",),
        writes=("metrics_1",),
        function_name="s3_pipeline.eval_model",
    )
    eval_2 = _frag(
        "eval_2",
        reads=("model_0",),
        writes=("metrics_2",),
        function_name="s3_pipeline.eval_model",
    )
    eval_0 = _frag(
        "eval_0",
        reads=("model_0", "metrics_1", "metrics_2"),
        writes=("report",),
        function_name="s3_pipeline.eval_model",
    )

    steps = _assign_step_numbers([train_0, train_1, eval_1, eval_2, eval_0])
    assert steps["train_0"] == steps["train_1"]
    assert steps["eval_1"] == steps["eval_2"]
    assert steps["eval_1"] > steps["train_0"]
    assert steps["eval_0"] > steps["eval_1"]
    assert steps["eval_0"] > steps["eval_2"]


def test_no_regression_independent_same_function_tasks() -> None:
    """
    Three ingest tasks with no shared artifacts must all get the same step.
    """
    ingest_0 = _frag(
        "ingest_0",
        reads=("raw_0",),
        writes=("proc_0",),
        function_name="s3_pipeline.ingest_shard",
    )
    ingest_1 = _frag(
        "ingest_1",
        reads=("raw_1",),
        writes=("proc_1",),
        function_name="s3_pipeline.ingest_shard",
    )
    ingest_2 = _frag(
        "ingest_2",
        reads=("raw_2",),
        writes=("proc_2",),
        function_name="s3_pipeline.ingest_shard",
    )

    steps = _assign_step_numbers([ingest_0, ingest_1, ingest_2])
    assert steps["ingest_0"] == steps["ingest_1"] == steps["ingest_2"]


def test_assign_step_numbers_matches_on_path_when_only_producer_has_hash() -> None:
    producer = TaskFragment(
        job_uid="train",
        parent_job_uid="driver",
        ray_task_id="train",
        ray_worker_id="w",
        ray_node_id="n",
        ray_actor_id=None,
        function_name="training",
        started_at=1.0,
        ended_at=1.1,
        exit_code=0,
        writes=[_artifact_with_path("s3://bucket/model.json", "etag-model")],
    )
    consumer = TaskFragment(
        job_uid="eval",
        parent_job_uid="driver",
        ray_task_id="eval",
        ray_worker_id="w",
        ray_node_id="n",
        ray_actor_id=None,
        function_name="evaluation",
        started_at=2.0,
        ended_at=2.1,
        exit_code=0,
        reads=[_artifact_with_path("s3://bucket/model.json")],
    )

    assert _assign_step_numbers([producer, consumer]) == {"train": 2, "eval": 3}
