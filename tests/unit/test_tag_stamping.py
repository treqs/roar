"""Unit tests for explicit tag stamping (`roar run --add-tag`) and its parsing helpers."""

from __future__ import annotations

from typing import Any

import pytest

from roar.application.tags import parse_add_tags, parse_tag_kv, stamp_tags
from roar.core.label_origins import LABEL_ORIGIN_USER


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


class TestParseTagKv:
    def test_parses_kind_and_value(self) -> None:
        assert parse_tag_kv("license=MIT") == ("license", "MIT")

    def test_strips_whitespace(self) -> None:
        assert parse_tag_kv(" license = MIT ") == ("license", "MIT")

    def test_raises_when_no_equals(self) -> None:
        with pytest.raises(ValueError, match="Expected KIND=VALUE"):
            parse_tag_kv("license")

    def test_raises_when_kind_empty(self) -> None:
        with pytest.raises(ValueError, match="Kind cannot be empty"):
            parse_tag_kv("=MIT")

    def test_raises_when_value_empty(self) -> None:
        with pytest.raises(ValueError, match="Value cannot be empty"):
            parse_tag_kv("license=")


class TestParseAddTags:
    def test_groups_single_pair(self) -> None:
        assert parse_add_tags(["license=MIT"]) == {"license": ["MIT"]}

    def test_groups_multiple_values_for_same_kind(self) -> None:
        result = parse_add_tags(["license=MIT", "license=Apache-2.0"])
        assert result == {"license": ["MIT", "Apache-2.0"]}

    def test_dedupes_identical_values(self) -> None:
        result = parse_add_tags(["license=MIT", "license=MIT"])
        assert result == {"license": ["MIT"]}

    def test_groups_independent_kinds(self) -> None:
        result = parse_add_tags(["license=MIT", "jurisdiction=EU"])
        assert result == {"license": ["MIT"], "jurisdiction": ["EU"]}

    def test_empty_input_yields_empty_dict(self) -> None:
        assert parse_add_tags([]) == {}

    def test_raises_on_malformed_pair(self) -> None:
        with pytest.raises(ValueError, match="Expected KIND=VALUE"):
            parse_add_tags(["license=MIT", "badpair"])


class TestStampTags:
    def test_stamps_new_kind_onto_output(self) -> None:
        repo = FakeLabelRepo()
        stamp_tags(repo, output_artifact_ids=["out1"], tags={"license": ["MIT"]})
        assert repo.docs["out1"]["tag"] == {"license": ["MIT"]}

    def test_unions_with_existing_tags(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("out1", {"tag": {"license": ["MIT"]}})
        stamp_tags(repo, output_artifact_ids=["out1"], tags={"license": ["Apache-2.0"]})
        assert set(repo.docs["out1"]["tag"]["license"]) == {"MIT", "Apache-2.0"}

    def test_preserves_non_tag_metadata(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("out1", {"owner": "ml-team"})
        stamp_tags(repo, output_artifact_ids=["out1"], tags={"license": ["MIT"]})
        assert repo.docs["out1"]["owner"] == "ml-team"
        assert repo.docs["out1"]["tag"]["license"] == ["MIT"]

    def test_stamps_all_outputs(self) -> None:
        repo = FakeLabelRepo()
        stamp_tags(repo, output_artifact_ids=["out1", "out2"], tags={"license": ["MIT"]})
        assert repo.docs["out1"]["tag"]["license"] == ["MIT"]
        assert repo.docs["out2"]["tag"]["license"] == ["MIT"]

    def test_uses_user_write_origin(self) -> None:
        repo = FakeLabelRepo()
        stamp_tags(repo, output_artifact_ids=["out1"], tags={"license": ["MIT"]})
        assert repo.write_calls == [("out1", LABEL_ORIGIN_USER)]

    def test_noop_when_value_already_present(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("out1", {"tag": {"license": ["MIT"]}})
        stamp_tags(repo, output_artifact_ids=["out1"], tags={"license": ["MIT"]})
        assert repo.write_calls == []

    def test_empty_tags_is_a_noop(self) -> None:
        repo = FakeLabelRepo()
        stamp_tags(repo, output_artifact_ids=["out1"], tags={})
        assert repo.write_calls == []

    def test_empty_outputs_is_a_noop(self) -> None:
        repo = FakeLabelRepo()
        stamp_tags(repo, output_artifact_ids=[], tags={"license": ["MIT"]})
        assert repo.write_calls == []
