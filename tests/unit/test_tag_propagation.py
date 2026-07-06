"""Unit tests for propagate_tags — hereditary tag inheritance at job-record time.

Uses a small in-memory fake label repo (rather than mocks) since propagation
reads and writes several artifacts across a single call and the interactions
between those reads/writes are exactly what's under test.
"""

from __future__ import annotations

from typing import Any

from roar.application.tags import propagate_tags
from roar.core.label_origins import LABEL_ORIGIN_SYSTEM


class FakeLabelRepo:
    """In-memory stand-in for SQLAlchemyLabelRepository's artifact-label surface."""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.versions: dict[str, int] = {}
        self.write_calls: list[tuple[str, str | None]] = []

    def seed(self, artifact_id: str, metadata: dict[str, Any]) -> None:
        self.docs[artifact_id] = metadata
        self.versions[artifact_id] = 1

    def get_current(
        self, entity_type: str, *, artifact_id: str | None = None
    ) -> dict[str, Any] | None:
        assert entity_type == "artifact"
        if artifact_id not in self.docs:
            return None
        return {"metadata": self.docs[artifact_id], "version": self.versions[artifact_id]}

    def create_version(
        self,
        entity_type: str,
        metadata: dict[str, Any],
        *,
        artifact_id: str | None = None,
        write_origin: str | None = None,
    ) -> dict[str, Any]:
        assert entity_type == "artifact"
        assert artifact_id is not None
        self.docs[artifact_id] = metadata
        self.versions[artifact_id] = self.versions.get(artifact_id, 0) + 1
        self.write_calls.append((artifact_id, write_origin))
        return {"metadata": metadata, "version": self.versions[artifact_id]}


