"""
Happy-path CLI tests for label authoring semantics.
"""

import json
import sqlite3
from pathlib import Path

import pytest


def _assert_ok(result) -> str:
    assert result.returncode == 0, f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
    return result.stdout


def _artifact_label_rows(repo: Path, artifact_path: Path) -> list[tuple[int, dict[str, object]]]:
    db_path = repo / ".roar" / "roar.db"
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT l.version, l.metadata
            FROM labels l
            JOIN artifacts a ON a.id = l.artifact_id
            WHERE l.entity_type = 'artifact'
              AND a.first_seen_path = ?
            ORDER BY l.version ASC
            """,
            (str(artifact_path.resolve()),),
        ).fetchall()
    return [(int(version), json.loads(metadata)) for version, metadata in rows]


def _artifact_label_rows_by_hash(
    repo: Path, artifact_hash: str
) -> list[tuple[int, dict[str, object]]]:
    db_path = repo / ".roar" / "roar.db"
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT l.version, l.metadata
            FROM labels l
            JOIN artifact_hashes ah ON ah.artifact_id = l.artifact_id
            WHERE l.entity_type = 'artifact'
              AND ah.algorithm = 'blake3'
              AND ah.digest = ?
            ORDER BY l.version ASC
            """,
            (artifact_hash,),
        ).fetchall()
    return [(int(version), json.loads(metadata)) for version, metadata in rows]


