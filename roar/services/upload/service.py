"""
Upload service for orchestrating artifact upload and registration.

Extracted from put.py to follow Single Responsibility Principle.
"""

from pathlib import Path
from typing import TYPE_CHECKING

from ...core.interfaces.upload import UploadResult
from ..vcs import GitAccessService
from .lineage_collector import LineageCollector

if TYPE_CHECKING:
    from ...core.interfaces.cloud import ICloudStorageProvider
    from ...core.interfaces.presenter import IPresenter
    from ...glaas_client import GlaasClient


class UploadService:
    """
    Service for orchestrating artifact upload and registration.

    Coordinates:
    - Preflight checks (GLaaS connectivity, git access)
    - File upload to cloud storage
    - Lineage collection
    - GLaaS registration
    - Reproducibility tagging

    Usage:
        service = UploadService(glaas_client, cloud_provider, presenter)
        result = service.upload_and_register(
            sources=[Path("data.csv")],
            dest_url="s3://bucket/path",
            force=False,
            tag=None,
            message="Training data",
            roar_dir=Path(".roar"),
        )
    """

    def __init__(
        self,
        glaas_client: "GlaasClient | None" = None,
        cloud_provider: "ICloudStorageProvider | None" = None,
        presenter: "IPresenter | None" = None,
    ):
        """
        Initialize upload service with dependencies.

        Args:
            glaas_client: GLaaS API client
            cloud_provider: Cloud storage provider for uploads
            presenter: Presenter for user feedback
        """
        self._glaas = glaas_client
        self._cloud = cloud_provider
        self._presenter = presenter
        self._lineage_collector = LineageCollector()
        self._git_access = GitAccessService()

    def _get_source_type(self, url: str | None) -> str:
        """Determine source type from destination URL."""
        if not url:
            return "output"
        if url.startswith("http"):
            return "https"
        return "output"

    def upload_and_register(
        self,
        sources: list[Path],
        dest_url: str,
        force: bool,
        tag: str | None,
        message: str | None,
        roar_dir: Path,
        git_repo: str | None = None,
        git_commit: str | None = None,
        repo_root: str | None = None,
        auto_tag: bool = False,
    ) -> UploadResult:
        """
        Upload artifacts and register with GLaaS.

        Args:
            sources: Source file/directory paths
            dest_url: Destination cloud URL
            force: Force upload even if exists
            tag: Explicit git tag to create
            message: Description for GLaaS
            roar_dir: Path to .roar directory
            git_repo: Git repository URL
            git_commit: Current git commit
            repo_root: Repository root path
            auto_tag: Automatically create reproducibility tag

        Returns:
            UploadResult with success status
        """
        warnings = []

        # Validate inputs
        if not sources:
            return UploadResult(success=False, error="No source files specified")

        # Resolve and validate source paths
        resolved_sources = []
        for src in sources:
            path = src.resolve() if isinstance(src, Path) else Path(src).resolve()
            if not path.exists():
                return UploadResult(success=False, error=f"Source not found: {src}")
            resolved_sources.append(path)

        # Preflight: Check GLaaS connectivity
        if self._glaas:
            health_ok, health_error = self._glaas.health_check()
            if not health_ok:
                return UploadResult(success=False, error=f"GLaaS not available: {health_error}")

        # Preflight: Check git push access (for tagging)
        if (tag or auto_tag) and git_repo and repo_root:
            access = self._git_access.check_push_access(git_repo, repo_root)
            if not access.has_access:
                warnings.append(f"Git push access warning: {access.error}")
                # Don't fail, just warn and skip tagging

        # Upload files to cloud storage (if cloud provider available)
        uploaded_count = 0

        if self._cloud:
            for src in resolved_sources:
                ok, error = self._upload_file(src, dest_url, force)
                if ok:
                    uploaded_count += 1
                else:
                    return UploadResult(
                        success=False,
                        error=f"Upload failed for {src}: {error}",
                        warnings=warnings,
                    )

        # Get hashes from local database (regardless of cloud upload)
        uploaded_hashes = self._get_local_hashes(resolved_sources, roar_dir)
        if not self._cloud:
            uploaded_count = len(uploaded_hashes)

        # Collect lineage data
        if not uploaded_hashes:
            return UploadResult(
                success=False,
                error="No artifacts found to register",
                warnings=warnings,
            )

        lineage = self._lineage_collector.collect(uploaded_hashes, roar_dir)

        # Get session hash for artifact registration
        session_hash = lineage.pipeline.get("hash") if lineage.pipeline else None

        # Register with GLaaS
        registered_count = 0
        if self._glaas:
            source_type = self._get_source_type(dest_url)
            for artifact in lineage.artifacts:
                if not session_hash:
                    continue  # Can't register without session
                artifact_size = artifact.get("size")
                if artifact_size is None:
                    continue  # Skip artifacts without size
                ok, _err = self._glaas.register_artifact(
                    hashes=artifact.get("hashes", []),
                    size=int(artifact_size),
                    source_type=source_type,
                    session_hash=session_hash,
                    source_url=dest_url if dest_url else None,
                )
                if ok:
                    registered_count += 1

            # Note: Jobs are registered via register_job separately, not here

        return UploadResult(
            success=True,
            artifacts_uploaded=uploaded_count,
            artifacts_registered=registered_count,
            lineage_jobs=len(lineage.jobs),
            warnings=warnings,
        )

    def _upload_file(
        self,
        source: Path,
        dest_url: str,
        force: bool,
    ) -> tuple[bool, str | None]:
        """
        Upload a single file to cloud storage.

        Returns:
            Tuple of (success, hash_or_error)
            - On success: (True, None) - hash will be looked up from DB separately
            - On failure: (False, error_message)
        """
        if not self._cloud:
            return False, "No cloud provider configured"

        try:
            # Check if already exists (unless force)
            dest_path = f"{dest_url.rstrip('/')}/{source.name}"
            if not force and hasattr(self._cloud, "exists") and self._cloud.exists(dest_path):
                return True, None  # Already exists

            # Upload - interface returns tuple[bool, str] (success, error_message)
            success, error = self._cloud.upload(str(source), dest_path)
            if success:
                return True, None
            else:
                return False, error

        except AttributeError as e:
            # Cloud provider doesn't have required method
            return False, f"Cloud provider missing method: {e}"
        except Exception as e:
            return False, str(e)

    def _get_local_hashes(
        self,
        sources: list[Path],
        roar_dir: Path,
    ) -> list[str]:
        """Get artifact hashes for local files from the database."""
        from ...db.context import create_database_context

        hashes = []
        with create_database_context(roar_dir) as ctx:
            for src in sources:
                # Look up artifact by path in outputs
                artifact = self._find_artifact_by_path(ctx, str(src))
                if artifact:
                    for h in artifact.get("hashes", []):
                        if h.get("algorithm") == "blake3":
                            hashes.append(h.get("digest"))
                            break
        return hashes

    def _find_artifact_by_path(self, ctx, path: str) -> dict | None:
        """Find artifact by output path."""
        # Check recent outputs
        outputs = ctx.artifacts.get_all_outputs_with_paths()
        for out in outputs:
            if out.get("path") == path:
                return ctx.artifacts.get(out["artifact_id"])
        return None
