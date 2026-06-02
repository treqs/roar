"""End-to-end fake-HF path: get -> local DB (anchor + crosswalk) -> run -> attribution.

Exercises the *real* product path (no `_form_get_composite` patch) with a fake HF
backend, covering Gap B, F1 (the crosswalk sha256 is not published), and Phase-4
register-time attribution on a real local DB.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from roar.integrations.download.hf import HFFileMeta


class _FakeHFBackend:
    """Minimal stand-in for HFDownloadBackend over an in-memory file set."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = dict(files)
        self._subpath = ""

    @property
    def commit(self) -> str:
        return "f" * 40

    @property
    def coordinates(self) -> dict[str, str]:
        return {
            "host": "hf",
            "repo_type": "datasets",
            "repo": "fake/ds",
            "ref": "main",
            "commit": self.commit,
        }

    def manifest(self) -> list[HFFileMeta]:
        out: list[HFFileMeta] = []
        for path, body in self._files.items():
            is_lfs = path.endswith(".parquet")
            out.append(
                HFFileMeta(
                    path=path,
                    size=len(body),
                    is_lfs=is_lfs,
                    sha256=hashlib.sha256(body).hexdigest() if is_lfs else None,
                    git_oid="0" * 40,
                )
            )
        return out

    def _sha256_by_path(self) -> dict[str, str]:
        return {f.path: f.sha256 for f in self.manifest() if f.sha256}

    def sha256_of(self, key: str) -> str:
        return hashlib.sha256(self._files[key]).hexdigest()

    def list_keys(self, prefix: str) -> list[str]:
        return sorted(self._files)

    def download(self, key: str, local_path: Path) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(self._files[key])


def _db_path(roar_dir: Path) -> Path:
    return next(roar_dir.rglob("roar.db"))


def test_fake_hf_get_anchor_crosswalk_then_attribution(tmp_path: Path) -> None:
    from roar.application.get.requests import GetRequest
    from roar.application.get.service import get_artifacts

    shard0 = b"PAR1-aaaa" * 200
    shard1 = b"PAR1-bbbb" * 200
    files = {
        ".gitattributes": b"* filter=lfs\n",  # boilerplate / non-LFS -> not in identity
        "shard_00000.parquet": shard0,
        "shard_00001.parquet": shard1,
    }
    backend = _FakeHFBackend(files)
    roar_dir = tmp_path / ".roar"
    req = GetRequest(
        source="hf://datasets/fake/ds/",
        destination=tmp_path / "data",
        roar_dir=roar_dir,
        cwd=tmp_path,
        repo_root=tmp_path,
    )

    with patch("roar.application.get.service.resolve_download_backend", return_value=backend):
        resp = get_artifacts(req)
    assert resp.success

    db = sqlite3.connect(_db_path(roar_dir))
    db.row_factory = sqlite3.Row

    # --- the anchor is the full dataset (2 parquet), composite-sha256 ---
    anchor = db.execute(
        "SELECT a.id, h.digest FROM artifacts a JOIN artifact_hashes h ON h.artifact_id=a.id "
        "WHERE h.algorithm='composite-sha256'"
    ).fetchone()
    assert anchor is not None
    assert (
        db.execute("SELECT component_count FROM artifacts WHERE id=?", (anchor["id"],)).fetchone()[
            0
        ]
        == 2
    )

    # --- F1: each downloaded shard publishes blake3 ONLY; sha256 lives in metadata ---
    shard_rows = db.execute(
        "SELECT a.id, a.metadata, group_concat(h.algorithm) algos "
        "FROM artifacts a JOIN artifact_hashes h ON h.artifact_id=a.id "
        "WHERE a.first_seen_path LIKE '%.parquet' GROUP BY a.id"
    ).fetchall()
    assert len(shard_rows) == 2
    for row in shard_rows:
        assert row["algos"] == "blake3", "crosswalk sha256 must not be a published hash (F1)"
        origin = json.loads(row["metadata"])["origin"]
        assert origin["algorithm"] == "sha256" and len(origin["digest"]) == 64

    # the anchor's sha256 leaves match the shards' crosswalk sha256
    expected_sha = {hashlib.sha256(shard0).hexdigest(), hashlib.sha256(shard1).hexdigest()}
    got_sha = {json.loads(r["metadata"])["origin"]["digest"] for r in shard_rows}
    assert got_sha == expected_sha
    db.close()

    # --- a run reads a shard; register-time attribution links it to the anchor ---
    from roar.application.publish.anchor_attribution import attribute_jobs_to_anchors
    from roar.db.context import create_database_context
    from roar.execution.recording import LocalJobRecorder, LocalRecordedArtifact

    shard_path = str(tmp_path / "data" / "shard_00000.parquet")
    with create_database_context(roar_dir) as ctx:
        art = ctx.artifacts.get_by_path(shard_path)
        blake3 = next(h["digest"] for h in art["hashes"] if h["algorithm"] == "blake3")
        job_id, _uid = LocalJobRecorder().record(
            ctx,
            command="python train.py",
            timestamp=time.time(),
            metadata="{}",
            job_type="run",
            execution_backend="local",
            execution_role="host",
            input_artifacts=[
                LocalRecordedArtifact(path=shard_path, hashes={"blake3": blake3}, size=len(shard0))
            ],
            output_artifacts=[],
            exit_code=0,
        )
        added = attribute_jobs_to_anchors(db_ctx=ctx, job_ids=[job_id], logger=MagicMock())
        assert added == 1
        input_ids = {i["artifact_id"] for i in ctx.jobs.get_inputs(job_id)}
        assert anchor["id"] in input_ids, "run must be attributed to the dataset anchor"
