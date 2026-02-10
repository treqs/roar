"""
Unit tests for GLaaS client error handling.

Verifies that the client raises appropriate exceptions instead of
returning (result, error) tuples.
"""

import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from roar.core.exceptions import (
    GlaasApiError,
    GlaasAuthError,
    GlaasConnectionError,
    GlaasNotConfiguredError,
)
from roar.glaas_client import GlaasClient


class TestGlaasClientExceptions:
    """Test that GlaasClient raises proper exceptions."""

    def test_health_check_raises_not_configured_when_no_url(self):
        """health_check should raise GlaasNotConfiguredError when URL is missing."""
        # Must patch get_glaas_url since GlaasClient falls back to it
        with patch("roar.glaas_client.get_glaas_url", return_value=None):
            client = GlaasClient(base_url=None)

            with pytest.raises(GlaasNotConfiguredError) as exc_info:
                client.health_check()

            assert "not configured" in str(exc_info.value).lower()

    def test_health_check_raises_connection_error_on_network_failure(self):
        """health_check should raise GlaasConnectionError on network errors."""
        client = GlaasClient(base_url="http://localhost:9999")

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

            with pytest.raises(GlaasConnectionError) as exc_info:
                client.health_check()

            assert "connection" in str(exc_info.value).lower()

    def test_health_check_raises_api_error_on_bad_status(self):
        """health_check should raise GlaasApiError on non-200 status."""
        client = GlaasClient(base_url="http://localhost:9999")

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.status = 503
            mock_response.__enter__ = MagicMock(return_value=mock_response)
            mock_response.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_response

            with pytest.raises(GlaasApiError) as exc_info:
                client.health_check()

            assert exc_info.value.status_code == 503

    def test_health_check_returns_true_on_success(self):
        """health_check should return True on success."""
        client = GlaasClient(base_url="http://localhost:9999")

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.status = 200
            mock_response.__enter__ = MagicMock(return_value=mock_response)
            mock_response.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_response

            result = client.health_check()
            assert result is True

    def test_get_artifact_raises_on_not_found(self):
        """get_artifact should raise GlaasApiError on 404."""
        client = GlaasClient(base_url="http://localhost:9999")

        with patch.object(client, "_request") as mock_request:
            mock_request.side_effect = GlaasApiError("Not found", status_code=404)

            with pytest.raises(GlaasApiError) as exc_info:
                client.get_artifact("abc123")

            assert exc_info.value.status_code == 404

    def test_get_artifact_returns_dict_on_success(self):
        """get_artifact should return dict directly on success."""
        client = GlaasClient(base_url="http://localhost:9999")

        expected = {"hash": "abc123", "size": 100}
        with patch.object(client, "_request") as mock_request:
            # _request returns (result, error) tuple
            mock_request.return_value = (expected, None)

            result = client.get_artifact("abc123")
            assert result == expected


class TestExceptionHierarchy:
    """Test that exception classes have correct hierarchy."""

    def test_glaas_errors_inherit_from_roar_exception(self):
        """All GLaaS errors should inherit from RoarException."""
        from roar.core.exceptions import GlaasError, RoarException

        assert issubclass(GlaasError, RoarException)
        assert issubclass(GlaasConnectionError, GlaasError)
        assert issubclass(GlaasAuthError, GlaasError)
        assert issubclass(GlaasApiError, GlaasError)
        assert issubclass(GlaasNotConfiguredError, GlaasError)

    def test_glaas_api_error_stores_status_code(self):
        """GlaasApiError should store HTTP status code."""
        error = GlaasApiError("Bad request", status_code=400)
        assert error.status_code == 400
        assert "Bad request" in str(error)


class TestOptionalAuth:
    """Test that _request() works without SSH keys (optional auth)."""

    def test_request_succeeds_without_auth_header(self):
        """_request proceeds without Authorization header when no SSH keys available."""
        client = GlaasClient(base_url="http://localhost:9999")

        with (
            patch("roar.glaas_client.make_auth_header", return_value=None),
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            mock_response = MagicMock()
            mock_response.status = 200
            mock_response.read.return_value = b'{"success": true, "data": {"id": 1}}'
            mock_response.__enter__ = MagicMock(return_value=mock_response)
            mock_response.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_response

            _result, error = client._request("POST", "/api/v1/test", {"key": "val"})

            assert error is None
            assert _result == {"id": 1}

            # Verify no Authorization header was set
            req = mock_urlopen.call_args[0][0]
            assert req.get_header("Authorization") is None

    def test_request_includes_auth_header_when_keys_available(self):
        """_request includes Authorization header when SSH keys are available."""
        client = GlaasClient(base_url="http://localhost:9999")

        with (
            patch("roar.glaas_client.make_auth_header", return_value="SSH-SIG test-signature"),
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            mock_response = MagicMock()
            mock_response.status = 200
            mock_response.read.return_value = b'{"success": true, "data": {}}'
            mock_response.__enter__ = MagicMock(return_value=mock_response)
            mock_response.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_response

            _result, error = client._request("POST", "/api/v1/test", {"key": "val"})

            assert error is None

            # Verify Authorization header was set
            req = mock_urlopen.call_args[0][0]
            assert req.get_header("Authorization") == "SSH-SIG test-signature"


class TestRegisterJobsBatch:
    """Test register_jobs_batch client method."""

    def test_empty_jobs_returns_empty(self):
        """Empty jobs list returns empty results without making request."""
        client = GlaasClient(base_url="http://localhost:9999")

        job_ids, errors, error = client.register_jobs_batch(
            session_hash="session123",
            jobs=[],
        )

        assert job_ids == []
        assert errors == []
        assert error is None

    def test_batch_calls_correct_endpoint(self):
        """Batch uses session-scoped /jobs/batch endpoint."""
        client = GlaasClient(base_url="http://localhost:9999")

        with patch.object(client, "_request") as mock_request:
            mock_request.return_value = (
                {"job_ids": ["id-1", "id-2"], "errors": []},
                None,
            )

            job_ids, errors, error = client.register_jobs_batch(
                session_hash="ses_abc123",
                jobs=[{"job_uid": "j1"}, {"job_uid": "j2"}],
            )

            mock_request.assert_called_once_with(
                "POST",
                "/api/v1/sessions/ses_abc123/jobs/batch",
                {"jobs": [{"job_uid": "j1"}, {"job_uid": "j2"}]},
            )
            assert job_ids == ["id-1", "id-2"]
            assert errors == []
            assert error is None

    def test_batch_propagates_overall_error(self):
        """Overall request error is returned for all jobs."""
        client = GlaasClient(base_url="http://localhost:9999")

        with patch.object(client, "_request") as mock_request:
            mock_request.return_value = (None, "HTTP 500: Internal Server Error")

            job_ids, errors, error = client.register_jobs_batch(
                session_hash="session123",
                jobs=[{"job_uid": "j1"}, {"job_uid": "j2"}],
            )

            assert job_ids == []
            assert len(errors) == 2
            assert error == "HTTP 500: Internal Server Error"
