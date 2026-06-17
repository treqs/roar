"""Tests for verbosity resolution and the verbosity-aware run report.

Covers:
  - resolve_verbosity precedence (CLI > config-verbosity > config-quiet > default)
  - Mutual exclusion of -q and -v
  - Soft deprecation of output.quiet=true
  - File-filter category counts and gated dropped-path collection
  - RunReportPresenter rendering at quiet / normal / verbose / debug
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from roar.application.run import verbosity as verbosity_mod
from roar.application.run.verbosity import resolve_verbosity
from roar.core.models.provenance import (
    FilteredFiles,
    PythonInjectData,
    TracerData,
)
from roar.core.models.run import RunResult
from roar.execution.provenance.file_filter import FileFilterService
from roar.presenters.run_report import RunReportPresenter
from roar.presenters.terminal import TerminalCaps


@pytest.fixture(autouse=True)
def _reset_deprecation_latch():
    verbosity_mod._reset_deprecation_latch()
    yield
    verbosity_mod._reset_deprecation_latch()


def _write_config(tmp_path: Path, body: str) -> Path:
    cfg_dir = tmp_path / ".roar"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.toml").write_text(body)
    return tmp_path


# ---------------------------------------------------------------------------
# resolve_verbosity precedence
# ---------------------------------------------------------------------------


class TestResolveVerbosityPrecedence:
    def test_default_is_normal(self, tmp_path: Path) -> None:
        assert resolve_verbosity(cli_quiet=False, cli_verbose=0, repo_root=tmp_path) == "normal"

    def test_cli_quiet_wins_over_config_verbose(self, tmp_path: Path) -> None:
        _write_config(tmp_path, '[output]\nverbosity = "verbose"\n')
        assert resolve_verbosity(cli_quiet=True, cli_verbose=0, repo_root=tmp_path) == "quiet"

    def test_cli_v_maps_to_verbose(self, tmp_path: Path) -> None:
        assert resolve_verbosity(cli_quiet=False, cli_verbose=1, repo_root=tmp_path) == "verbose"

    def test_cli_vv_maps_to_debug(self, tmp_path: Path) -> None:
        assert resolve_verbosity(cli_quiet=False, cli_verbose=2, repo_root=tmp_path) == "debug"

    def test_cli_vvv_caps_at_debug(self, tmp_path: Path) -> None:
        assert resolve_verbosity(cli_quiet=False, cli_verbose=5, repo_root=tmp_path) == "debug"

    def test_config_verbosity_used_when_no_cli(self, tmp_path: Path) -> None:
        _write_config(tmp_path, '[output]\nverbosity = "debug"\n')
        assert resolve_verbosity(cli_quiet=False, cli_verbose=0, repo_root=tmp_path) == "debug"

    def test_config_verbosity_takes_precedence_over_quiet(self, tmp_path: Path) -> None:
        """If both keys exist, verbosity wins (and no deprecation noise fires)."""
        _write_config(tmp_path, '[output]\nverbosity = "verbose"\nquiet = true\n')
        stream = io.StringIO()
        result = resolve_verbosity(
            cli_quiet=False, cli_verbose=0, repo_root=tmp_path, stream=stream
        )
        assert result == "verbose"
        assert stream.getvalue() == ""

    def test_legacy_quiet_true_resolves_to_quiet_with_warning(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "[output]\nquiet = true\n")
        stream = io.StringIO()
        result = resolve_verbosity(
            cli_quiet=False, cli_verbose=0, repo_root=tmp_path, stream=stream
        )
        assert result == "quiet"
        assert "deprecated" in stream.getvalue()
        assert "output.verbosity" in stream.getvalue()

    def test_legacy_quiet_false_silent_no_warning(self, tmp_path: Path) -> None:
        """Templates ship with `quiet = false`; that's the same as default — don't nag."""
        _write_config(tmp_path, "[output]\nquiet = false\n")
        stream = io.StringIO()
        result = resolve_verbosity(
            cli_quiet=False, cli_verbose=0, repo_root=tmp_path, stream=stream
        )
        assert result == "normal"
        assert stream.getvalue() == ""

    def test_deprecation_warning_fires_only_once_per_process(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "[output]\nquiet = true\n")
        s1 = io.StringIO()
        s2 = io.StringIO()
        resolve_verbosity(cli_quiet=False, cli_verbose=0, repo_root=tmp_path, stream=s1)
        resolve_verbosity(cli_quiet=False, cli_verbose=0, repo_root=tmp_path, stream=s2)
        assert "deprecated" in s1.getvalue()
        assert s2.getvalue() == ""

    def test_invalid_config_value_raises(self, tmp_path: Path) -> None:
        import click

        _write_config(tmp_path, '[output]\nverbosity = "loud"\n')
        with pytest.raises(click.UsageError):
            resolve_verbosity(cli_quiet=False, cli_verbose=0, repo_root=tmp_path)

    def test_quiet_and_verbose_combined_raises(self, tmp_path: Path) -> None:
        import click

        with pytest.raises(click.UsageError):
            resolve_verbosity(cli_quiet=True, cli_verbose=1, repo_root=tmp_path)


# ---------------------------------------------------------------------------
# File filter: category counts + gated path collection
# ---------------------------------------------------------------------------


def _make_data(reads: list[str], writes: list[str]) -> tuple[TracerData, PythonInjectData]:
    tracer = TracerData(
        opened_files=[],
        read_files=reads,
        written_files=writes,
        processes=[],
        start_time=0.0,
        end_time=1.0,
    )
    py = PythonInjectData(modules_files=[])
    return tracer, py


class TestFileFilterCounts:
    def test_counts_per_category(self) -> None:
        reads = [
            "/sys/foo",
            "/etc/passwd",
            "/tmp/scratch.dat",  # read-only /tmp -> pre-existing input, KEPT
            "/tmp/output.txt",  # also written below -> intermediate, dropped
            "/tmp/torchinductor_user/cached.so",
            "/some/repo/.git/HEAD",
            "/home/.roar/runtime.json",
        ]
        writes = ["/dev/null", "/tmp/output.txt"]
        tracer, py = _make_data(reads, writes)
        result: FilteredFiles = FileFilterService().filter_files(tracer, py, {})

        assert result.counts.system_reads == 2  # /sys, /etc
        # /tmp/output.txt is dropped from both reads (intermediate) and writes;
        # /tmp/scratch.dat is read-only and survives as a pre-existing input.
        assert result.counts.tmp_files == 2  # output.txt read + output.txt write
        assert "/tmp/scratch.dat" in result.read_files
        assert result.counts.torch_cache == 1
        assert result.counts.git_metadata == 1
        assert result.counts.roar_internal == 1
        assert result.counts.write_noise == 1  # /dev/null

    def test_credential_files_always_filtered(self) -> None:
        # .netrc / _netrc hold login tokens — never reproducibility-relevant,
        # so they are dropped from reads and writes regardless of config.
        reads = ["/home/user/.netrc", "/home/user/project/data.parquet"]
        writes = ["/home/user/_netrc"]
        tracer, py = _make_data(reads, writes)
        result = FileFilterService().filter_files(tracer, py, {})

        assert result.counts.credential_files == 2  # .netrc read + _netrc write
        assert result.read_files == ["/home/user/project/data.parquet"]
        assert result.written_files == []

    def test_dropped_paths_empty_by_default(self) -> None:
        tracer, py = _make_data(["/sys/foo"], [])
        result = FileFilterService().filter_files(tracer, py, {})
        assert result.dropped_paths == {}

    def test_dropped_paths_collected_when_requested(self) -> None:
        tracer, py = _make_data(["/sys/foo", "/etc/bar"], ["/dev/null"])
        result = FileFilterService().filter_files(tracer, py, {}, collect_dropped_paths=True)
        assert sorted(result.dropped_paths["system_reads"]) == ["/etc/bar", "/sys/foo"]
        assert result.dropped_paths["write_noise"] == ["/dev/null"]
        # Empty categories present as empty lists, not missing.
        assert result.dropped_paths.get("torch_cache") == []


# ---------------------------------------------------------------------------
# RunReportPresenter: rendering at each verbosity
# ---------------------------------------------------------------------------


def _no_pipe_caps() -> TerminalCaps:
    """TerminalCaps that pretend stderr is an interactive TTY."""
    return TerminalCaps(is_tty=True, can_color=False, can_emoji=False, width=120)


def _pipe_caps() -> TerminalCaps:
    return TerminalCaps(is_tty=False, can_color=False, can_emoji=False, width=80)


def _make_result(
    *,
    inputs: list[dict] | None = None,
    outputs: list[dict] | None = None,
    filter_counts: dict[str, int] | None = None,
    dropped_paths: dict[str, list[str]] | None = None,
) -> RunResult:
    return RunResult(
        exit_code=0,
        job_id=1,
        job_uid="abcdef0123",
        duration=0.5,
        inputs=inputs or [],
        outputs=outputs or [],
        post_duration=0.1,
        filter_counts=filter_counts or {},
        dropped_paths=dropped_paths or {},
    )


class TestPresenterQuiet:
    def test_quiet_suppresses_summary(self) -> None:
        out = io.StringIO()
        p = RunReportPresenter(stream=out, caps=_no_pipe_caps(), verbosity="quiet")
        p.summary(_make_result(filter_counts={"package_reads": 5}), ["python"])
        p.done(exit_code=0, trace_duration=0.5, post_duration=0.1)
        assert out.getvalue() == ""

    def test_legacy_quiet_kwarg_still_works(self) -> None:
        """Old call sites passing quiet=True keep their silent behavior."""
        out = io.StringIO()
        p = RunReportPresenter(stream=out, caps=_no_pipe_caps(), quiet=True)
        p.summary(_make_result(filter_counts={"package_reads": 5}), ["python"])
        assert out.getvalue() == ""


class TestPresenterNormal:
    def test_normal_renders_filter_count_line(self) -> None:
        out = io.StringIO()
        p = RunReportPresenter(stream=out, caps=_no_pipe_caps(), verbosity="normal")
        p.summary(
            _make_result(filter_counts={"system_reads": 18, "package_reads": 1244, "tmp_files": 4}),
            ["python"],
        )
        text = out.getvalue()
        assert "ignored i/o" in text
        assert "18 system" in text
        assert "1244 packages" in text
        assert "4 /tmp" in text

    def test_normal_skips_filter_line_when_no_drops(self) -> None:
        out = io.StringIO()
        p = RunReportPresenter(stream=out, caps=_no_pipe_caps(), verbosity="normal")
        p.summary(_make_result(), ["python"])
        # No `ignored i/o` row should appear when nothing was filtered (or
        # the counts dict is empty).
        assert "ignored i/o" not in out.getvalue()

    def test_normal_does_not_list_files(self) -> None:
        out = io.StringIO()
        p = RunReportPresenter(stream=out, caps=_no_pipe_caps(), verbosity="normal")
        p.summary(
            _make_result(
                inputs=[{"path": "/a/in.csv"}],
                outputs=[{"path": "/a/out.csv"}],
            ),
            ["python"],
        )
        text = out.getvalue()
        assert "/a/in.csv" not in text
        assert "/a/out.csv" not in text


class TestPresenterVerbose:
    def test_verbose_lists_inputs_and_outputs(self) -> None:
        out = io.StringIO()
        p = RunReportPresenter(stream=out, caps=_no_pipe_caps(), verbosity="verbose")
        p.summary(
            _make_result(
                inputs=[{"path": "/a/in.csv"}, {"path": "/a/in2.csv"}],
                outputs=[{"path": "/a/out.csv"}],
            ),
            ["python"],
        )
        text = out.getvalue()
        assert "/a/in.csv" in text
        assert "/a/in2.csv" in text
        assert "/a/out.csv" in text

    def test_verbose_does_not_list_filtered(self) -> None:
        out = io.StringIO()
        p = RunReportPresenter(stream=out, caps=_no_pipe_caps(), verbosity="verbose")
        p.summary(
            _make_result(
                filter_counts={"package_reads": 2},
                dropped_paths={"package_reads": ["/pkg/a", "/pkg/b"]},
            ),
            ["python"],
        )
        text = out.getvalue()
        assert "2 packages" in text  # count line still shows up
        assert "/pkg/a" not in text
        assert "/pkg/b" not in text


class TestPresenterDebug:
    def test_debug_lists_filtered_paths_per_category(self) -> None:
        out = io.StringIO()
        p = RunReportPresenter(stream=out, caps=_no_pipe_caps(), verbosity="debug")
        p.summary(
            _make_result(
                filter_counts={"package_reads": 2, "system_reads": 1},
                dropped_paths={
                    "package_reads": ["/pkg/a", "/pkg/b"],
                    "system_reads": ["/sys/x"],
                },
            ),
            ["python"],
        )
        text = out.getvalue()
        assert "/pkg/a" in text
        assert "/pkg/b" in text
        assert "/sys/x" in text
        assert "(filtered)" in text

    def test_debug_handles_empty_dropped_paths(self) -> None:
        out = io.StringIO()
        p = RunReportPresenter(stream=out, caps=_no_pipe_caps(), verbosity="debug")
        p.summary(_make_result(filter_counts={"package_reads": 2}), ["python"])
        # Counts present, no per-category list — debug just skips listing.
        assert "2 packages" in out.getvalue()


class TestPresenterPipeMode:
    def test_pipe_mode_normal_skips_summary(self) -> None:
        out = io.StringIO()
        caps = _pipe_caps()
        p = RunReportPresenter(stream=out, caps=caps, verbosity="normal")
        p.summary(_make_result(filter_counts={"package_reads": 5}), ["python"])
        assert out.getvalue() == ""

    def test_pipe_mode_verbose_renders_anyway(self) -> None:
        """CI logs and redirected output are exactly where verbose listings
        matter most. Pipe mode should not suppress them."""
        out = io.StringIO()
        caps = _pipe_caps()
        p = RunReportPresenter(stream=out, caps=caps, verbosity="verbose")
        p.summary(
            _make_result(inputs=[{"path": "/a/in.csv"}]),
            ["python"],
        )
        assert "/a/in.csv" in out.getvalue()
