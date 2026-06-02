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


class _LeRobotBackend:
    """LeRobot-shaped repo: a large non-LFS meta file + two LFS data shards."""

    _subpath = ""

    def __init__(self, meta_size: int) -> None:
        self._meta_size = meta_size
        self.sha256_of_calls: list[str] = []

    @property
    def coordinates(self) -> dict[str, str]:
        return {
            "host": "hf",
            "repo_type": "datasets",
            "repo": "x/lerobot",
            "ref": "main",
            "commit": "f" * 40,
        }

    def manifest(self) -> list[HFFileMeta]:
        return [
            HFFileMeta(
                path="meta/info.json",
                size=self._meta_size,
                is_lfs=False,
                sha256=None,
                git_oid="0" * 40,
            ),
            HFFileMeta(
                path="data/chunk-000/file-0.parquet",
                size=10,
                is_lfs=True,
                sha256="a" * 64,
                git_oid="0" * 40,
            ),
            HFFileMeta(
                path="data/chunk-000/file-1.parquet",
                size=10,
                is_lfs=True,
                sha256="b" * 64,
                git_oid="0" * 40,
            ),
        ]

    def sha256_of(self, key: str) -> str:
        self.sha256_of_calls.append(key)
        return "c" * 64


def test_anchor_budget_excludes_nonlfs_over_budget_and_signals(tmp_path: Path) -> None:
    from roar.application.get.service import _form_get_composite

    db = MagicMock()
    db.artifacts.register.return_value = ("anchor-id", True)

    over = 70 * 1024 * 1024  # non-LFS meta past the 64 MB budget
    backend = _LeRobotBackend(meta_size=over)
    notices: dict = {}
    result = _form_get_composite(db_ctx=db, backend=backend, full_anchor=False, notices=notices)

    assert result is not None  # anchor still forms (LFS-only over the 2 parquet)
    assert backend.sha256_of_calls == []  # the over-budget non-LFS file is NOT downloaded
    assert round(notices["lfs_only_mb"]) == round(over / 1e6)


def test_full_anchor_includes_nonlfs_over_budget(tmp_path: Path) -> None:
    from roar.application.get.service import _form_get_composite

    db = MagicMock()
    db.artifacts.register.return_value = ("anchor-id", True)

    backend = _LeRobotBackend(meta_size=70 * 1024 * 1024)
    notices: dict = {}
    result = _form_get_composite(db_ctx=db, backend=backend, full_anchor=True, notices=notices)

    assert result is not None
    assert backend.sha256_of_calls == ["meta/info.json"]  # downloaded + folded in
    assert "lfs_only_mb" not in notices  # no exclusion -> no warning


def test_view_edge_resolution_from_real_get_db(tmp_path: Path) -> None:
    """get -> DB (anchor + crosswalk); resolving a run's shard input yields a consumes
    view edge over the anchor (bloom contains the shard) and prunes the shard input."""
    import base64

    from roar.application.get.requests import GetRequest
    from roar.application.get.service import get_artifacts
    from roar.application.publish.composite_builder import CompositeArtifactBuilder
    from roar.application.publish.view_edges import resolve_view_edges_for_job
    from roar.db.context import create_database_context

    shard0 = b"PAR1-aaaa" * 200
    shard1 = b"PAR1-bbbb" * 200
    backend = _FakeHFBackend({"shard_00000.parquet": shard0, "shard_00001.parquet": shard1})
    roar_dir = tmp_path / ".roar"
    req = GetRequest(
        source="hf://datasets/fake/ds/",
        destination=tmp_path / "data",
        roar_dir=roar_dir,
        cwd=tmp_path,
        repo_root=tmp_path,
    )
    with patch("roar.application.get.service.resolve_download_backend", return_value=backend):
        assert get_artifacts(req).success

    shard0_sha = hashlib.sha256(shard0).hexdigest()
    with create_database_context(roar_dir) as ctx:
        art = ctx.artifacts.get_by_path(str(tmp_path / "data" / "shard_00000.parquet"))
        shard0_blake3 = next(h["digest"] for h in art["hashes"] if h["algorithm"] == "blake3")
        edges, prune = resolve_view_edges_for_job(db_ctx=ctx, input_hashes=[shard0_blake3])

    assert len(edges) == 1
    edge = edges[0]
    assert edge["relation"] == "consumes"
    assert edge["parent_total"] == 2  # the full dataset
    assert edge["selected_count"] == 1
    assert prune == {shard0_blake3}  # the shard collapses into the view edge

    # The view bloom contains the consumed shard's origin sha256 (keyed for glaas-api).
    builder = CompositeArtifactBuilder()
    raw = base64.b64decode(edge["bloom"]["bloom_filter_base64"])
    bits, nhash = edge["bloom"]["bloom_bits"], edge["bloom"]["bloom_hashes"]
    h1, h2 = builder._bloom_hash_pair(f"sha256:{shard0_sha}".encode())
    if h2 == 0:
        h2 = 1
    assert all(
        (raw[((h1 + i * h2) % bits) // 8] >> (((h1 + i * h2) % bits) % 8)) & 1 for i in range(nhash)
    )


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
