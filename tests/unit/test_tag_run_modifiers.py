"""Unit tests for run-modifier record & replay.

`roar run --block-tag/--add-tag` are recorded on the job metadata as
``run_modifiers`` so `roar reproduce` can replay them — otherwise the
reproduced tag/barrier layer diverges from the original.
"""

from __future__ import annotations

import json

from roar.application.tags import build_run_modifiers, run_modifier_flags
from roar.db.services.job_recording import JobRecordingService
from roar.execution.reproduction.pipeline_executor import PipelineExecutor


class TestBuildRunModifiers:
    def test_both_flag_kinds(self) -> None:
        assert build_run_modifiers(["contains_pii", "license=GPL-3.0"], ["license=Apache-2.0"]) == {
            "block_tags": ["contains_pii", "license=GPL-3.0"],
            "add_tags": ["license=Apache-2.0"],
        }

    def test_only_block(self) -> None:
        assert build_run_modifiers(["contains_pii"], []) == {"block_tags": ["contains_pii"]}

    def test_only_add(self) -> None:
        assert build_run_modifiers([], ["license=MIT"]) == {"add_tags": ["license=MIT"]}

    def test_empty_is_none(self) -> None:
        assert build_run_modifiers([], []) is None
        assert build_run_modifiers(["", "  "], [""]) is None

    def test_wandb_to_trackio(self) -> None:
        assert build_run_modifiers([], [], wandb_to_trackio=True) == {"wandb_to_trackio": True}
        assert build_run_modifiers(["contains_pii"], [], wandb_to_trackio=True) == {
            "block_tags": ["contains_pii"],
            "wandb_to_trackio": True,
        }
        assert build_run_modifiers([], [], wandb_to_trackio=False) is None


class TestRunModifierFlags:
    def test_renders_flags(self) -> None:
        flags = run_modifier_flags(
            {"block_tags": ["contains_pii", "license=GPL-3.0"], "add_tags": ["license=Apache-2.0"]}
        )
        assert flags == (
            "--block-tag contains_pii --block-tag license=GPL-3.0 --add-tag license=Apache-2.0"
        )

    def test_shell_quotes_values_with_spaces(self) -> None:
        assert run_modifier_flags({"add_tags": ["note=has space"]}) == "--add-tag 'note=has space'"

    def test_none_or_non_dict_is_empty(self) -> None:
        assert run_modifier_flags(None) == ""
        assert run_modifier_flags("nope") == ""
        assert run_modifier_flags({}) == ""

    def test_roundtrip(self) -> None:
        mods = build_run_modifiers(["contains_pii"], ["license=MIT"])
        assert run_modifier_flags(mods) == "--block-tag contains_pii --add-tag license=MIT"

    def test_wandb_to_trackio_flag(self) -> None:
        assert run_modifier_flags({"wandb_to_trackio": True}) == "--wandb-to-trackio"
        assert (
            run_modifier_flags({"add_tags": ["license=MIT"], "wandb_to_trackio": True})
            == "--add-tag license=MIT --wandb-to-trackio"
        )


class TestWithRunModifiersInjection:
    def test_injects_and_preserves_existing_metadata(self) -> None:
        base = json.dumps({"roar_version": "0.4.0", "runtime": {"command": ["python3", "s.py"]}})
        out = JobRecordingService._with_run_modifiers(base, ("contains_pii",), ("license=MIT",))
        parsed = json.loads(out)
        assert parsed["roar_version"] == "0.4.0"  # untouched
        assert parsed["runtime"] == {"command": ["python3", "s.py"]}  # untouched
        assert parsed["run_modifiers"] == {
            "block_tags": ["contains_pii"],
            "add_tags": ["license=MIT"],
        }

    def test_no_flags_returns_metadata_unchanged(self) -> None:
        base = json.dumps({"roar_version": "0.4.0"})
        assert JobRecordingService._with_run_modifiers(base, (), ()) == base

    def test_none_metadata_still_records_modifiers(self) -> None:
        out = JobRecordingService._with_run_modifiers(None, ("contains_pii",), ())
        assert json.loads(out) == {"run_modifiers": {"block_tags": ["contains_pii"]}}

    def test_malformed_metadata_is_not_fatal(self) -> None:
        out = JobRecordingService._with_run_modifiers("not json", ("contains_pii",), ())
        assert json.loads(out) == {"run_modifiers": {"block_tags": ["contains_pii"]}}

    def test_wandb_to_trackio_recorded(self) -> None:
        out = JobRecordingService._with_run_modifiers(None, (), (), wandb_to_trackio=True)
        assert json.loads(out) == {"run_modifiers": {"wandb_to_trackio": True}}


class TestWrapWithRoarReplaysModifiers:
    def test_modifiers_inserted_between_run_and_command(self) -> None:
        ex = PipelineExecutor(roar_executable="roar")
        wrapped = ex._wrap_with_roar(
            "python3 redact.py", "run", None, modifiers="--block-tag contains_pii"
        )
        assert wrapped == "roar run --block-tag contains_pii python3 redact.py"

    def test_no_modifiers_is_unchanged(self) -> None:
        ex = PipelineExecutor(roar_executable="roar")
        assert ex._wrap_with_roar("python3 x.py", "run", None) == "roar run python3 x.py"

    def test_wandb_to_trackio_reemitted(self) -> None:
        # The reproduce path: a run captured with --wandb-to-trackio re-emits the
        # flag so the reproduction's `import wandb` resolves the same way.
        ex = PipelineExecutor(roar_executable="roar")
        wrapped = ex._wrap_with_roar(
            "python3 train.py",
            "run",
            None,
            modifiers=run_modifier_flags({"wandb_to_trackio": True}),
        )
        assert wrapped == "roar run --wandb-to-trackio python3 train.py"
