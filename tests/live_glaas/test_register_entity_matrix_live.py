"""End-to-end register coverage for the publish entity matrix."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from roar.db.context import create_database_context
from tests.live_glaas import test_composite_live as composite_live

managed_glaas_url = composite_live.managed_glaas_url
glaas_url = composite_live.glaas_url
temp_git_repo = composite_live.temp_git_repo
glaas_configured = composite_live.glaas_configured
glaas_db_queryable = composite_live.glaas_db_queryable
_api_get = composite_live._api_get
_db_query_rows = composite_live._db_query_rows

pytestmark = pytest.mark.live_glaas


def _git_info(repo: Path) -> dict[str, str]:
    return {
        "repo": subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "commit": subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "branch": subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
    }


def _active_session_id(repo: Path) -> int:
    with sqlite3.connect(repo / ".roar" / "roar.db") as conn:
        row = conn.execute(
            "SELECT id FROM sessions WHERE is_active = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None, "No active local session found"
    return int(row[0])


def _current_status_session_hash(repo: Path, roar_cli) -> str:
    status_result = roar_cli("status")
    assert status_result.returncode == 0, status_result.stderr or status_result.stdout
    match = re.search(r"Session:\s+([a-f0-9]{64})", status_result.stdout)
    assert match is not None, status_result.stdout
    return match.group(1)


def _parse_session_hash(output: str) -> str:
    match = re.search(r"/dag/([a-f0-9]{64})", output)
    if not match:
        raise AssertionError(f"Unable to parse session hash from output:\n{output}")
    return match.group(1)


def _job_row_for_script(repo: Path, script_name: str) -> dict[str, Any]:
    with sqlite3.connect(repo / ".roar" / "roar.db") as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT job_uid, step_number, command
            FROM jobs
            WHERE script = ?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (script_name,),
        ).fetchone()
    if row is None:
        raise AssertionError(f"Expected a tracked job for script {script_name}")
    return dict(row)


def _artifact_hash_for_output(repo: Path, path: str, algorithm: str) -> str:
    with sqlite3.connect(repo / ".roar" / "roar.db") as conn:
        row = conn.execute(
            """
            SELECT ah.digest
            FROM job_outputs jo
            JOIN artifacts a ON a.id = jo.artifact_id
            JOIN artifact_hashes ah ON ah.artifact_id = a.id
            WHERE jo.path = ?
              AND ah.algorithm = ?
            ORDER BY a.first_seen_at DESC
            LIMIT 1
            """,
            (path, algorithm),
        ).fetchone()
    if row is None or not isinstance(row[0], str) or not row[0]:
        raise AssertionError(f"Missing {algorithm} hash for output path {path}")
    return row[0]


def _seed_remote_output(
    repo: Path,
    *,
    command: str,
    path: str,
    digest: str,
    source_type: str,
    source_url: str,
) -> str:
    git = _git_info(repo)
    with create_database_context(repo / ".roar") as db:
        session = db.sessions.get_active()
        if not session:
            raise AssertionError("Expected an active session before seeding remote outputs")
        session_id = int(session["id"])
        step_number = db.sessions.get_next_step_number(session_id)
        artifact_id, _created = db.artifacts.register(
            hashes={"blake3": digest},
            size=128,
            path=path,
            source_type=source_type,
            source_url=source_url,
        )
        job_id, _job_uid = db.jobs.create(
            command=command,
            timestamp=time.time(),
            session_id=session_id,
            step_number=step_number,
            git_repo=git["repo"],
            git_commit=git["commit"],
            git_branch=git["branch"],
            duration_seconds=0.25,
            exit_code=0,
        )
        db.jobs.add_output(job_id, artifact_id, path)
    return digest


def _seed_composite_output(repo: Path, *, root_path: str) -> str:
    git = _git_info(repo)
    composite_hash = hashlib.blake2b(root_path.encode("utf-8"), digest_size=32).hexdigest()
    leaf_rows: list[dict[str, Any]] = []
    with create_database_context(repo / ".roar") as db:
        session = db.sessions.get_active()
        if not session:
            raise AssertionError("Expected an active session before seeding composite outputs")
        session_id = int(session["id"])
        step_number = db.sessions.get_next_step_number(session_id)

        for relative_path, payload in (
            ("part-000.json", b'{"id":0}\n'),
            ("part-001.json", b'{"id":1}\n'),
        ):
            leaf_digest = hashlib.blake2b(payload, digest_size=32).hexdigest()
            leaf_artifact_id, _created = db.artifacts.register(
                hashes={"blake3": leaf_digest},
                size=len(payload),
                path=f"{root_path}/{relative_path}",
            )
            leaf_rows.append(
                {
                    "relative_path": relative_path,
                    "leaf_kind": "file",
                    "component_algorithm": "blake3",
                    "component_digest": leaf_digest,
                    "component_size": len(payload),
                    "artifact_id": leaf_artifact_id,
                }
            )

        artifact_id, _created = db.artifacts.register(
            hashes={"composite-blake3": composite_hash},
            size=sum(int(row["component_size"]) for row in leaf_rows),
            path=root_path,
        )
        db.composites.upsert_details(
            artifact_id=artifact_id,
            components=leaf_rows,
            component_count_total=len(leaf_rows),
            membership_index={
                "total_components": len(leaf_rows),
                "stored_components": len(leaf_rows),
                "bloom_filter_base64": "AQIDBA==",
                "bloom_bits": 2048,
                "bloom_hashes": 12,
                "bloom_version": 1,
            },
        )
        job_id, _job_uid = db.jobs.create(
            command="python build_composite.py",
            timestamp=time.time(),
            session_id=session_id,
            step_number=step_number,
            git_repo=git["repo"],
            git_commit=git["commit"],
            git_branch=git["branch"],
            duration_seconds=0.5,
            exit_code=0,
        )
        db.jobs.add_output(job_id, artifact_id, root_path)
    return composite_hash


def _setup_local_target_matrix(
    repo: Path,
    *,
    roar_cli,
    git_commit,
    script_name: str,
    output_name: str,
    output_payload: str,
) -> dict[str, Any]:
    script_path = repo / script_name
    script_path.write_text(
        f"""
