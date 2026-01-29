"""
Data transfer objects for registration operations.

These DTOs provide type-safe data structures for passing registration
data between services and the GLaaS client.
"""

from dataclasses import dataclass, field


@dataclass
class HashEntry:
    """A single hash entry with algorithm and digest."""

    algorithm: str
    digest: str

    def to_dict(self) -> dict[str, str]:
        """Convert to dict for API calls."""
        return {"algorithm": self.algorithm, "digest": self.digest}


@dataclass
class JobIODTO:
    """Input/output item for job registration."""

    hash: str
    path: str

    def to_dict(self) -> dict[str, str]:
        """Convert to dict for API calls."""
        return {"hash": self.hash, "path": self.path}


@dataclass
class ArtifactDTO:
    """Artifact data for registration."""

    hashes: list[HashEntry]
    size: int
    source_type: str | None = None
    source_url: str | None = None
    metadata: str | None = None

    def to_dict(self, session_hash: str) -> dict:
        """Convert to dict for API calls."""
        result = {
            "hashes": [h.to_dict() for h in self.hashes],
            "size": self.size,
            "source_type": self.source_type,
            "session_hash": session_hash,
        }
        if self.source_url:
            result["source_url"] = self.source_url
        if self.metadata:
            result["metadata"] = self.metadata
        return result

    @classmethod
    def from_dict(cls, data: dict) -> "ArtifactDTO":
        """Create from dict (e.g., from database query)."""
        hashes = []
        if "hashes" in data:
            hashes = [HashEntry(**h) for h in data["hashes"]]
        elif "hash" in data:
            # Single hash with assumed algorithm
            hashes = [HashEntry(algorithm="blake3", digest=data["hash"])]

        return cls(
            hashes=hashes,
            size=data.get("size", 0),
            source_type=data.get("source_type"),
            source_url=data.get("source_url"),
            metadata=data.get("metadata"),
        )


@dataclass
class JobDTO:
    """Job data for registration."""

    job_uid: str
    command: str
    timestamp: float
    git_commit: str
    git_branch: str
    duration_seconds: float
    exit_code: int
    job_type: str | None
    step_number: int
    inputs: list[JobIODTO] = field(default_factory=list)
    outputs: list[JobIODTO] = field(default_factory=list)
    metadata: str | None = None

    def to_create_dict(self, session_hash: str) -> dict:
        """Convert to dict for job creation (without I/O links)."""
        return {
            "command": self.command,
            "timestamp": self.timestamp,
            "session_hash": session_hash,
            "job_uid": self.job_uid,
            "git_commit": self.git_commit,
            "git_branch": self.git_branch,
            "duration_seconds": self.duration_seconds,
            "exit_code": self.exit_code,
            "job_type": self.job_type,
            "step_number": self.step_number,
            "metadata": self.metadata,
        }

    def to_link_dict(self) -> dict:
        """Convert to dict for artifact linking."""
        return {
            "inputs": [io.to_dict() for io in self.inputs] if self.inputs else None,
            "outputs": [io.to_dict() for io in self.outputs] if self.outputs else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "JobDTO":
        """Create from dict (e.g., from database query)."""
        inputs = []
        outputs = []

        # Handle _inputs/_outputs format from lineage queries
        for inp in data.get("_inputs", []):
            if inp.get("hash") and inp.get("path"):
                inputs.append(JobIODTO(hash=inp["hash"], path=inp["path"]))

        for out in data.get("_outputs", []):
            if out.get("hash") and out.get("path"):
                outputs.append(JobIODTO(hash=out["hash"], path=out["path"]))

        return cls(
            job_uid=data.get("job_uid", ""),
            command=data.get("command", ""),
            timestamp=data.get("timestamp", 0.0),
            git_commit=data.get("git_commit", ""),
            git_branch=data.get("git_branch", ""),
            duration_seconds=data.get("duration_seconds", 0.0),
            exit_code=data.get("exit_code", 0),
            job_type=data.get("job_type"),
            step_number=data.get("step_number", 0),
            inputs=inputs,
            outputs=outputs,
            metadata=data.get("metadata"),
        )


@dataclass
class SessionDTO:
    """Session data for registration."""

    session_hash: str
    git_repo: str
    git_commit: str
    git_branch: str

    def to_dict(self) -> dict:
        """Convert to dict for API calls."""
        return {
            "hash": self.session_hash,
            "git_repo": self.git_repo,
            "git_commit": self.git_commit,
            "git_branch": self.git_branch,
        }
