"""Unit tests for propagate_tags — hereditary tag inheritance at job-record time.

Uses a small in-memory fake label repo (rather than mocks) since propagation
reads and writes several artifacts across a single call and the interactions
between those reads/writes are exactly what's under test.

Values are stored as provenance records (`{value, origin, job}`), and
propagation is scope-gated: a candidate value only joins the union if it was
produced by a job in the *current* session, or is covered by a bind on the
input artifact. Most tests below use a single shared session (SESSION and
JOB, wired through `resolve_job_session_id`) so the scope gate trivially
passes and the original union/preservation/no-op mechanics are what's under
test; `TestScopeGating` exercises the gate itself.
"""

from __future__ import annotations

from typing import Any

from roar.application.tags import parse_block_tags, propagate_tags
from roar.core.label_origins import LABEL_ORIGIN_SYSTEM, LABEL_ORIGIN_USER

SESSION = 1
JOB = "job-a"


def _resolve_same_session(_job: str) -> int | None:
    return SESSION


def _tag(**kinds: list[str]) -> dict[str, Any]:
    """Build a `{"tag": {...}}` doc — every value system-origin, from JOB (in SESSION)."""
    return {
        "tag": {
            kind: {
                "values": [
                    {"value": value, "origin": LABEL_ORIGIN_SYSTEM, "job": JOB} for value in values
                ]
            }
            for kind, values in kinds.items()
        }
    }


def _values(doc: dict[str, Any], kind: str) -> list[str]:
    return [record["value"] for record in doc["tag"][kind]["values"]]


class FakeLabelRepo:
    """In-memory stand-in for SQLAlchemyLabelRepository's artifact-label surface."""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.versions: dict[str, int] = {}
        self.write_calls: list[tuple[str, str | None]] = []

    def seed(self, artifact_id: str, metadata: dict[str, Any]) -> None:
        self.docs[artifact_id] = metadata
        self.versions[artifact_id] = 1

    def seed_bind(self, artifact_id: str, *, action: str, covers: dict[str, list[str]]) -> None:
        doc = self.docs.setdefault(artifact_id, {})
        tag = dict(doc.get("tag", {}))
        bind_doc = dict(tag.get("bind") or {"events": []})
        events = [*bind_doc.get("events", []), {"action": action, "covers": covers}]
        tag["bind"] = {"events": events}
        doc["tag"] = tag
        self.versions.setdefault(artifact_id, 1)

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


def _propagate(
    repo: FakeLabelRepo,
    *,
    input_artifact_ids: list[str],
    output_artifact_ids: list[str],
    current_session_id: int | None = SESSION,
    resolve_job_session_id: Any = _resolve_same_session,
    job_uid: str | None = JOB,
    blocked_kinds: frozenset[str] = frozenset(),
    blocked_values: dict[str, frozenset[str]] | None = None,
) -> None:
    propagate_tags(
        repo,
        input_artifact_ids=input_artifact_ids,
        output_artifact_ids=output_artifact_ids,
        current_session_id=current_session_id,
        resolve_job_session_id=resolve_job_session_id,
        job_uid=job_uid,
        blocked_kinds=blocked_kinds,
        blocked_values=blocked_values,
    )