from pathlib import Path

Path("artifacts").mkdir(exist_ok=True)
Path("artifacts/{output_name}").write_text({output_payload!r} + "\\n", encoding="utf-8")
print("built {output_name}")
""".strip()
        + "\n",
        encoding="utf-8",
    )
    git_commit(f"Add {script_name}")

    run_result = roar_cli("run", sys.executable, script_name)
    assert run_result.returncode == 0, run_result.stderr
    git_commit(f"After {script_name}")

    local_output_path = str((repo / "artifacts" / output_name).resolve())
    local_hash = _artifact_hash_for_output(repo, local_output_path, "blake3")
    job_row = _job_row_for_script(repo, script_name)
    composite_root = str((repo / "exports" / "bundle").resolve())
    composite_hash = _seed_composite_output(repo, root_path=composite_root)
    session_hash = _current_status_session_hash(repo, roar_cli)

    return {
        "local_output_path": local_output_path,
        "local_hash": local_hash,
        "job_uid": str(job_row["job_uid"]),
        "step_number": int(job_row["step_number"]),
        "command": str(job_row["command"]),
        "composite_root": composite_root,
        "composite_hash": composite_hash,
        "session_hash": session_hash,
    }


def test_register_session_hash_publishes_local_remote_and_composite_entities(
    glaas_configured: Path,
    glaas_db_queryable,
    glaas_url: str,
    roar_cli,
    git_commit,
) -> None:
    del glaas_db_queryable
    repo = glaas_configured

    script_path = repo / "build_local.py"
    script_path.write_text(
        """
from pathlib import Path