@pytest.mark.happy_path
class TestLabelCommand:
    """CLI product-path tests for label lifecycle behavior."""

    def test_detected_dataset_composite_artifact_gets_auto_labels(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        python_exe,
    ):
        dataset_script = temp_git_repo / "emit_dataset.py"
        dataset_script.write_text(
            "\n".join(
                [
                    "from __future__ import annotations",
                    "",
                    "from pathlib import Path",
                    "import argparse",
                    "",
                    "parser = argparse.ArgumentParser()",
                    "parser.add_argument('--output-dir', required=True)",
                    "args = parser.parse_args()",
                    "",
                    "root = Path(args.output_dir)",
                    "(root / 'train').mkdir(parents=True, exist_ok=True)",
                    "(root / 'train' / 'part-00000.csv').write_text('value\\n1\\n', encoding='utf-8')",
                    "(root / 'train' / 'part-00001.csv').write_text('value\\n2\\n', encoding='utf-8')",
                ]
            ),
            encoding="utf-8",
        )
        git_commit("Add dataset emitter")

        result = roar_cli("run", python_exe, "emit_dataset.py", "--output-dir", "dataset")
        assert result.returncode == 0

        dataset_root = temp_git_repo / "dataset"

        label_show = _assert_ok(roar_cli("label", "show", "artifact", "dataset", check=False))
        assert f"dataset.id={dataset_root.resolve().as_uri()}" in label_show
        assert "dataset.modality=tabular" in label_show
        assert "dataset.type=dataset" in label_show

        show_output = _assert_ok(roar_cli("show", "dataset", check=False))
        assert "Labels:" in show_output
        assert f"dataset.id={dataset_root.resolve().as_uri()}" in show_output
        assert "dataset.modality=tabular" in show_output
        assert "dataset.type=dataset" in show_output

        rows = _artifact_label_rows(temp_git_repo, dataset_root)
        assert rows[0][1]["dataset"]["type"] == "dataset"
        assert rows[0][1]["dataset"]["id"] == dataset_root.resolve().as_uri()
        assert rows[0][1]["dataset"]["modality"] == "tabular"

    def test_artifact_label_set_patches_current_document_and_preserves_history(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        result = roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        assert result.returncode == 0
        git_commit("After preprocess")

        _assert_ok(
            roar_cli(
                "label",
                "set",
                "artifact",
                "processed.csv",
                "stage=raw",
                "owner=ml",
                check=False,
            )
        )
        _assert_ok(
            roar_cli(
                "label",
                "set",
                "artifact",
                "processed.csv",
                "stage=gold",
                check=False,
            )
        )

        label_show = _assert_ok(roar_cli("label", "show", "artifact", "processed.csv", check=False))
        assert "stage=gold" in label_show
        assert "owner=ml" in label_show

        history_output = _assert_ok(
            roar_cli("label", "history", "artifact", "processed.csv", check=False)
        )
        assert "stage=raw" in history_output
        assert "stage=gold" in history_output
        assert "owner=ml" in history_output

        show_output = _assert_ok(roar_cli("show", "processed.csv", check=False))
        assert "Labels:" in show_output
        assert "stage=gold" in show_output
        assert "owner=ml" in show_output
        assert "stage=raw" not in show_output

        rows = _artifact_label_rows(temp_git_repo, temp_git_repo / "processed.csv")
        assert rows == [
            (1, {"owner": "ml", "stage": "raw"}),
            (2, {"owner": "ml", "stage": "gold"}),
        ]

    def test_artifact_label_set_noop_does_not_create_new_version(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        result = roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        assert result.returncode == 0
        git_commit("After preprocess")

        _assert_ok(
            roar_cli(
                "label",
                "set",
                "artifact",
                "processed.csv",
                "stage=raw",
                "owner=ml",
                check=False,
            )
        )
        _assert_ok(
            roar_cli(
                "label",
                "set",
                "artifact",
                "processed.csv",
                "stage=raw",
                "owner=ml",
                check=False,
            )
        )

        rows = _artifact_label_rows(temp_git_repo, temp_git_repo / "processed.csv")
        assert rows == [(1, {"owner": "ml", "stage": "raw"})]

    def test_dag_job_and_artifact_labels_are_visible_in_show_and_dag_json(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        result = roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        assert result.returncode == 0
        git_commit("After preprocess")

        _assert_ok(
            roar_cli(
                "label",
                "set",
                "dag",
                "current",
                "experiment=ablation-7",
                "project=forecasting",
                check=False,
            )
        )
        _assert_ok(
            roar_cli(
                "label",
                "set",
                "job",
                "@1",
                "phase=preprocess",
                "tool=python",
                check=False,
            )
        )
        _assert_ok(
            roar_cli(
                "label",
                "set",
                "artifact",
                "processed.csv",
                "owner=ml",
                "stage=gold",
                check=False,
            )
        )

        session_output = _assert_ok(roar_cli("show", check=False))
        assert "Labels:" in session_output
        assert "experiment=ablation-7" in session_output
        assert "project=forecasting" in session_output

        job_output = _assert_ok(roar_cli("show", "@1", check=False))
        assert "Labels:" in job_output
        assert "phase=preprocess" in job_output
        assert "tool=python" in job_output

        artifact_output = _assert_ok(roar_cli("show", "processed.csv", check=False))
        assert "Labels:" in artifact_output
        assert "owner=ml" in artifact_output
        assert "stage=gold" in artifact_output

        dag_output = _assert_ok(roar_cli("dag", "--show-artifacts", "--json", check=False))
        dag_data = json.loads(dag_output)

        assert dag_data["labels"] == {"experiment": "ablation-7", "project": "forecasting"}

        preprocess_node = next(
            node for node in dag_data["nodes"] if "preprocess.py" in node["command"]
        )
        assert preprocess_node["labels"] == {"phase": "preprocess", "tool": "python"}

        processed_artifact = next(
            artifact for artifact in dag_data["artifacts"] if "processed.csv" in artifact["path"]
        )
        assert processed_artifact["labels"] == {"owner": "ml", "stage": "gold"}

    def test_artifact_label_cp_carries_forward_labels_to_new_artifact_version(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        result = roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        assert result.returncode == 0
        git_commit("Track processed.csv v1")

        source_hash = json.loads(_assert_ok(roar_cli("lineage", "processed.csv", check=False)))[
            "artifact"
        ]["hash"]

        _assert_ok(
            roar_cli(
                "label",
                "set",
                "artifact",
                source_hash,
                "owner=ml",
                "stage=baseline",
                check=False,
            )
        )

        (temp_git_repo / "input.csv").write_text("id,value\n1,foo\n2,bar\n3,baz\n4,qux\n")
        git_commit("Update input.csv for processed v2")

        result = roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        assert result.returncode == 0
        git_commit("Track processed.csv v2")

        destination_hash = json.loads(
            _assert_ok(roar_cli("lineage", "processed.csv", check=False))
        )["artifact"]["hash"]
        assert destination_hash != source_hash

        _assert_ok(
            roar_cli(
                "label",
                "set",
                "artifact",
                "processed.csv",
                "note=current",
                "stage=edited",
                check=False,
            )
        )
        _assert_ok(
            roar_cli(
                "label",
                "cp",
                "artifact",
                source_hash,
                "artifact",
                "processed.csv",
                check=False,
            )
        )

        label_show = _assert_ok(roar_cli("label", "show", "artifact", "processed.csv", check=False))
        assert "owner=ml" in label_show
        assert "note=current" in label_show
        assert "stage=baseline" in label_show
        assert "stage=edited" not in label_show

        show_output = _assert_ok(roar_cli("show", "processed.csv", check=False))
        assert "Labels:" in show_output
        assert "owner=ml" in show_output
        assert "note=current" in show_output
        assert "stage=baseline" in show_output

        assert _artifact_label_rows_by_hash(temp_git_repo, source_hash) == [
            (1, {"owner": "ml", "stage": "baseline"})
        ]
        assert _artifact_label_rows_by_hash(temp_git_repo, destination_hash) == [
            (1, {"note": "current", "stage": "edited"}),
            (2, {"note": "current", "owner": "ml", "stage": "baseline"}),
        ]

    def test_artifact_label_cp_noop_does_not_create_new_destination_version(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        result = roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        assert result.returncode == 0
        git_commit("Track processed.csv v1")

        source_hash = json.loads(_assert_ok(roar_cli("lineage", "processed.csv", check=False)))[
            "artifact"
        ]["hash"]
        _assert_ok(
            roar_cli(
                "label",
                "set",
                "artifact",
                source_hash,
                "owner=ml",
                "stage=baseline",
                check=False,
            )
        )

        (temp_git_repo / "input.csv").write_text("id,value\n1,foo\n2,bar\n3,baz\n4,qux\n")
        git_commit("Update input.csv for processed v2")

        result = roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        assert result.returncode == 0
        git_commit("Track processed.csv v2")

        destination_hash = json.loads(
            _assert_ok(roar_cli("lineage", "processed.csv", check=False))
        )["artifact"]["hash"]
        assert destination_hash != source_hash

        _assert_ok(
            roar_cli(
                "label",
                "set",
                "artifact",
                "processed.csv",
                "owner=ml",
                "stage=baseline",
                check=False,
            )
        )
        _assert_ok(
            roar_cli(
                "label",
                "cp",
                "artifact",
                source_hash,
                "artifact",
                "processed.csv",
                check=False,
            )
        )

        assert _artifact_label_rows_by_hash(temp_git_repo, destination_hash) == [
            (1, {"owner": "ml", "stage": "baseline"})
        ]
