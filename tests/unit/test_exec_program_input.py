"""Tests for recording the exec'd program (argv[0]) as a run input.

`roar run ./script.sh` (or a compiled binary) may never trigger an
open()/read() the tracer records — the kernel execs it — so the program
escapes input capture and nothing flags it when it's a loose, uncommitted
file. `ProvenanceService._resolve_exec_program` resolves the user's argv[0]
so it can be fed through the normal input path; the existing system/package
filters then drop bare interpreters (`python`, `bash`) while a program the
user pointed at outside those trees survives.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from roar.execution.provenance.service import ProvenanceService


def _svc() -> ProvenanceService:
    return ProvenanceService()


def test_absolute_program_returned_as_is(tmp_path: Path) -> None:
    prog = tmp_path / "mybin"
    prog.write_text("#!/bin/sh\n")
    assert _svc()._resolve_exec_program([str(prog), "arg"]) == str(prog)


def test_relative_program_resolved_against_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prog = tmp_path / "script.sh"
    prog.write_text("#!/bin/sh\n")
    monkeypatch.chdir(tmp_path)
    # roar never chdirs for a run, so the program resolves against the cwd.
    expected = os.path.join(os.getcwd(), "script.sh")
    assert _svc()._resolve_exec_program(["./script.sh"]) == expected


def test_bare_command_resolves_via_path() -> None:
    """A bare interpreter/tool name resolves via PATH (lands under /usr or a
    venv, where the system/package filters drop it — no special-casing here)."""
    sh = shutil.which("sh")
    assert sh is not None  # present on every POSIX CI runner
    assert _svc()._resolve_exec_program(["sh", "-c", "true"]) == sh


def test_missing_or_empty_command_returns_none(tmp_path: Path) -> None:
    svc = _svc()
    assert svc._resolve_exec_program(None) is None
    assert svc._resolve_exec_program([]) is None
    assert svc._resolve_exec_program([""]) is None
    # A path that doesn't resolve to an existing file is dropped.
    assert svc._resolve_exec_program([str(tmp_path / "nope")]) is None


def test_injected_program_flows_into_raw_reads(tmp_path: Path) -> None:
    """The resolved program is appended to the raw reads (deduped) so it goes
    through the same filtering + sourced/unsourced classification as any input.
    """
    from roar.core.models.provenance import TracerData

    prog = tmp_path / "mybin"
    prog.write_text("binary\n")
    tracer = TracerData(read_files=["/home/u/proj/data.csv"])

    # Mirror the collect() injection step.
    program_path = _svc()._resolve_exec_program([str(prog)])
    assert program_path == str(prog)
    if program_path and program_path not in set(tracer.read_files):
        tracer.read_files.append(program_path)

    assert str(prog) in tracer.read_files
    # Idempotent: a program already captured by the tracer isn't duplicated.
    already = TracerData(read_files=[str(prog)])
    if program_path and program_path not in set(already.read_files):
        already.read_files.append(program_path)
    assert already.read_files.count(str(prog)) == 1