Path("artifacts").mkdir(exist_ok=True)
Path("artifacts/local_model.json").write_text('{"status":"ok"}\\n', encoding="utf-8")
print("built local_model.json")
""".strip()
        + "\n",
        encoding="utf-8",
    )
    git_commit("Add local build script")

    run_result = roar_cli("run", sys.executable, "build_local.py")
    assert run_result.returncode == 0, run_result.stderr
    git_commit("After local build")

    local_output_path = str((repo / "artifacts" / "local_model.json").resolve())
    local_hash = _artifact_hash_for_output(repo, local_output_path, "blake3")
    s3_path = "s3://demo-bucket/exports/final_metrics.json"
    s3_hash = _seed_remote_output(
        repo,
        command="python publish_s3.py",
        path=s3_path,
        digest=hashlib.blake2b(b"s3-final-metrics", digest_size=32).hexdigest(),
        source_type="s3",
        source_url=s3_path,
    )
    gs_path = "gs://demo-bucket/checkpoints/final_model.bin"
    gs_hash = _seed_remote_output(
        repo,
        command="python publish_gs.py",
        path=gs_path,
        digest=hashlib.blake2b(b"gs-final-model", digest_size=32).hexdigest(),
        source_type="gs",
        source_url=gs_path,
    )
    composite_root = str((repo / "exports" / "bundle").resolve())
    composite_hash = _seed_composite_output(repo, root_path=composite_root)

    session_hash = _current_status_session_hash(repo, roar_cli)

    register_result = roar_cli("register", session_hash, "--yes")
    assert register_result.returncode == 0, register_result.stderr or register_result.stdout

    session_rows = _db_query_rows("SELECT hash FROM sessions WHERE hash = $1", [session_hash])
    assert len(session_rows) == 1, session_rows
    job_rows = _db_query_rows(
        """
        SELECT command
        FROM jobs
        WHERE session_hash = $1
        ORDER BY command ASC
        """,
        [session_hash],
    )
    assert len(job_rows) >= 4, job_rows

    published_paths = _db_query_rows(
        """
        SELECT jo.path, j.command, jo.artifact_hash
        FROM job_outputs jo
        JOIN jobs j ON j.id = jo.job_id
        WHERE j.session_hash = $1
        ORDER BY j.command ASC, jo.path ASC
        """,
        [session_hash],
    )
    path_set = {str(row["path"]) for row in published_paths}
    assert local_output_path in path_set, published_paths
    assert s3_path in path_set, published_paths
    assert gs_path in path_set, published_paths
    assert composite_root in path_set, published_paths

    artifact_rows = _db_query_rows(
        """
        SELECT hash, source_type, original_session_hash
        FROM artifacts
        WHERE hash = ANY($1::text[])
        ORDER BY hash ASC
        """,
        [[local_hash, s3_hash, gs_hash, composite_hash]],
    )
    artifact_by_hash = {str(row["hash"]): row for row in artifact_rows}
    assert artifact_by_hash[local_hash]["source_type"] is None, artifact_by_hash
    assert artifact_by_hash[s3_hash]["source_type"] == "s3", artifact_by_hash
    assert artifact_by_hash[gs_hash]["source_type"] == "gs", artifact_by_hash
    assert artifact_by_hash[composite_hash]["original_session_hash"] == session_hash, (
        artifact_by_hash
    )

    local_public = _api_get(glaas_url, f"/api/v1/public/artifacts/{local_hash}")
    assert local_public.get("success") is True, local_public
    assert local_public["data"]["hash"] == local_hash, local_public

    composite_public = _api_get(glaas_url, f"/api/v1/public/artifacts/{composite_hash}")
    assert composite_public.get("success") is True, composite_public
    assert composite_public["data"]["isComposite"] is True, composite_public

    composite_metadata_rows = _db_query_rows(
        """
        SELECT component_count_total, component_count_stored
        FROM composite_metadata
        WHERE artifact_hash = $1
        """,
        [composite_hash],
    )
    assert len(composite_metadata_rows) == 1, composite_metadata_rows
    assert int(composite_metadata_rows[0]["component_count_total"]) == 2, composite_metadata_rows
    assert int(composite_metadata_rows[0]["component_count_stored"]) == 2, composite_metadata_rows

    component_rows = _db_query_rows(
        """
        SELECT relative_path
        FROM composite_components
        WHERE composite_hash = $1
        ORDER BY relative_path ASC
        """,
        [composite_hash],
    )
    assert [str(row["relative_path"]) for row in component_rows] == [
        "part-000.json",
        "part-001.json",
    ]


def test_register_artifact_hash_targets_publish_primitive_and_composite_artifacts(
    glaas_configured: Path,
    glaas_db_queryable,
    glaas_url: str,
    roar_cli,
    git_commit,
) -> None:
    del glaas_db_queryable
    repo = glaas_configured
    targets = _setup_local_target_matrix(
        repo,
        roar_cli=roar_cli,
        git_commit=git_commit,
        script_name="build_hash_targets.py",
        output_name="hash_target_model.json",
        output_payload='{"status":"hash"}',
    )

    primitive_result = roar_cli("register", targets["local_hash"], "--yes")
    assert primitive_result.returncode == 0, primitive_result.stderr or primitive_result.stdout
    assert _parse_session_hash(primitive_result.stdout) == targets["session_hash"], (
        primitive_result.stdout
    )

    primitive_public = _api_get(glaas_url, f"/api/v1/public/artifacts/{targets['local_hash']}")
    assert primitive_public.get("success") is True, primitive_public
    assert primitive_public["data"]["hash"] == targets["local_hash"], primitive_public

    composite_result = roar_cli("register", targets["composite_hash"], "--yes")
    assert composite_result.returncode == 0, composite_result.stderr or composite_result.stdout
    assert _parse_session_hash(composite_result.stdout) == targets["session_hash"], (
        composite_result.stdout
    )

    composite_public = _api_get(glaas_url, f"/api/v1/public/artifacts/{targets['composite_hash']}")
    assert composite_public.get("success") is True, composite_public
    assert composite_public["data"]["isComposite"] is True, composite_public

    composite_metadata_rows = _db_query_rows(
        """
        SELECT component_count_total, component_count_stored
        FROM composite_metadata
        WHERE artifact_hash = $1
        """,
        [targets["composite_hash"]],
    )
    assert len(composite_metadata_rows) == 1, composite_metadata_rows
    assert int(composite_metadata_rows[0]["component_count_total"]) == 2, composite_metadata_rows
    assert int(composite_metadata_rows[0]["component_count_stored"]) == 2, composite_metadata_rows


def test_register_job_uid_and_local_path_targets_publish_local_lineage(
    glaas_configured: Path,
    glaas_db_queryable,
    glaas_url: str,
    roar_cli,
    git_commit,
) -> None:
    del glaas_db_queryable
    repo = glaas_configured
    targets = _setup_local_target_matrix(
        repo,
        roar_cli=roar_cli,
        git_commit=git_commit,
        script_name="build_job_target.py",
        output_name="job_target_model.json",
        output_payload='{"status":"job"}',
    )

    job_result = roar_cli("register", targets["job_uid"], "--yes")
    assert job_result.returncode == 0, job_result.stderr or job_result.stdout
    job_session_hash = _parse_session_hash(job_result.stdout)
    assert job_session_hash == targets["session_hash"], job_result.stdout

    job_rows = _db_query_rows(
        """
        SELECT command
        FROM jobs
        WHERE session_hash = $1
        ORDER BY command ASC
        """,
        [job_session_hash],
    )
    assert any(targets["command"] == str(row["command"]) for row in job_rows), job_rows

    local_path_result = roar_cli("register", targets["local_output_path"], "--yes")
    assert local_path_result.returncode == 0, local_path_result.stderr or local_path_result.stdout
    assert _parse_session_hash(local_path_result.stdout) == targets["session_hash"], (
        local_path_result.stdout
    )

    published_paths = _db_query_rows(
        """
        SELECT jo.path, jo.artifact_hash
        FROM job_outputs jo
        JOIN jobs j ON j.id = jo.job_id
        WHERE j.session_hash = $1
        ORDER BY jo.path ASC
        """,
        [targets["session_hash"]],
    )
    assert any(str(row["path"]) == targets["local_output_path"] for row in published_paths), (
        published_paths
    )
    assert any(str(row["artifact_hash"]) == targets["local_hash"] for row in published_paths), (
        published_paths
    )

    local_public = _api_get(glaas_url, f"/api/v1/public/artifacts/{targets['local_hash']}")
    assert local_public.get("success") is True, local_public
    assert local_public["data"]["hash"] == targets["local_hash"], local_public


def test_register_session_hash_prefix_publishes_session_lineage(
    glaas_configured: Path,
    glaas_db_queryable,
    glaas_url: str,
    roar_cli,
    git_commit,
) -> None:
    del glaas_db_queryable
    repo = glaas_configured

    script_path = repo / "build_local_prefix.py"
    script_path.write_text(
        """
