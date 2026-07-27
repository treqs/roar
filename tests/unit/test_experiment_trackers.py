"""Unit tests for the trackio branch of ExperimentTrackerAnalyzer ([70]).

trackio stores one SQLite DB per project; roar records only the run identity + the
hosted HF-Space URL (never the metrics — TReqs stores no customer data).
"""

import sqlite3
from pathlib import Path

from roar.analyzers.experiment_trackers import ExperimentTrackerAnalyzer


def _make_trackio_db(dirpath: Path, project: str, space_id: str | None = None) -> str:
    """Create a minimal trackio-shaped SQLite DB, return its path."""
    db = dirpath / "huggingface" / "trackio" / f"{project}.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE metrics (id INTEGER PRIMARY KEY, run_id TEXT, timestamp TEXT, "
        "run_name TEXT, step INTEGER, metrics TEXT, log_id TEXT, space_id TEXT)"
    )
    for step in range(3):
        con.execute(
            "INSERT INTO metrics (run_id, timestamp, run_name, step, metrics, log_id, space_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                "run123",
                "2026-07-27T00:00:00Z",
                "brave-run-0",
                step,
                f'{{"loss": {round(1.0 / (step + 1), 4)}}}',
                f"log{step}",
                space_id,
            ),
        )
    con.commit()
    con.close()
    return str(db)


class TestTrackioExtractor:
    def test_url_from_env_space_id(self, tmp_path: Path):
        db = _make_trackio_db(tmp_path, "minimind-o")
        info = ExperimentTrackerAnalyzer()._extract_trackio_info(
            [db], {"ROAR_TRACKIO_SPACE_ID": "reproducible-ai/experiments"}
        )
        assert info is not None
        assert info["tracker"] == "trackio"
        assert info["project"] == "minimind-o"
        assert info["run_id"] == "run123"
        assert info["run_name"] == "brave-run-0"
        assert (
            info["url"]
            == "https://huggingface.co/spaces/reproducible-ai/experiments?project=minimind-o"
        )

    def test_space_id_from_db_column(self, tmp_path: Path):
        db = _make_trackio_db(tmp_path, "yolo", space_id="reproducible-ai/experiments")
        info = ExperimentTrackerAnalyzer()._extract_trackio_info([db], {})  # no env
        assert info is not None
        assert info["url"].endswith("/reproducible-ai/experiments?project=yolo")

    def test_no_space_id_records_identity_but_no_url(self, tmp_path: Path):
        db = _make_trackio_db(tmp_path, "cosyvoice")  # space_id None + no env
        info = ExperimentTrackerAnalyzer()._extract_trackio_info([db], {})
        assert info is not None
        assert info["project"] == "cosyvoice"
        assert "url" not in info

    def test_detection_and_analyze_end_to_end(self, tmp_path: Path):
        db = _make_trackio_db(tmp_path, "sam2", space_id="reproducible-ai/experiments")
        analyzer = ExperimentTrackerAnalyzer()
        ctx = {"tracer_data": {"written_files": [db]}, "env": {}}
        assert analyzer.relevant(ctx) is True
        res = analyzer.analyze(ctx)
        assert res is not None
        assert "trackio" in res["trackers_detected"]
        run = next(r for r in res["runs"] if r["tracker"] == "trackio")
        assert run["url"].endswith("?project=sam2")

    def test_url_only_no_metric_values_leak(self, tmp_path: Path):
        """No-data-storage invariant: metric VALUES must never reach lineage."""
        db = _make_trackio_db(tmp_path, "m", space_id="reproducible-ai/experiments")
        info = ExperimentTrackerAnalyzer()._extract_trackio_info([db], {})
        assert "loss" not in str(info)
