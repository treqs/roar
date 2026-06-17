"""Environment-preparation helpers for reproduction workflows.

Reproduction needs the recorded *code* on disk before it can run. ``resolve_code_source``
picks the best available source, in priority order:

1. **matching local repo** — the recorded commit is reachable in the repo you're
   standing in (with or without a remote): reproduce **in place**, checking out
   the recorded commit and re-running the steps in your working tree (outputs are
   recreated where they belong). Refused only if you have uncommitted *changes*
   that would be clobbered.
2. **recorded remote** — not in a repo, but a remote URL was recorded: clone it.
3. **rerun-only** — no repo/commit was recorded at all (e.g. a package-only run
   like ``roar run wget …``): re-run the recorded command in a scratch dir.

Hard errors (never silently guess): you're in a git repo that doesn't match the
lineage; you have uncommitted modifications; or the code is a commit with no
remote and you're not in its repo.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from ...core.interfaces.reproduction import EnvironmentInfo, PipelineInfo
from ...execution.reproduction.environment_setup import EnvironmentSetupService
from ...utils.git_url import urls_match


@dataclass
class CodeSourcePlan:
    """How reproduction will obtain the recorded code."""

    kind: str  # "reuse" | "clone" | "rerun"
    repo_dir: Path | None = None  # the in-place repo, or pre-made scratch dir


def prepare_reproduction_environment(
    *,
    pipeline: PipelineInfo,
    cwd: Path,
    presenter,
    auto_confirm: bool,
    dpkg_any_version: bool = False,
    pip_any_version: bool = False,
    package_sync: bool = False,
) -> EnvironmentInfo:
    """Resolve the code source, then build the environment around it."""
    plan = resolve_code_source(cwd, pipeline, presenter=presenter)
    service = EnvironmentSetupService(presenter=presenter)

    if plan.kind == "reuse":
        # In place: the user's repo already holds the code (now at the recorded
        # commit) and its environment. Don't clone or rebuild — run the recorded
        # steps where they belong, reusing the existing venv if present.
        assert plan.repo_dir is not None
        venv = plan.repo_dir / ".venv" if (plan.repo_dir / ".venv").is_dir() else None
        return EnvironmentInfo(repo_dir=plan.repo_dir, venv_dir=venv, python_version=None)

    if plan.kind == "clone":
        target_dir = cwd / "reproduce"
        presenter.print(f"\nSetting up environment in {target_dir}...")
        return service.setup(
            pipeline,
            target_dir,
            auto_confirm,
            dpkg_any_version=dpkg_any_version,
            pip_any_version=pip_any_version,
            package_sync=package_sync,
        )

    # rerun: a repo-less run; the scratch dir is already made by the resolver.
    assert plan.repo_dir is not None
    presenter.print(f"\nSetting up environment in {plan.repo_dir}...")
    return service.setup_in_place(
        pipeline,
        plan.repo_dir,
        auto_confirm,
        dpkg_any_version=dpkg_any_version,
        pip_any_version=pip_any_version,
        package_sync=package_sync,
    )


def resolve_code_source(cwd: Path, pipeline: PipelineInfo, *, presenter) -> CodeSourcePlan:
    """Pick where reproduction's code comes from (see module docstring).

    Raises ``ValueError`` for the cases roar refuses to guess through.
    """
    commit = pipeline.git_commit or None
    recorded_repo = pipeline.git_repo or None
    repo_root = _git_toplevel(cwd)

    if repo_root is not None:
        origin = _git_origin(repo_root)
        commit_here = commit is not None and _commit_exists(repo_root, commit)
        origin_matches = (
            recorded_repo is not None and origin is not None and urls_match(origin, recorded_repo)
        )
        if not (commit_here or origin_matches):
            raise ValueError(
                "You're inside a git repository that doesn't match this lineage "
                f"(recorded repo: {recorded_repo or 'none'}).\n"
                "Reproduce from an empty directory, or cd into the lineage's own repository."
            )

        # Matching repo — reproduce IN PLACE. Refuse only if there are uncommitted
        # *modifications* we could clobber; deleted/untracked files are fine
        # (reproduction recreates the recorded outputs).
        modified = _uncommitted_modifications(repo_root)
        if modified:
            raise ValueError(
                f"Your working tree has uncommitted changes to {modified} tracked file(s). "
                "Reproduction runs in place and could overwrite them — commit or stash first, "
                "then retry."
            )

        if commit and not commit_here:
            presenter.print("Fetching the recorded commit from origin...")
            _git_fetch(repo_root)
            commit_here = _commit_exists(repo_root, commit)
            if not commit_here:
                raise ValueError(
                    f"Recorded commit {commit[:12]} was not found locally or on origin."
                )
        if commit:
            checkout = _run_git(["checkout", commit], cwd=repo_root)
            if checkout.returncode != 0:
                presenter.print(
                    f"Warning: could not check out {commit[:12]} "
                    f"({checkout.stderr.strip()}); using current HEAD"
                )

        presenter.print(
            f"Current repository matches the recorded lineage — reproducing in place in "
            f"{repo_root}.\n"
            "⚠  This re-runs the recorded steps in your working tree; recorded outputs "
            "may be overwritten."
        )
        return CodeSourcePlan(kind="reuse", repo_dir=Path(repo_root))

    # Not in a git repo.
    if recorded_repo:
        return CodeSourcePlan(kind="clone")
    if not commit:
        # No code under version control was recorded (e.g. a package-only run);
        # the faithful reproduction is to re-run the recorded command.
        scratch = cwd / "reproduce" / "rerun"
        scratch.mkdir(parents=True, exist_ok=True)
        presenter.print(
            "No git repository was recorded — reproducing by re-running the recorded command."
        )
        return CodeSourcePlan(kind="rerun", repo_dir=scratch)

    # A commit was recorded, but there's no remote and we're not in its repo:
    # the code genuinely isn't reachable from here.
    raise ValueError(
        f"This lineage's code is git commit {commit[:12]}, but it has no published remote "
        "and you're not inside its repository.\n"
        "Run `roar reproduce` from inside that repository, or push it to a remote and retry."
    )


def _run_git(args: list[str], cwd: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=str(cwd))


def _git_toplevel(cwd: Path) -> str | None:
    try:
        result = _run_git(["rev-parse", "--show-toplevel"], cwd=cwd)
    except FileNotFoundError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _git_origin(repo_root: str) -> str | None:
    result = _run_git(["remote", "get-url", "origin"], cwd=repo_root)
    return result.stdout.strip() if result.returncode == 0 else None


def _commit_exists(repo_root: str, commit: str) -> bool:
    return _run_git(["cat-file", "-e", f"{commit}^{{commit}}"], cwd=repo_root).returncode == 0


def _uncommitted_modifications(repo_root: str) -> int:
    """Count uncommitted changes that reproduction could clobber.

    Counts modifications/additions/renames to tracked files. Deleted files and
    untracked files are NOT counted — reproduction recreates the recorded
    outputs, so deleting one and reproducing to restore it is a supported flow.
    """
    result = _run_git(["status", "--porcelain"], cwd=repo_root)
    count = 0
    for line in result.stdout.splitlines():
        if not line:
            continue
        code = line[:2]
        if code == "??":  # untracked — may be a to-be-recreated output
            continue
        if code.strip() == "D":  # deleted — reproduction recreates outputs
            continue
        count += 1
    return count


def _git_fetch(repo_root: str) -> None:
    _run_git(["fetch", "--all", "--quiet"], cwd=repo_root)
