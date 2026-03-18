from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from roar.db.context import create_database_context


def test_db_package_lazy_imports_heavy_submodules() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    script = """
import json
import sys
from pathlib import Path


def loaded():
    names = [
        "roar.db.context",
        "roar.db.engine",
        "roar.db.models",
        "roar.db.repositories",
        "roar.db.services",
        "sqlalchemy",
    ]
    return {name: name in sys.modules for name in names}


import roar.db as db
states = {"after_package_import": loaded()}

from roar.db import create_database_context

states["after_factory_import"] = loaded()
_ctx = create_database_context(Path(".roar"))
states["after_factory_call"] = loaded()

print(json.dumps(states))
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    states = json.loads(proc.stdout)

    assert states["after_package_import"] == {
        "roar.db.context": False,
        "roar.db.engine": False,
        "roar.db.models": False,
        "roar.db.repositories": False,
        "roar.db.services": False,
        "sqlalchemy": False,
    }
    assert states["after_factory_import"] == {
        "roar.db.context": True,
        "roar.db.engine": False,
        "roar.db.models": False,
        "roar.db.repositories": False,
        "roar.db.services": False,
        "sqlalchemy": False,
    }
    assert states["after_factory_call"] == states["after_factory_import"]


def test_database_context_initializes_repositories_and_services_lazily(tmp_path: Path) -> None:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()

    with create_database_context(roar_dir) as db_ctx:
        assert db_ctx._artifact_repo is None
        assert db_ctx._job_repo is None
        assert db_ctx._session_repo is None
        assert db_ctx._collection_repo is None
        assert db_ctx._composite_repo is None
        assert db_ctx._label_repo is None
        assert db_ctx._hashing_service is None
        assert db_ctx._session_service is None
        assert db_ctx._lineage_service is None
        assert db_ctx._job_recording_service is None

        session_repo = db_ctx.sessions
        assert db_ctx._session_repo is session_repo
        assert db_ctx._artifact_repo is None
        assert db_ctx._job_repo is None
        assert db_ctx._hashing_service is None

        job_repo = db_ctx.jobs
        assert db_ctx._job_repo is job_repo
        assert db_ctx._artifact_repo is not None
        assert db_ctx._hashing_service is None
        assert db_ctx._session_service is None

        session_service = db_ctx.session_service
        assert db_ctx._session_service is session_service
        assert db_ctx._hashing_service is None
        assert db_ctx._lineage_service is None
        assert db_ctx._job_recording_service is None

        job_recording = db_ctx.job_recording
        assert db_ctx._job_recording_service is job_recording
        assert db_ctx._hashing_service is not None
        assert db_ctx._session_service is session_service
        assert db_ctx._lineage_service is None
        assert db_ctx._collection_repo is None
        assert db_ctx._composite_repo is None
        assert db_ctx._label_repo is None
