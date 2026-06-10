from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from roar.db.context import create_database_context


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
        assert db_ctx._label_repo is not None
