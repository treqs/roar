"""Unit tests for the trackio branch of ExperimentTrackerAnalyzer ([70]).

roar *scrapes* trackio exactly like W&B: it reads the run identity + the hosted HF
Space URL that trackio itself persisted (``project_metadata.space_id``), and never
the metrics (TReqs stores no customer data). No roar-side config, env, or flags.
"""

import sqlite3
from pathlib import Path

from roar.analyzers.experiment_trackers import ExperimentTrackerAnalyzer


def _make_trackio_db(dirpath: Path, project: str, space_id: str | None = None) -> str:
    """Create a minimal trackio-shaped SQLite DB, return its path.

    Mirrors the real schema: a ``metrics`` table and a ``project_metadata`` table
    into which trackio writes ``space_id`` once a run syncs to a HF Space.
    """
    db = dirpath / "huggingface" / "trackio" / f"{project}.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE metrics (id INTEGER PRIMARY KEY, run_id TEXT, timestamp TEXT, "
        "run_name TEXT, step INTEGER, metrics TEXT, log_id TEXT, space_id TEXT)"
    )
    con.execute("CREATE TABLE project_metadata (key TEXT, value TEXT)")
    for step in range(3):
        con.execute(
            "INSERT INTO metrics (run_id, run_name, step, metrics) VALUES (?,?,?,?)",
            ("run123", "brave-run-0", step, f'{{"loss": {round(1.0 / (step + 1), 4)}}}'),
        )
    if space_id:
        con.execute("INSERT INTO project_metadata (key, value) VALUES ('space_id', ?)", (space_id,))
    con.commit()
    con.close()
    return str(db)


class TestTrackioExtractor:
    def test_url_from_persisted_space_metadata(self, tmp_path: Path):
        db = _make_trackio_db(tmp_path, "minimind-o", space_id="reproducible-ai/experiments")
        info = ExperimentTrackerAnalyzer()._extract_trackio_info([db], {})
        assert info is not None
        assert info["tracker"] == "trackio"
        assert info["project"] == "minimind-o"
        assert info["run_id"] == "run123"
        assert info["run_name"] == "brave-run-0"
        assert (
            info["url"]
            == "https://huggingface.co/spaces/reproducible-ai/experiments?project=minimind-o"
        )

    def test_no_space_metadata_records_identity_but_no_url(self, tmp_path: Path):
        db = _make_trackio_db(tmp_path, "cosyvoice")  # local-only run, never synced
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
