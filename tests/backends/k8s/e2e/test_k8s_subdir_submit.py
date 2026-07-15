"""Subdirectory-invocation e2e: plan-time state must reach the root .roar.

Regression for the cwd/.roar bug: submitting from a nested project
directory used to save the fragment-session key (and prepared manifest)
under a freshly created `<subdir>/.roar`, while the finalizer loads the
key from the context-resolved project root — lineage was silently never
reconstituted. The submit itself succeeded, which is what made it easy
to miss.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from .conftest import KUBE_CONTEXT, NAMESPACE, TOOLS_BIN, kubectl
from .test_k8s_product_path import JOB_MANIFEST_TEMPLATE, wheel_server  # noqa: F401

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.k8s_e2e,
    pytest.mark.timeout(900),
]


@pytest.fixture(scope="module")
def subdir_run(
    k8s_cluster: None,
    glaas_health: str,
    wheel_server: dict[str, str],  # noqa: F811
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Any]:
    project_dir = tmp_path_factory.mktemp("k8s-subdir")
    subprocess.run(["git", "init", "-q"], cwd=project_dir, check=True)
    roar_dir = project_dir / ".roar"
    roar_dir.mkdir()
    (roar_dir / "config.toml").write_text(
        "\n".join(
            [
                "[glaas]",
                'url = "http://localhost:3001"',
                "",
                "[k8s]",
                "enabled = true",
                f'runtime_install_requirement = "{wheel_server["url"]}"',
                'cluster_glaas_url = "http://glaas:3001"',
                "wait_for_completion = true",
                "wait_timeout_seconds = 300",
                "poll_interval_seconds = 2.0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    nested = project_dir / "experiments" / "run-1"
    nested.mkdir(parents=True)

    job_name = f"roar-subdir-{uuid.uuid4().hex[:6]}"
    manifest_path = nested / "job.yaml"
    manifest_path.write_text(
        JOB_MANIFEST_TEMPLATE.format(job_name=job_name, namespace=NAMESPACE),
        encoding="utf-8",
    )

    (project_dir / ".gitignore").write_text(".roar/\n", encoding="utf-8")
    subprocess.run(["git", "config", "user.email", "e2e@example.com"], cwd=project_dir, check=True)
    subprocess.run(["git", "config", "user.name", "E2E"], cwd=project_dir, check=True)
    subprocess.run(["git", "add", "-A"], cwd=project_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=project_dir, check=True)

    env = dict(os.environ)
    env["PATH"] = f"{TOOLS_BIN}{os.pathsep}{env.get('PATH', '')}"
    env.pop("GLAAS_URL", None)
    env.pop("ROAR_PROJECT_DIR", None)

    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "roar",
                "run",
                "kubectl",
                "apply",
                "--context",
                KUBE_CONTEXT,
                "-f",
                "job.yaml",
            ],
            cwd=nested,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
        return {
            "project_dir": project_dir,
            "nested_dir": nested,
            "job_name": job_name,
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    finally:
        kubectl(["delete", f"job/{job_name}", "-n", NAMESPACE, "--ignore-not-found"], check=False)
        kubectl(
            [
                "delete",
                "secret",
                "-n",
                NAMESPACE,
                "-l",
                "app.kubernetes.io/managed-by=roar",
                "--ignore-not-found",
            ],
            check=False,
        )


def _describe(run: dict[str, Any]) -> str:
    return f"exit={run['exit_code']}\nstdout:\n{run['stdout']}\nstderr:\n{run['stderr']}"


def test_subdir_submit_reconstitutes_into_project_root(subdir_run: dict[str, Any]) -> None:
    assert subdir_run["exit_code"] == 0, _describe(subdir_run)
    combined = subdir_run["stdout"] + subdir_run["stderr"]
    assert "lineage reconstituted" in combined, _describe(subdir_run)
    assert "failed to load fragment session" not in combined, _describe(subdir_run)

    # No stray .roar in the invocation directory.
    assert not (Path(subdir_run["nested_dir"]) / ".roar").exists(), _describe(subdir_run)

    db_path = Path(subdir_run["project_dir"]) / ".roar" / "roar.db"
    assert db_path.is_file(), _describe(subdir_run)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        tasks = conn.execute("SELECT id FROM jobs WHERE job_type = 'k8s_task'").fetchall()
    finally:
        conn.close()
    assert len(tasks) == 1, _describe(subdir_run)
