"""Unit tests for the trackio branch of ExperimentTrackerAnalyzer ([70]).

roar *scrapes* trackio exactly like W&B: it reads the run identity + the hosted HF
Space URL that trackio itself persisted (``project_metadata.space_id``), and never
the metrics (TReqs stores no customer data). No roar-side config, env, or flags.

The extractor returns ONE record per project DB (a run may touch several projects),
and tolerates older/imported trackio schemas that lack a ``run_id`` column or the
``project_metadata`` table.
"""

import sqlite3
from pathlib import Path

from roar.analyzers.experiment_trackers import ExperimentTrackerAnalyzer


def _make_trackio_db(
    dirpath: Path,
    project: str,
    space_id: str | None = None,
    *,
    with_project_metadata: bool = True,
    with_run_id: bool = True,
    run_id: str = "run123",
    run_name: str = "brave-run-0",
) -> str:
    """Create a minimal trackio-shaped SQLite DB, return its path.

    Mirrors the real schema: a ``metrics`` table and a ``project_metadata`` table
    into which trackio writes ``space_id`` once a run syncs to a HF Space. The
    flags let us reproduce older/imported schemas: ``with_run_id=False`` drops the
    ``run_id`` column (trackio's TensorBoard import), ``with_project_metadata=False``
    drops the whole table (a never-synced/older DB).
    """
    db = dirpath / "huggingface" / "trackio" / f"{project}.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    if with_run_id:
        con.execute(
            "CREATE TABLE metrics (id INTEGER PRIMARY KEY, run_id TEXT, timestamp TEXT, "
            "run_name TEXT, step INTEGER, metrics TEXT, log_id TEXT, space_id TEXT)"
        )
        for step in range(3):
            con.execute(
                "INSERT INTO metrics (run_id, run_name, step, metrics) VALUES (?,?,?,?)",
                (run_id, run_name, step, f'{{"loss": {round(1.0 / (step + 1), 4)}}}'),
            )
    else:
        con.execute(
            "CREATE TABLE metrics (id INTEGER PRIMARY KEY, timestamp TEXT, "
            "run_name TEXT, step INTEGER, metrics TEXT)"
        )
        for step in range(3):
            con.execute(
                "INSERT INTO metrics (run_name, step, metrics) VALUES (?,?,?)",
                (run_name, step, f'{{"loss": {round(1.0 / (step + 1), 4)}}}'),
            )
    if with_project_metadata:
        con.execute("CREATE TABLE project_metadata (key TEXT, value TEXT)")
        if space_id:
            con.execute(
                "INSERT INTO project_metadata (key, value) VALUES ('space_id', ?)", (space_id,)
            )
    con.commit()
    con.close()
    return str(db)


class TestTrackioExtractor:
    def test_url_from_persisted_space_metadata(self, tmp_path: Path):
        db = _make_trackio_db(tmp_path, "minimind-o", space_id="reproducible-ai/experiments")
        infos = ExperimentTrackerAnalyzer()._extract_trackio_info([db], {})
        assert infos is not None and len(infos) == 1
        info = infos[0]
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
        infos = ExperimentTrackerAnalyzer()._extract_trackio_info([db], {})
        assert infos is not None and len(infos) == 1
        assert infos[0]["project"] == "cosyvoice"
        assert infos[0]["run_id"] == "run123"
        assert "url" not in infos[0]

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
        infos = ExperimentTrackerAnalyzer()._extract_trackio_info([db], {})
        assert "loss" not in str(infos)

    def test_multi_project_no_cross_contamination(self, tmp_path: Path):
        """F1: a run touching >1 project must never splice one project's Space
        onto another project's name."""
        alpha = _make_trackio_db(tmp_path, "alpha", space_id="team/alpha-space")
        zebra = _make_trackio_db(tmp_path, "zebra")  # local-only, no Space
        infos = ExperimentTrackerAnalyzer()._extract_trackio_info([alpha, zebra], {})
        assert infos is not None and len(infos) == 2
        by_project = {i["project"]: i for i in infos}
        assert (
            by_project["alpha"]["url"]
            == "https://huggingface.co/spaces/team/alpha-space?project=alpha"
        )
        # zebra was never synced → no URL, and alpha's Space never leaks onto it.
        assert "url" not in by_project["zebra"]
        assert not any("project=zebra" in i.get("url", "") for i in infos)

    def test_missing_project_metadata_keeps_run_identity(self, tmp_path: Path):
        """F2: a DB without the project_metadata table must still record the run
        identity (the two lookups are independent)."""
        db = _make_trackio_db(tmp_path, "beta", with_project_metadata=False)
        infos = ExperimentTrackerAnalyzer()._extract_trackio_info([db], {})
        assert infos is not None and len(infos) == 1
        assert infos[0]["run_name"] == "brave-run-0"
        assert "url" not in infos[0]

    def test_runid_less_schema_still_records_url(self, tmp_path: Path):
        """F2: an older/imported schema without a run_id column must still yield
        the hosted-Space URL from project_metadata."""
        db = _make_trackio_db(tmp_path, "omega", space_id="team/omega-space", with_run_id=False)
        infos = ExperimentTrackerAnalyzer()._extract_trackio_info([db], {})
        assert infos is not None and len(infos) == 1
        assert infos[0]["url"] == "https://huggingface.co/spaces/team/omega-space?project=omega"
        assert infos[0]["run_name"] == "brave-run-0"
        assert infos[0].get("run_id") is None
