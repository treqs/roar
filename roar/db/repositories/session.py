"""
SQLAlchemy session repository implementation.

Handles session and step management operations.
"""

import re
import secrets
import time
from pathlib import Path
from typing import Any

import blake3
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session as SASession

from ...core.interfaces.repositories import SessionRepository
from ..models import Job, Session


class SQLAlchemySessionRepository(SessionRepository):
    """
    SQLAlchemy implementation of session repository.

    Manages session records and their associated steps.
    """

    def __init__(self, session: SASession):
        """
        Initialize repository with database session.

        Args:
            session: SQLAlchemy session
        """
        self._session = session

    def normalize_path(self, path: str, repo_root: str | None = None) -> str:
        """
        Normalize a path for step identity computation.

        Args:
            path: Absolute file path
            repo_root: Repository root directory

        Returns:
            Normalized relative path with numeric sequences globified.
        """
        path_obj = Path(path).resolve()
        home = Path.home()

        if repo_root:
            repo_root_obj = Path(repo_root).resolve()
            try:
                rel = path_obj.relative_to(repo_root_obj)
                return self._globify_numbers(str(rel))
            except ValueError:
                pass

        try:
            rel = path_obj.relative_to(home)
            return self._globify_numbers(str(rel))
        except ValueError:
            pass

        return self._globify_numbers(path_obj.name)

    @staticmethod
    def _globify_numbers(path_str: str) -> str:
        """
        Replace numeric sequences in filenames with wildcards.

        Args:
            path_str: Path string

        Returns:
            Path with numeric sequences replaced by wildcards.
        """
        result = re.sub(r"([_-])(\d{3,})(?=\.|$|/)", r"\1*", path_str)
        result = re.sub(r"([_-])(\d{3,})/", r"\1*/", result)
        return result

    def compute_step_identity(
        self,
        input_paths: list[str],
        output_paths: list[str],
        repo_root: str | None = None,
        command: str | None = None,
    ) -> str:
        """
        Compute step identity hash from normalized input and output paths.

        Args:
            input_paths: List of input file paths
            output_paths: List of output file paths
            repo_root: Repository root directory
            command: Command string (used if no inputs/outputs)

        Returns:
            BLAKE3 hash identifying this step.
        """
        from .job import SQLAlchemyJobRepository

        norm_inputs = (
            sorted(self.normalize_path(p, repo_root) for p in input_paths) if input_paths else []
        )
        norm_outputs = (
            sorted(self.normalize_path(p, repo_root) for p in output_paths) if output_paths else []
        )

        if not norm_inputs and not norm_outputs and command:
            script = SQLAlchemyJobRepository._extract_script(command) or command
            identity_str = f"COMMAND:{script}"
            return blake3.blake3(identity_str.encode()).hexdigest()

        identity_parts = ["INPUTS:", *norm_inputs, "OUTPUTS:", *norm_outputs]
        identity_str = "\0".join(identity_parts)

        return blake3.blake3(identity_str.encode()).hexdigest()

    def get_or_create_active(self) -> int:
        """
        Get the active session, creating one if none exists.

        Returns:
            Session ID.
        """
        session = self._session.execute(
            select(Session).where(Session.is_active == 1)
        ).scalar_one_or_none()

        if session:
            return session.id

        session_hash = secrets.token_hex(32)  # 64-char hex string
        new_session = Session(
            created_at=time.time(),
            is_active=1,
            hash=session_hash,
        )
        self._session.add(new_session)
        self._session.flush()
        return new_session.id

    def get_active(self) -> dict[str, Any] | None:
        """
        Get the currently active session.

        Returns:
            Session dict or None if no active session.
        """
        session = self._session.execute(
            select(Session).where(Session.is_active == 1)
        ).scalar_one_or_none()
        return self._session_to_dict(session) if session else None

    def set_active(self, session_id: int) -> None:
        """
        Set a session as active (deactivates others).

        Args:
            session_id: Session ID to activate
        """
        self._session.execute(update(Session).values(is_active=0))
        self._session.execute(update(Session).where(Session.id == session_id).values(is_active=1))
        self._session.flush()

    def create(
        self,
        source_artifact_hash: str | None = None,
        git_repo: str | None = None,
        git_commit: str | None = None,
        make_active: bool = True,
    ) -> int:
        """
        Create a new session.

        Args:
            source_artifact_hash: Hash of source artifact
            git_repo: Git repository URL
            git_commit: Git commit hash
            make_active: Whether to set as active session

        Returns:
            New session ID.
        """
        if make_active:
            self._session.execute(update(Session).values(is_active=0))

        session_hash = secrets.token_hex(32)  # 64-char hex string
        session = Session(
            created_at=time.time(),
            source_artifact_hash=source_artifact_hash,
            git_repo=git_repo,
            git_commit_start=git_commit,
            is_active=1 if make_active else 0,
            hash=session_hash,
        )
        self._session.add(session)
        self._session.flush()
        return session.id

    def get(self, session_id: int) -> dict[str, Any] | None:
        """
        Get a session by ID.

        Args:
            session_id: Session ID

        Returns:
            Session dict or None if not found.
        """
        session = self._session.get(Session, session_id)
        return self._session_to_dict(session) if session else None

    def get_by_hash(self, session_hash: str) -> dict[str, Any] | None:
        """
        Get a session by its content hash.

        Args:
            session_hash: Session content hash

        Returns:
            Session dict or None if not found.
        """
        session = self._session.execute(
            select(Session).where(Session.hash == session_hash)
        ).scalar_one_or_none()
        return self._session_to_dict(session) if session else None

    def get_steps(self, session_id: int) -> list[dict[str, Any]]:
        """
        Get all steps (jobs) in a session, ordered by step number.

        Args:
            session_id: Session ID

        Returns:
            List of job dicts.
        """
        jobs = (
            self._session.execute(
                select(Job)
                .where(Job.session_id == session_id)
                .order_by(Job.step_number.asc(), Job.timestamp.asc())
            )
            .scalars()
            .all()
        )
        return [self._job_to_dict(j) for j in jobs]

    def get_step_by_identity(self, session_id: int, step_identity: str) -> dict[str, Any] | None:
        """
        Find a step in a session by its identity hash.

        Args:
            session_id: Session ID
            step_identity: Step identity hash

        Returns:
            Job dict or None if not found.
        """
        job = self._session.execute(
            select(Job)
            .where(Job.session_id == session_id, Job.step_identity == step_identity)
            .order_by(Job.timestamp.desc())
            .limit(1)
        ).scalar_one_or_none()
        return self._job_to_dict(job) if job else None

    def get_step_by_number(
        self, session_id: int, step_number: int, job_type: str | None = None
    ) -> dict[str, Any] | None:
        """
        Get a step by its number in the session.

        Args:
            session_id: Session ID
            step_number: Step number
            job_type: Filter by job type

        Returns:
            Job dict or None if not found.
        """
        if job_type == "build":
            query = (
                select(Job)
                .where(
                    Job.session_id == session_id,
                    Job.step_number == step_number,
                    Job.job_type == "build",
                )
                .order_by(Job.timestamp.desc())
                .limit(1)
            )
        else:
            query = (
                select(Job)
                .where(
                    Job.session_id == session_id,
                    Job.step_number == step_number,
                    Job.job_type.is_(None) | (Job.job_type == "run"),
                )
                .order_by(Job.timestamp.desc())
                .limit(1)
            )
        job = self._session.execute(query).scalar_one_or_none()
        return self._job_to_dict(job) if job else None

    def get_step_by_name(self, session_id: int, step_name: str) -> dict[str, Any] | None:
        """
        Get a step by its user-assigned name.

        Args:
            session_id: Session ID
            step_name: Step name

        Returns:
            Job dict or None if not found.
        """
        job = self._session.execute(
            select(Job)
            .where(Job.session_id == session_id, Job.step_name == step_name)
            .order_by(Job.timestamp.desc())
            .limit(1)
        ).scalar_one_or_none()
        return self._job_to_dict(job) if job else None

    def get_next_step_number(self, session_id: int) -> int:
        """
        Get the next step number for a session.

        Args:
            session_id: Session ID

        Returns:
            Next available step number.
        """
        max_step = self._session.execute(
            select(func.max(Job.step_number)).where(Job.session_id == session_id)
        ).scalar()
        return (max_step or 0) + 1

    def update_current_step(self, session_id: int, step_number: int) -> None:
        """
        Update the current step position in a session.

        Args:
            session_id: Session ID
            step_number: Current step number
        """
        self._session.execute(
            update(Session).where(Session.id == session_id).values(current_step=step_number)
        )
        self._session.flush()

    def update_git_commits(
        self, session_id: int, git_commit: str, update_start: bool = False
    ) -> None:
        """
        Update git commit references for a session.

        Args:
            session_id: Session ID
            git_commit: Git commit hash
            update_start: Whether to update start commit if not set
        """
        session = self._session.get(Session, session_id)
        if not session:
            return

        if update_start and not session.git_commit_start:
            session.git_commit_start = git_commit
        session.git_commit_end = git_commit
        self._session.flush()

    def rename_step(
        self, session_id: int, step_number: int, new_name: str, job_type: str | None = None
    ) -> None:
        """
        Rename a step in a session.

        Args:
            session_id: Session ID
            step_number: Step number to rename
            new_name: New step name
            job_type: Filter by job type
        """
        if job_type == "build":
            self._session.execute(
                update(Job)
                .where(
                    Job.session_id == session_id,
                    Job.step_number == step_number,
                    Job.job_type == "build",
                )
                .values(step_name=new_name)
            )
        else:
            self._session.execute(
                update(Job)
                .where(
                    Job.session_id == session_id,
                    Job.step_number == step_number,
                    Job.job_type.is_(None) | (Job.job_type == "run"),
                )
                .values(step_name=new_name)
            )
        self._session.flush()

    def get_step_for_job(self, session_id: int, job_id: int) -> dict[str, Any] | None:
        """
        Get the step info for a job in a session.

        Args:
            session_id: Session ID
            job_id: Job database ID

        Returns:
            Dict with id, step_number, step_name, command, or None.
        """
        job = self._session.execute(
            select(Job.id, Job.step_number, Job.step_name, Job.command).where(
                Job.session_id == session_id, Job.id == job_id
            )
        ).first()
        if not job:
            return None
        return {
            "id": job.id,
            "step_number": job.step_number,
            "step_name": job.step_name,
            "command": job.command,
        }

    def clear(self, session_id: int) -> None:
        """
        Clear a session by removing it and disassociating its jobs.

        Args:
            session_id: Session ID to clear
        """
        self._session.execute(
            update(Job)
            .where(Job.session_id == session_id)
            .values(session_id=None, step_number=None, step_name=None)
        )
        self._session.execute(delete(Session).where(Session.id == session_id))
        self._session.flush()

    def populate_from_server(
        self,
        source_artifact_hash: str,
        jobs: list[dict[str, Any]],
        git_repo: str | None = None,
        git_commit: str | None = None,
    ) -> int:
        """
        Populate a session from server DAG data.

        Args:
            source_artifact_hash: Hash of source artifact
            jobs: List of job dicts from server
            git_repo: Git repository URL
            git_commit: Git commit hash

        Returns:
            New session ID.
        """
        session_id = self.create(
            source_artifact_hash=source_artifact_hash,
            git_repo=git_repo,
            git_commit=git_commit,
            make_active=True,
        )

        for job_data in jobs:
            step_num = job_data.get("step_number", 1)
            command = job_data.get("command", "")
            job_git_repo = job_data.get("git_repo") or git_repo
            job_git_commit = job_data.get("git_commit") or git_commit
            job_type = job_data.get("job_type")

            job = Job(
                timestamp=time.time(),
                command=command,
                session_id=session_id,
                step_number=step_num,
                git_repo=job_git_repo,
                git_commit=job_git_commit,
                status="pending",
                job_type=job_type,
            )
            self._session.add(job)

        self._session.flush()
        return session_id

    def update_hash(self, session_id: int, job_repo) -> None:
        """
        No-op: Session hash is generated at creation time and never changes.

        Kept for backward compatibility.
        """
        pass

    def check_git_consistency(self, session_id: int) -> dict[str, Any]:
        """
        Check if a session has mixed git commits.

        Args:
            session_id: Session ID

        Returns:
            Dict with consistent (bool), commits (list), and warning (str or None).
        """
        commits_result = (
            self._session.execute(
                select(Job.git_commit)
                .where(Job.session_id == session_id, Job.git_commit.isnot(None))
                .distinct()
            )
            .scalars()
            .all()
        )

        commits = [c for c in commits_result if c]

        return {
            "consistent": len(commits) <= 1,
            "commits": commits,
            "warning": None
            if len(commits) <= 1
            else f"Pipeline has {len(commits)} different git commits: {', '.join(c[:8] for c in commits)}",
        }

    def get_summary(self, session_id: int, job_repo) -> dict[str, Any] | None:
        """
        Get a summary of a session for display.

        Args:
            session_id: Session ID
            job_repo: Job repository (for inputs/outputs lookup)

        Returns:
            Summary dict or None if session not found.
        """
        session = self.get(session_id)
        if not session:
            return None

        steps = self.get_steps(session_id)
        git_check = self.check_git_consistency(session_id)

        unique_steps: dict[int, dict] = {}
        for step in steps:
            num = step["step_number"]
            if num not in unique_steps or step["timestamp"] > unique_steps[num]["timestamp"]:
                unique_steps[num] = step

        return {
            "id": session_id,
            "hash": session.get("hash"),
            "created_at": session["created_at"],
            "current_step": session["current_step"],
            "total_steps": len(unique_steps),
            "is_active": session["is_active"],
            "git_consistent": git_check["consistent"],
            "git_warning": git_check.get("warning"),
            "steps": [unique_steps[n] for n in sorted(unique_steps.keys())],
        }

    def _session_to_dict(self, session: Session) -> dict[str, Any]:
        """Convert Session model to dict."""
        return {
            "id": session.id,
            "hash": session.hash,
            "created_at": session.created_at,
            "source_artifact_hash": session.source_artifact_hash,
            "current_step": session.current_step,
            "is_active": session.is_active,
            "git_repo": session.git_repo,
            "git_commit_start": session.git_commit_start,
            "git_commit_end": session.git_commit_end,
            "synced_at": session.synced_at,
            "metadata": session.metadata_,
        }

    def _job_to_dict(self, job: Job) -> dict[str, Any]:
        """Convert Job model to dict."""
        return {
            "id": job.id,
            "job_uid": job.job_uid,
            "timestamp": job.timestamp,
            "command": job.command,
            "script": job.script,
            "step_identity": job.step_identity,
            "session_id": job.session_id,
            "step_number": job.step_number,
            "step_name": job.step_name,
            "git_repo": job.git_repo,
            "git_commit": job.git_commit,
            "git_branch": job.git_branch,
            "duration_seconds": job.duration_seconds,
            "exit_code": job.exit_code,
            "synced_at": job.synced_at,
            "status": job.status,
            "job_type": job.job_type,
            "metadata": job.metadata_,
            "telemetry": job.telemetry,
        }


# Backward compatibility alias
SQLiteSessionRepository = SQLAlchemySessionRepository