class TestBasicPropagation:
    def test_single_input_tags_single_output(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", _tag(license=["MIT"]))
        _propagate(repo, input_artifact_ids=["in1"], output_artifact_ids=["out1"])
        assert _values(repo.docs["out1"], "license") == ["MIT"]

    def test_unions_values_from_multiple_inputs(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", _tag(license=["MIT"]))
        repo.seed("in2", _tag(license=["Apache-2.0"]))
        _propagate(repo, input_artifact_ids=["in1", "in2"], output_artifact_ids=["out1"])
        assert set(_values(repo.docs["out1"], "license")) == {"MIT", "Apache-2.0"}

    def test_dedupes_identical_values_across_inputs(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", _tag(license=["MIT"]))
        repo.seed("in2", _tag(license=["MIT"]))
        _propagate(repo, input_artifact_ids=["in1", "in2"], output_artifact_ids=["out1"])
        assert _values(repo.docs["out1"], "license") == ["MIT"]

    def test_propagates_to_all_outputs(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", _tag(license=["MIT"]))
        _propagate(repo, input_artifact_ids=["in1"], output_artifact_ids=["out1", "out2"])
        assert _values(repo.docs["out1"], "license") == ["MIT"]
        assert _values(repo.docs["out2"], "license") == ["MIT"]

    def test_merges_multiple_kinds_independently(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", _tag(license=["MIT"], jurisdiction=["EU"]))
        _propagate(repo, input_artifact_ids=["in1"], output_artifact_ids=["out1"])
        assert _values(repo.docs["out1"], "license") == ["MIT"]
        assert _values(repo.docs["out1"], "jurisdiction") == ["EU"]


class TestExistingOutputTags:
    def test_preserves_and_unions_with_existing_output_tags(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", _tag(license=["Apache-2.0"]))
        repo.seed(
            "out1",
            {"tag": {"license": {"values": [{"value": "MIT", "origin": LABEL_ORIGIN_USER}]}}},
        )
        _propagate(repo, input_artifact_ids=["in1"], output_artifact_ids=["out1"])
        assert set(_values(repo.docs["out1"], "license")) == {"MIT", "Apache-2.0"}

    def test_preserves_non_tag_metadata_on_output(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", _tag(license=["MIT"]))
        repo.seed("out1", {"owner": "ml-team"})
        _propagate(repo, input_artifact_ids=["in1"], output_artifact_ids=["out1"])
        assert repo.docs["out1"]["owner"] == "ml-team"
        assert _values(repo.docs["out1"], "license") == ["MIT"]

    def test_preserves_output_kind_not_present_on_any_input(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", _tag(license=["MIT"]))
        repo.seed("out1", _tag(jurisdiction=["US"]))
        _propagate(repo, input_artifact_ids=["in1"], output_artifact_ids=["out1"])
        assert _values(repo.docs["out1"], "jurisdiction") == ["US"]
        assert _values(repo.docs["out1"], "license") == ["MIT"]


class TestBlockedKinds:
    def test_blocked_kind_is_not_propagated(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", _tag(license=["GPL-3.0"], jurisdiction=["EU"]))
        _propagate(
            repo,
            input_artifact_ids=["in1"],
            output_artifact_ids=["out1"],
            blocked_kinds=frozenset({"license"}),
        )
        assert "license" not in repo.docs["out1"]["tag"]
        assert _values(repo.docs["out1"], "jurisdiction") == ["EU"]

    def test_all_kinds_blocked_is_a_full_noop(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", _tag(license=["GPL-3.0"]))
        _propagate(
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
        _propagate(repo, input_artifact_ids=[], output_artifact_ids=["out1"])
        assert repo.write_calls == []

    def test_no_outputs_is_a_noop(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", _tag(license=["MIT"]))
        _propagate(repo, input_artifact_ids=["in1"], output_artifact_ids=[])
        assert repo.write_calls == []

    def test_inputs_with_no_tags_is_a_noop(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", {"owner": "ml-team"})
        _propagate(repo, input_artifact_ids=["in1"], output_artifact_ids=["out1"])
        assert repo.write_calls == []

    def test_output_already_has_all_inherited_values_is_a_noop(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", _tag(license=["MIT"]))
        repo.seed("out1", _tag(license=["MIT"]))
        _propagate(repo, input_artifact_ids=["in1"], output_artifact_ids=["out1"])
        assert repo.write_calls == []

    def test_input_with_no_label_document_at_all_is_handled(self) -> None:
        repo = FakeLabelRepo()
        _propagate(repo, input_artifact_ids=["in1"], output_artifact_ids=["out1"])
        assert repo.write_calls == []


class TestWriteOrigin:
    def test_stamps_system_write_origin(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", _tag(license=["MIT"]))
        _propagate(repo, input_artifact_ids=["in1"], output_artifact_ids=["out1"])
        assert repo.write_calls == [("out1", LABEL_ORIGIN_SYSTEM)]

    def test_only_writes_outputs_that_actually_changed(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", _tag(license=["MIT"]))
        repo.seed("out1", _tag(license=["MIT"]))  # already up to date
        _propagate(repo, input_artifact_ids=["in1"], output_artifact_ids=["out1", "out2"])
        assert repo.write_calls == [("out2", LABEL_ORIGIN_SYSTEM)]

    def test_newly_propagated_records_carry_the_producing_job(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", _tag(license=["MIT"]))
        _propagate(repo, input_artifact_ids=["in1"], output_artifact_ids=["out1"], job_uid="job-b")
        record = repo.docs["out1"]["tag"]["license"]["values"][0]
        assert record == {"value": "MIT", "origin": LABEL_ORIGIN_SYSTEM, "job": "job-b"}


class TestDuplicateIds:
    def test_duplicate_input_ids_are_deduped(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", _tag(license=["MIT"]))
        _propagate(repo, input_artifact_ids=["in1", "in1"], output_artifact_ids=["out1"])
        assert _values(repo.docs["out1"], "license") == ["MIT"]

    def test_duplicate_output_ids_write_once(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", _tag(license=["MIT"]))
        _propagate(repo, input_artifact_ids=["in1"], output_artifact_ids=["out1", "out1"])
        assert len(repo.write_calls) == 1


class TestScopeGating:
    """The core draft4 change: propagation only crosses a session boundary via a bind."""

    def test_same_session_value_propagates_without_bind(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", _tag(license=["MIT"]))  # job JOB, resolves to SESSION
        _propagate(repo, input_artifact_ids=["in1"], output_artifact_ids=["out1"])
        assert _values(repo.docs["out1"], "license") == ["MIT"]

    def test_cross_session_value_without_bind_is_excluded(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", _tag(license=["MIT"]))  # produced in SESSION
        _propagate(
            repo,
            input_artifact_ids=["in1"],
            output_artifact_ids=["out1"],
            current_session_id=SESSION + 1,  # a different session reads it
        )
        assert repo.write_calls == []
        assert "out1" not in repo.docs

    def test_cross_session_value_with_bind_propagates(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", _tag(license=["MIT"]))
        repo.seed_bind("in1", action="bind", covers={"license": ["MIT"]})
        _propagate(
            repo,
            input_artifact_ids=["in1"],
            output_artifact_ids=["out1"],
            current_session_id=SESSION + 1,
        )
        assert _values(repo.docs["out1"], "license") == ["MIT"]

    def test_unbind_revokes_cross_session_propagation(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", _tag(license=["MIT"]))
        repo.seed_bind("in1", action="bind", covers={"license": ["MIT"]})
        repo.seed_bind("in1", action="unbind", covers={"license": ["MIT"]})
        _propagate(
            repo,
            input_artifact_ids=["in1"],
            output_artifact_ids=["out1"],
            current_session_id=SESSION + 1,
        )
        assert repo.write_calls == []
        assert "out1" not in repo.docs

    def test_bind_only_covers_the_exact_value_it_named(self) -> None:
        repo = FakeLabelRepo()
        repo.seed(
            "in1",
            {
                "tag": {
                    "license": {
                        "values": [
                            {"value": "MIT", "origin": LABEL_ORIGIN_SYSTEM, "job": JOB},
                            {"value": "GPL-3.0", "origin": LABEL_ORIGIN_SYSTEM, "job": JOB},
                        ]
                    }
                }
            },
        )
        repo.seed_bind("in1", action="bind", covers={"license": ["MIT"]})
        _propagate(
            repo,
            input_artifact_ids=["in1"],
            output_artifact_ids=["out1"],
            current_session_id=SESSION + 1,
        )
        assert _values(repo.docs["out1"], "license") == ["MIT"]

    def test_user_origin_value_with_no_job_needs_a_bind_even_within_session(self) -> None:
        """A record with no `job` can't be scope-matched by session; it needs a bind.

        In practice `TagService.add()` always pairs a user-origin write with an
        implicit bind, so this only matters as a defensive characterization of
        propagate_tags's contract in isolation.
        """
        repo = FakeLabelRepo()
        repo.seed(
            "in1", {"tag": {"license": {"values": [{"value": "MIT", "origin": LABEL_ORIGIN_USER}]}}}
        )
        _propagate(repo, input_artifact_ids=["in1"], output_artifact_ids=["out1"])
        assert repo.write_calls == []
        assert "out1" not in repo.docs

    def test_resolve_job_session_id_is_memoized_per_call(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", _tag(license=["MIT"]))
        repo.seed("in2", _tag(jurisdiction=["EU"]))
        calls: list[str] = []

        def _tracking_resolver(job: str) -> int | None:
            calls.append(job)
            return SESSION

        _propagate(
            repo,
            input_artifact_ids=["in1", "in2"],
            output_artifact_ids=["out1"],
            resolve_job_session_id=_tracking_resolver,
        )
        assert calls == [JOB]  # both inputs share the same job -> resolved once


class TestValueBarriers:
    """`--block-tag KIND=VALUE` filters a single value; whole-kind still drops all."""

    def test_value_barrier_filters_one_value_keeps_others(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", _tag(license=["MIT", "GPL-3.0"]))
        _propagate(
            repo,
            input_artifact_ids=["in1"],
            output_artifact_ids=["out1"],
            blocked_values={"license": frozenset({"GPL-3.0"})},
        )
        assert _values(repo.docs["out1"], "license") == ["MIT"]

    def test_value_barrier_dropping_all_values_writes_nothing(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", _tag(license=["GPL-3.0"]))
        _propagate(
            repo,
            input_artifact_ids=["in1"],
            output_artifact_ids=["out1"],
            blocked_values={"license": frozenset({"GPL-3.0"})},
        )
        assert "out1" not in repo.docs  # nothing inherited -> no version created

    def test_value_barrier_leaves_other_kinds_untouched(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", _tag(license=["GPL-3.0"], contains_pii=["present"]))
        _propagate(
            repo,
            input_artifact_ids=["in1"],
            output_artifact_ids=["out1"],
            blocked_values={"license": frozenset({"GPL-3.0"})},
        )
        assert "license" not in repo.docs["out1"].get("tag", {})
        assert _values(repo.docs["out1"], "contains_pii") == ["present"]

    def test_whole_kind_block_still_drops_every_value(self) -> None:
        repo = FakeLabelRepo()
        repo.seed("in1", _tag(license=["MIT", "GPL-3.0"]))
        _propagate(
            repo,
            input_artifact_ids=["in1"],
            output_artifact_ids=["out1"],
            blocked_kinds=frozenset({"license"}),
        )
        assert "out1" not in repo.docs


class TestParseBlockTags:
    def test_whole_kind(self) -> None:
        kinds, values = parse_block_tags(["contains_pii"])
        assert kinds == frozenset({"contains_pii"})
        assert values == {}

    def test_value_level(self) -> None:
        kinds, values = parse_block_tags(["license=GPL-3.0"])
        assert kinds == frozenset()
        assert values == {"license": frozenset({"GPL-3.0"})}

    def test_multiple_values_same_kind_accumulate(self) -> None:
        _kinds, values = parse_block_tags(["license=GPL-3.0", "license=AGPL-3.0"])
        assert values == {"license": frozenset({"GPL-3.0", "AGPL-3.0"})}

    def test_whole_kind_wins_over_value_level(self) -> None:
        kinds, values = parse_block_tags(["license", "license=GPL-3.0"])
        assert kinds == frozenset({"license"})
        assert values == {}  # value entry dropped — the whole kind is blocked

    def test_empty_value_is_treated_as_whole_kind(self) -> None:
        kinds, values = parse_block_tags(["license="])
        assert kinds == frozenset({"license"})
        assert values == {}

    def test_blanks_are_ignored(self) -> None:
        assert parse_block_tags(["", "  "]) == (frozenset(), {})