class TestBasicPropagation:
    def test_single_input_tags_single_output(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", {"tag": {"license": ["MIT"]}})
        propagate_tags(repo, input_artifact_ids=["in1"], output_artifact_ids=["out1"])
        assert repo.docs["out1"]["tag"] == {"license": ["MIT"]}

    def test_unions_values_from_multiple_inputs(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", {"tag": {"license": ["MIT"]}})
        repo.seed("in2", {"tag": {"license": ["Apache-2.0"]}})
        propagate_tags(repo, input_artifact_ids=["in1", "in2"], output_artifact_ids=["out1"])
        assert set(repo.docs["out1"]["tag"]["license"]) == {"MIT", "Apache-2.0"}

    def test_dedupes_identical_values_across_inputs(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", {"tag": {"license": ["MIT"]}})
        repo.seed("in2", {"tag": {"license": ["MIT"]}})
        propagate_tags(repo, input_artifact_ids=["in1", "in2"], output_artifact_ids=["out1"])
        assert repo.docs["out1"]["tag"]["license"] == ["MIT"]

    def test_propagates_to_all_outputs(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", {"tag": {"license": ["MIT"]}})
        propagate_tags(repo, input_artifact_ids=["in1"], output_artifact_ids=["out1", "out2"])
        assert repo.docs["out1"]["tag"]["license"] == ["MIT"]
        assert repo.docs["out2"]["tag"]["license"] == ["MIT"]

    def test_merges_multiple_kinds_independently(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", {"tag": {"license": ["MIT"], "jurisdiction": ["EU"]}})
        propagate_tags(repo, input_artifact_ids=["in1"], output_artifact_ids=["out1"])
        assert repo.docs["out1"]["tag"] == {"license": ["MIT"], "jurisdiction": ["EU"]}

    def test_promotes_legacy_scalar_input_value_to_list(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", {"tag": {"license": "MIT"}})
        propagate_tags(repo, input_artifact_ids=["in1"], output_artifact_ids=["out1"])
        assert repo.docs["out1"]["tag"]["license"] == ["MIT"]


class TestExistingOutputTags:
    def test_preserves_and_unions_with_existing_output_tags(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", {"tag": {"license": ["Apache-2.0"]}})
        repo.seed("out1", {"tag": {"license": ["MIT"]}})
        propagate_tags(repo, input_artifact_ids=["in1"], output_artifact_ids=["out1"])
        assert set(repo.docs["out1"]["tag"]["license"]) == {"MIT", "Apache-2.0"}

    def test_preserves_non_tag_metadata_on_output(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", {"tag": {"license": ["MIT"]}})
        repo.seed("out1", {"owner": "ml-team"})
        propagate_tags(repo, input_artifact_ids=["in1"], output_artifact_ids=["out1"])
        assert repo.docs["out1"]["owner"] == "ml-team"
        assert repo.docs["out1"]["tag"]["license"] == ["MIT"]

    def test_preserves_output_kind_not_present_on_any_input(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", {"tag": {"license": ["MIT"]}})
        repo.seed("out1", {"tag": {"jurisdiction": ["US"]}})
        propagate_tags(repo, input_artifact_ids=["in1"], output_artifact_ids=["out1"])
        assert repo.docs["out1"]["tag"] == {"jurisdiction": ["US"], "license": ["MIT"]}


class TestBlockedKinds:
    def test_blocked_kind_is_not_propagated(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", {"tag": {"license": ["GPL-3.0"], "jurisdiction": ["EU"]}})
        propagate_tags(
            repo,
            input_artifact_ids=["in1"],
            output_artifact_ids=["out1"],
            blocked_kinds=frozenset({"license"}),
        )
        assert repo.docs["out1"]["tag"] == {"jurisdiction": ["EU"]}

    def test_all_kinds_blocked_is_a_full_noop(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", {"tag": {"license": ["GPL-3.0"]}})
        propagate_tags(
            repo,
            input_artifact_ids=["in1"],
            output_artifact_ids=["out1"],
            blocked_kinds=frozenset({"license"}),
        )
        assert "out1" not in repo.docs
        assert repo.write_calls == []


class TestNoOpCases:
    def test_no_inputs_is_a_noop(self) -> None:
        repo = FakeLabelRepo()
        propagate_tags(repo, input_artifact_ids=[], output_artifact_ids=["out1"])
        assert repo.write_calls == []

    def test_no_outputs_is_a_noop(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", {"tag": {"license": ["MIT"]}})
        propagate_tags(repo, input_artifact_ids=["in1"], output_artifact_ids=[])
        assert repo.write_calls == []

    def test_inputs_with_no_tags_is_a_noop(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", {"owner": "ml-team"})
        propagate_tags(repo, input_artifact_ids=["in1"], output_artifact_ids=["out1"])
        assert repo.write_calls == []

    def test_output_already_has_all_inherited_values_is_a_noop(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", {"tag": {"license": ["MIT"]}})
        repo.seed("out1", {"tag": {"license": ["MIT"]}})
        propagate_tags(repo, input_artifact_ids=["in1"], output_artifact_ids=["out1"])
        assert repo.write_calls == []

    def test_input_with_no_label_document_at_all_is_handled(self) -> None:
        repo = FakeLabelRepo()
        propagate_tags(repo, input_artifact_ids=["in1"], output_artifact_ids=["out1"])
        assert repo.write_calls == []


class TestWriteOrigin:
    def test_stamps_system_write_origin(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", {"tag": {"license": ["MIT"]}})
        propagate_tags(repo, input_artifact_ids=["in1"], output_artifact_ids=["out1"])
        assert repo.write_calls == [("out1", LABEL_ORIGIN_SYSTEM)]

    def test_only_writes_outputs_that_actually_changed(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", {"tag": {"license": ["MIT"]}})
        repo.seed("out1", {"tag": {"license": ["MIT"]}})  # already up to date
        propagate_tags(repo, input_artifact_ids=["in1"], output_artifact_ids=["out1", "out2"])
        assert repo.write_calls == [("out2", LABEL_ORIGIN_SYSTEM)]


class TestDuplicateIds:
    def test_duplicate_input_ids_are_deduped(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", {"tag": {"license": ["MIT"]}})
        propagate_tags(repo, input_artifact_ids=["in1", "in1"], output_artifact_ids=["out1"])
        assert repo.docs["out1"]["tag"]["license"] == ["MIT"]

    def test_duplicate_output_ids_write_once(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", {"tag": {"license": ["MIT"]}})
        propagate_tags(repo, input_artifact_ids=["in1"], output_artifact_ids=["out1", "out1"])
        assert len(repo.write_calls) == 1
