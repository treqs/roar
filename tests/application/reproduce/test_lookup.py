from pathlib import Path
from unittest.mock import patch

from roar.application.reproduce.lookup import (
    _restrict_steps_to_artifact_subdag,
    lookup_pipeline_result,
)


def _step(sid: int, inputs: list[int], outputs: list[int], cmd: str = "") -> dict:
    return {
        "id": sid,
        "command": cmd,
        "_inputs": [{"artifact_id": a} for a in inputs],
        "_outputs": [{"artifact_id": a} for a in outputs],
    }


def test_subdag_drops_redundant_reruns_of_same_artifact() -> None:
    # train.py run twice, both producing the same model artifact (id 20) from
    # the same data (id 10). Reproducing model id 20 should keep ONE run.
    steps = [
        _step(1, inputs=[10], outputs=[20], cmd="python3 train.py"),
        _step(2, inputs=[10], outputs=[20], cmd="python3 train.py"),
    ]
    kept = _restrict_steps_to_artifact_subdag(steps, target_artifact_id=20)
    assert [s["id"] for s in kept] == [1]


def test_subdag_keeps_transitive_input_producers_and_drops_unrelated() -> None:
    # 10 --(job1)--> 20 --(job2)--> 30 ; job3 is unrelated (produces 99).
    steps = [
        _step(1, inputs=[10], outputs=[20]),
        _step(2, inputs=[20], outputs=[30]),
        _step(3, inputs=[10], outputs=[99]),
    ]
    kept = _restrict_steps_to_artifact_subdag(steps, target_artifact_id=30)
    assert [s["id"] for s in kept] == [1, 2]


def test_subdag_falls_back_to_all_steps_when_target_not_produced() -> None:
    steps = [_step(1, inputs=[10], outputs=[20])]
    # target 77 isn't produced by any step -> keep everything (no empty plan)
    assert _restrict_steps_to_artifact_subdag(steps, target_artifact_id=77) == steps


def test_lookup_pipeline_result_prefers_local_and_skips_remote(tmp_path: Path) -> None:
    with (
        patch(
            "roar.application.reproduce.lookup._lookup_local", return_value={"pipeline": "local"}
        ),
        patch("roar.application.reproduce.lookup._lookup_remote") as lookup_remote,
    ):
        result = lookup_pipeline_result(
            hash_prefix="a" * 64,
            roar_dir=tmp_path / ".roar",
            server_url="http://example.test",
            glaas_client=None,
        )

    assert result.pipeline == {"pipeline": "local"}
    assert result.error is None
    assert result.source == "local"
    lookup_remote.assert_not_called()


def test_lookup_pipeline_result_returns_remote_result_on_local_miss(tmp_path: Path) -> None:
    with (
        patch("roar.application.reproduce.lookup._lookup_local", return_value=None),
        patch(
            "roar.application.reproduce.lookup._lookup_remote",
            return_value=({"pipeline": "remote"}, None),
        ),
    ):
        result = lookup_pipeline_result(
            hash_prefix="a" * 64,
            roar_dir=tmp_path / ".roar",
            server_url="http://example.test",
            glaas_client=None,
        )

    assert result.pipeline == {"pipeline": "remote"}
    assert result.error is None
    assert result.source == "remote"


def test_lookup_pipeline_result_returns_remote_error_on_local_miss(tmp_path: Path) -> None:
    with (
        patch("roar.application.reproduce.lookup._lookup_local", return_value=None),
        patch(
            "roar.application.reproduce.lookup._lookup_remote",
            return_value=(None, "permission denied"),
        ),
    ):
        result = lookup_pipeline_result(
            hash_prefix="a" * 64,
            roar_dir=tmp_path / ".roar",
            server_url="http://example.test",
            glaas_client=None,
        )

    assert result.pipeline is None
    assert result.error == "permission denied"
    assert result.source == "remote"


def test_lookup_pipeline_result_returns_not_found_when_remote_lookup_disabled(
    tmp_path: Path,
) -> None:
    with patch("roar.application.reproduce.lookup._lookup_local", return_value=None):
        result = lookup_pipeline_result(
            hash_prefix="a" * 64,
            roar_dir=tmp_path / ".roar",
            server_url=None,
            glaas_client=None,
        )

    assert result.pipeline is None
    assert "Artifact not found" in (result.error or "")
    assert result.source == "none"