from pathlib import Path

Path("artifacts").mkdir(exist_ok=True)
Path("artifacts/prefix_model.json").write_text('{"status":"prefix"}\\n', encoding="utf-8")
print("built prefix_model.json")
""".strip()
        + "\n",
        encoding="utf-8",
    )
    git_commit("Add local prefix build script")

    run_result = roar_cli("run", sys.executable, "build_local_prefix.py")
    assert run_result.returncode == 0, run_result.stderr
    git_commit("After local prefix build")

    local_output_path = str((repo / "artifacts" / "prefix_model.json").resolve())
    local_hash = _artifact_hash_for_output(repo, local_output_path, "blake3")
    session_hash = _current_status_session_hash(repo, roar_cli)
    session_hash_prefix = session_hash[:8]

    register_result = roar_cli("register", session_hash_prefix, "--yes")
    assert register_result.returncode == 0, register_result.stderr or register_result.stdout

    session_rows = _db_query_rows("SELECT hash FROM sessions WHERE hash = $1", [session_hash])
    assert len(session_rows) == 1, session_rows

    job_rows = _db_query_rows(
        """
        SELECT command
        FROM jobs
        WHERE session_hash = $1
        ORDER BY command ASC
        """,
        [session_hash],
    )
    assert len(job_rows) >= 1, job_rows

    published_paths = _db_query_rows(
        """
        SELECT jo.path, jo.artifact_hash
        FROM job_outputs jo
        JOIN jobs j ON j.id = jo.job_id
        WHERE j.session_hash = $1
        ORDER BY jo.path ASC
        """,
        [session_hash],
    )
    assert any(str(row["path"]) == local_output_path for row in published_paths), published_paths
    assert any(str(row["artifact_hash"]) == local_hash for row in published_paths), published_paths

    local_public = _api_get(glaas_url, f"/api/v1/public/artifacts/{local_hash}")
    assert local_public.get("success") is True, local_public
    assert local_public["data"]["hash"] == local_hash, local_public
