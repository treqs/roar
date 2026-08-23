"""The artifact source-type allowlists must agree with each other.

roar keeps two of them: the validator's ``VALID_SOURCE_TYPES``, applied to every
primitive artifact registration, and ``_VALID_REMOTE_SOURCE_TYPES`` in the publish
path, applied when normalising a composite's source type. ``hf`` was added to the
publish-path set (and to the receiver) but not to the validator's, and the two sat
apart for months without anything noticing.

Nothing noticed because the failure is quiet: a staged artifact that fails
validation is *skipped* and its message collected into a warnings list, so
``roar put … hf://`` still uploaded the file and still reported success — it simply
dropped the artifact's source from the registration.

These tests pin the sets together so the next scheme cannot be half-added.
"""

from __future__ import annotations

from roar.application.publish.registration import _VALID_REMOTE_SOURCE_TYPES
from roar.core.validation import VALID_SOURCE_TYPES, validate_artifact_registration


def _artifact(source_type: str | None) -> dict:
    return {
        "hashes": [{"algorithm": "blake3", "digest": "d" * 64}],
        "size": 1,
        "source_type": source_type,
        "session_hash": "a" * 64,
    }


def test_the_two_local_allowlists_agree():
    """The validator must accept every scheme the publish path can emit. A scheme
    the publish path stamps but the validator rejects is silently unregisterable."""
    assert _VALID_REMOTE_SOURCE_TYPES <= {v for v in VALID_SOURCE_TYPES if v is not None}


def test_every_remote_scheme_validates():
    for source_type in sorted(_VALID_REMOTE_SOURCE_TYPES):
        assert validate_artifact_registration(**_artifact(source_type)), (
            f"{source_type!r} is emitted by the publish path but rejected by the validator"
        )


def test_hf_validates():
    """`roar put … hf://` stamps this on the artifact it publishes."""
    assert validate_artifact_registration(**_artifact("hf"))


def test_none_still_validates():
    """Local artifacts carry no source type."""
    assert validate_artifact_registration(**_artifact(None))


def test_an_unknown_scheme_is_still_rejected():
    result = validate_artifact_registration(**_artifact("ftp"))
    assert not result
    assert any("source_type" in e for e in result.errors)


def test_the_rejection_message_lists_what_is_allowed():
    """The message used to hardcode its list and went stale the moment the set
    changed; it is now derived from the set itself."""
    result = validate_artifact_registration(**_artifact("ftp"))
    message = "; ".join(result.errors)
    for allowed in (v for v in VALID_SOURCE_TYPES if v is not None):
        assert repr(allowed) in message
