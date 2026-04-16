from unittest.mock import patch

from roar.application.lookup.models import RefKind
from roar.application.lookup.policy import (
    ArtifactRemoteLookupOperation,
    remote_artifact_fallback_enabled,
)
from roar.application.lookup.refs import parse_ref


def test_parse_ref_uses_explicit_artifact_selector_for_short_hex() -> None:
    parsed = parse_ref("deadbeef", selector="artifact")

    assert parsed.kind == RefKind.ARTIFACT_HASH
    assert parsed.is_artifact_lookup_candidate is True


def test_remote_artifact_fallback_for_show_requires_config_flag() -> None:
    parsed = parse_ref("a" * 64)

    with patch("roar.application.lookup.policy.config_get", return_value=False):
        assert remote_artifact_fallback_enabled(ArtifactRemoteLookupOperation.SHOW, parsed) is False

    with patch("roar.application.lookup.policy.config_get", return_value=True):
        assert remote_artifact_fallback_enabled(ArtifactRemoteLookupOperation.SHOW, parsed) is True


def test_remote_artifact_fallback_for_diff_requires_config_flag() -> None:
    parsed = parse_ref("a" * 64)

    with patch("roar.application.lookup.policy.config_get", return_value=False):
        assert remote_artifact_fallback_enabled(ArtifactRemoteLookupOperation.DIFF, parsed) is False

    with patch("roar.application.lookup.policy.config_get", return_value=True):
        assert remote_artifact_fallback_enabled(ArtifactRemoteLookupOperation.DIFF, parsed) is True


def test_remote_artifact_fallback_for_reproduce_is_not_config_gated() -> None:
    parsed = parse_ref("a" * 64)

    with patch("roar.application.lookup.policy.config_get", return_value=False):
        assert (
            remote_artifact_fallback_enabled(ArtifactRemoteLookupOperation.REPRODUCE, parsed)
            is True
        )
