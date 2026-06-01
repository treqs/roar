"""Tests for ArtifactRegistrationService.register_batch_under_registration_session
backwards-compat fallback when talking to a glaas instance that doesn't have the
staged-artifact endpoint (https://github.com/treqs-inc/glaas-api/pull/50)."""

from unittest.mock import MagicMock

from roar.integrations.glaas.registration.artifact import ArtifactRegistrationService


def _service():
    client = MagicMock()
    logger = MagicMock()
    return ArtifactRegistrationService(client=client, logger=logger), client


def _artifacts(n=2):
    return [
        {
            "hashes": [{"algorithm": "blake3", "digest": f"hash{i:062d}"}],
            "size": 100 + i,
            "source_type": None,
        }
        for i in range(n)
    ]


def test_404_on_first_batch_silently_skips_phase3():
    """Old glaas without the staged endpoint returns 404; coordinator should
    treat it as 'fall back to legacy link-implicit creation' and surface no
    error, so `roar register` doesn't fail on every old server."""
    service, client = _service()
    client.register_artifacts_batch_under_registration_session.return_value = (
        0,
        2,
        "HTTP 404: HTTP Error 404: Not Found",
    )

    result = service.register_batch_under_registration_session(_artifacts(2), "reg-sess-x")

    assert result.success_count == 0
    assert result.error_count == 0
    assert result.errors == []


def test_non_404_error_still_surfaces():
    """Other HTTP errors (validation, 5xx, etc.) should still register as
    errors so the user knows registration didn't fully succeed."""
    service, client = _service()
    client.register_artifacts_batch_under_registration_session.return_value = (
        0,
        2,
        "HTTP 500: internal server error",
    )

    result = service.register_batch_under_registration_session(_artifacts(2), "reg-sess-x")

    assert result.error_count > 0
    assert any("500" in e for e in result.errors)


def test_404_after_a_successful_batch_does_not_silently_skip():
    """If we've already successfully sent batches and a later one returns
    404, that's a real anomaly (the endpoint can't disappear mid-register)
    — surface it normally."""
    service, client = _service()
    # First batch succeeds, second returns 404
    client.register_artifacts_batch_under_registration_session.side_effect = [
        (5, 0, None),
        (0, 5, "HTTP 404: HTTP Error 404: Not Found"),
    ]
    # Force two batches by making the payload size exceed 90KB
    artifacts = []
    for i in range(450):
        artifacts.append(
            {
                "hashes": [{"algorithm": "blake3", "digest": f"hash{i:060d}"}],
                "size": 1,
                "source_type": None,
                "metadata": "x" * 200,
            }
        )

    result = service.register_batch_under_registration_session(artifacts, "reg-sess-x")

    # First batch ran; second 404'd — that gets surfaced (not silently dropped)
    assert any("404" in e for e in result.errors)
