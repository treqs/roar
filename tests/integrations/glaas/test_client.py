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
    GlaasConnectionError,
    GlaasNotConfiguredError,
)
from roar.integrations.glaas import GlaasClient
from roar.integrations.glaas.transport import reset_auth_mode_cache
from roar.publish_auth import PublishAuthContext


@pytest.fixture(autouse=True)
def reset_glaas_auth_mode() -> None:
    reset_auth_mode_cache()
    yield
    reset_auth_mode_cache()


def _optional_auth_client() -> GlaasClient:
    return GlaasClient(
        base_url="http://localhost:9999",
        publish_auth=PublishAuthContext(access_token=None, scope_request=None),
    )


def test_force_anonymous_suppresses_ssh_auth_header_generation() -> None:
    client = GlaasClient(
        base_url="http://localhost:9999",
        publish_auth=PublishAuthContext(access_token="token", scope_request=None),
        force_anonymous=True,
    )

    with patch("roar.integrations.glaas.client.make_auth_header") as make_auth_header:
        assert client._make_auth_header("POST", "/api/v1/labels/sync", b"{}") is None

    make_auth_header.assert_not_called()
    assert client.publish_auth.access_token is None
    assert client.publish_auth.ssh_auth_available is False


def test_finalize_current_user_private_scope_reports_server_support_gap() -> None:
    client = GlaasClient(
        base_url="http://localhost:9999",
        publish_auth=PublishAuthContext(
            access_token="token",
            scope_request={
                "owner_resolution": "current_user",
                "owner_type": "user",
                "visibility": "private",
            },
        ),
    )

    with patch.object(client, "_request") as mock_request:
        mock_request.return_value = (
            None,
            "HTTP 400: Invalid scope_request: unknown field owner_resolution",
        )
        _result, error = client.finalize_registration_session(
            "reg-1",
            git_repo="repo",
            git_commit="commit",
            git_branch="main",
        )

    assert error == (
        "Private personal scope is not available on this GLaaS server yet. "
        "Use `roar scope use <owner>/<project>` or `roar register --public`."
    )


class TestGlaasClientExceptions:
    """Test that GlaasClient raises proper exceptions."""

    def test_health_check_raises_not_configured_when_no_url(self):
        """health_check should raise GlaasNotConfiguredError when URL is missing."""
        # Must patch get_glaas_url since GlaasClient falls back to it
        with patch("roar.integrations.glaas.client.get_glaas_url", return_value=None):
            client = GlaasClient(base_url=None)

            with pytest.raises(GlaasNotConfiguredError) as exc_info:
                client.health_check()

            assert "not configured" in str(exc_info.value).lower()

    def test_empty_base_url_does_not_fall_back_to_config_lookup(self):
        """An explicit empty URL should stay unconfigured without reading config."""
        with patch("roar.integrations.glaas.client.get_glaas_url") as get_glaas_url:
            client = GlaasClient(base_url="")

        get_glaas_url.assert_not_called()
        assert client.base_url is None

    def test_health_check_raises_connection_error_on_network_failure(self):
        """health_check should raise GlaasConnectionError on network errors."""
        client = _optional_auth_client()

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

            with pytest.raises(GlaasConnectionError) as exc_info:
                client.health_check()

            assert "connection" in str(exc_info.value).lower()

    def test_health_check_raises_api_error_on_bad_status(self):
        """health_check should raise GlaasApiError on non-200 status."""
        client = _optional_auth_client()

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.status = 503
            mock_response.__enter__ = MagicMock(return_value=mock_response)
            mock_response.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_response

            with pytest.raises(GlaasApiError) as exc_info:
                client.health_check()

            assert exc_info.value.status_code == 503

    def test_health_check_returns_health_payload_on_success(self):
        """health_check should return the parsed health payload on success."""
        client = _optional_auth_client()

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.status = 200
            mock_response.read.return_value = b'{"status": "healthy"}'
            mock_response.__enter__ = MagicMock(return_value=mock_response)
            mock_response.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_response

            result = client.health_check()
            assert result == {"status": "healthy"}

    def test_get_artifact_raises_on_not_found(self):
        """get_artifact should raise GlaasApiError on 404."""
        client = _optional_auth_client()

        with patch.object(client, "_request") as mock_request:
            mock_request.side_effect = GlaasApiError("Not found", status_code=404)

            with pytest.raises(GlaasApiError) as exc_info:
                client.get_artifact("abc123")

            assert exc_info.value.status_code == 404

    def test_get_artifact_returns_dict_on_success(self):
        """get_artifact should return dict directly on success."""
        client = _optional_auth_client()

        expected = {"hash": "abc123", "size": 100}
        with patch.object(client, "_request") as mock_request:
            # _request returns (result, error) tuple
            mock_request.return_value = (expected, None)

            result = client.get_artifact("abc123")
            assert result == expected


class TestExceptionHierarchy:
    """Test that exception classes have correct hierarchy."""

    def test_glaas_api_error_stores_status_code(self):
        """GlaasApiError should store HTTP status code."""
        error = GlaasApiError("Bad request", status_code=400)
        assert error.status_code == 400
        assert "Bad request" in str(error)


class TestOptionalAuth:
    """Test that _request() works without SSH keys (optional auth)."""

    @staticmethod
    def _json_response(payload: bytes) -> MagicMock:
        response = MagicMock()
        response.status = 200
        response.read.return_value = payload
        response.__enter__ = MagicMock(return_value=response)
        response.__exit__ = MagicMock(return_value=False)
        return response

    def test_request_succeeds_without_auth_header(self):
        """_request proceeds without Authorization header when no SSH keys available."""
        client = _optional_auth_client()

        with (
            patch("roar.integrations.glaas.client.make_auth_header", return_value=None),
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            mock_urlopen.return_value = self._json_response(b'{"success": true, "data": {"id": 1}}')

            _result, error = client._request("POST", "/api/v1/test", {"key": "val"})

            assert error is None
            assert _result == {"id": 1}

            # Verify no Authorization header was set
            req = mock_urlopen.call_args[0][0]
            assert req.get_header("Authorization") is None

    def test_request_includes_auth_header_when_keys_available(self):
        """_request includes Authorization header when SSH keys are available."""
        client = _optional_auth_client()

        with (
            patch(
                "roar.integrations.glaas.client.make_auth_header",
                return_value="SSH-SIG test-signature",
            ),
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            mock_urlopen.side_effect = [
                self._json_response(b'{"success": true, "data": {"items": []}}'),
                self._json_response(b'{"success": true, "data": {}}'),
            ]

            _result, error = client._request("POST", "/api/v1/test", {"key": "val"})

            assert error is None
            assert mock_urlopen.call_count == 2

            probe_request = mock_urlopen.call_args_list[0][0][0]
            request = mock_urlopen.call_args_list[1][0][0]
            assert probe_request.full_url == "http://localhost:9999/api/v1/sessions?limit=1"
            assert probe_request.get_header("Authorization") == "SSH-SIG test-signature"
            assert request.get_header("Authorization") == "SSH-SIG test-signature"

    def test_authenticated_probe_is_cached_after_first_success(self):
        """Once a protected probe succeeds, later requests should not re-probe."""
        client = _optional_auth_client()

        with (
            patch(
                "roar.integrations.glaas.client.make_auth_header",
                return_value="SSH-SIG test-signature",
            ),
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            mock_urlopen.side_effect = [
                self._json_response(b'{"success": true, "data": {"items": []}}'),
                self._json_response(b'{"success": true, "data": {"id": 1}}'),
                self._json_response(b'{"success": true, "data": {"id": 2}}'),
            ]

            first_result, first_error = client._request("GET", "/api/v1/test", None)
            second_result, second_error = client._request("GET", "/api/v1/test-2", None)

            assert first_error is None
            assert second_error is None
            assert first_result == {"id": 1}
            assert second_result == {"id": 2}
            assert mock_urlopen.call_count == 3
            assert mock_urlopen.call_args_list[0][0][0].full_url.endswith(
                "/api/v1/sessions?limit=1"
            )
            assert mock_urlopen.call_args_list[1][0][0].full_url.endswith("/api/v1/test")
            assert mock_urlopen.call_args_list[2][0][0].full_url.endswith("/api/v1/test-2")

    def test_request_retries_without_auth_after_401(self):
        """Optional-auth endpoints should fall back to anonymous access after 401."""
        client = _optional_auth_client()

        unauthorized = urllib.error.HTTPError(
            url="http://localhost:9999/api/v1/test",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )
        unauthorized.read = MagicMock(return_value=b'{"detail":"Unauthorized"}')

        with (
            patch(
                "roar.integrations.glaas.client.make_auth_header",
                return_value="SSH-SIG test-signature",
            ),
            patch("roar.integrations.glaas.transport._get_logger") as mock_get_logger,
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            mock_urlopen.side_effect = [
                unauthorized,
                self._json_response(b'{"success": true, "data": {"id": 1}}'),
            ]

            result, error = client._request("GET", "/api/v1/test", None)

            assert error is None
            assert result == {"id": 1}
            assert mock_urlopen.call_count == 2

            first_request = mock_urlopen.call_args_list[0][0][0]
            second_request = mock_urlopen.call_args_list[1][0][0]
            assert first_request.full_url == "http://localhost:9999/api/v1/sessions?limit=1"
            assert first_request.get_header("Authorization") == "SSH-SIG test-signature"
            assert second_request.get_header("Authorization") is None
            mock_get_logger.return_value.warning.assert_called_once()

    def test_bearer_401_retries_with_ssh_and_disables_bearer_for_later_requests(self):
        """An expired bearer should fall back to SSH and not poison staged writes."""
        client = GlaasClient(
            base_url="http://localhost:9999",
            publish_auth=PublishAuthContext(
                access_token="expired-token",
                scope_request=None,
                ssh_auth_available=True,
            ),
        )

        unauthorized = urllib.error.HTTPError(
            url="http://localhost:9999/api/v1/test",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )
        unauthorized.read = MagicMock(return_value=b'{"detail":"expired"}')

        with (
            patch(
                "roar.integrations.glaas.client.make_auth_header",
                return_value="Signature test-signature",
            ) as make_auth_header,
            patch("roar.integrations.glaas.transport._get_logger") as mock_get_logger,
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            mock_urlopen.side_effect = [
                unauthorized,
                self._json_response(b'{"success": true, "data": {"id": 1}}'),
                self._json_response(b'{"success": true, "data": {"id": 2}}'),
            ]

            first_result, first_error = client._request("GET", "/api/v1/test", None)
            second_result, second_error = client._request(
                "POST",
                "/api/v1/test-2",
                {"key": "val"},
                allow_auth_fallback=False,
            )

            assert first_error is None
            assert second_error is None
            assert first_result == {"id": 1}
            assert second_result == {"id": 2}
            assert mock_urlopen.call_count == 3

            first_request = mock_urlopen.call_args_list[0][0][0]
            retry_request = mock_urlopen.call_args_list[1][0][0]
            later_request = mock_urlopen.call_args_list[2][0][0]
            assert first_request.get_header("Authorization") == "Bearer expired-token"
            assert retry_request.get_header("Authorization") == "Signature test-signature"
            assert later_request.get_header("Authorization") == "Signature test-signature"
            assert make_auth_header.call_count == 2
            mock_get_logger.return_value.warning.assert_called_once_with(
                "GLaaS bearer authentication was rejected; retrying with SSH signature auth."
            )

    def test_bearer_401_does_not_try_ssh_when_ssh_credentials_are_unavailable(self):
        """Without SSH credentials, the original bearer rejection is still surfaced."""
        client = GlaasClient(
            base_url="http://localhost:9999",
            publish_auth=PublishAuthContext(
                access_token="expired-token",
                scope_request=None,
                ssh_auth_available=False,
            ),
        )

        unauthorized = urllib.error.HTTPError(
            url="http://localhost:9999/api/v1/test",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )
        unauthorized.read = MagicMock(return_value=b'{"detail":"expired"}')

        with (
            patch("roar.integrations.glaas.client.make_auth_header") as make_auth_header,
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            mock_urlopen.side_effect = unauthorized

            result, error = client._request("GET", "/api/v1/test", None)

            assert result is None
            assert error == "HTTP 401: expired"
            assert mock_urlopen.call_count == 1
            assert (
                mock_urlopen.call_args_list[0][0][0].get_header("Authorization")
                == "Bearer expired-token"
            )
            make_auth_header.assert_not_called()

    def test_bearer_401_then_ssh_fallback_also_rejected_returns_ssh_error(self):
        """If the SSH fallback after a bearer 401 also fails, the call must not crash
        or hang, and the returned error should surface the SSH failure while noting
        that bearer auth was tried (and failed) first."""
        client = GlaasClient(
            base_url="http://localhost:9999",
            publish_auth=PublishAuthContext(
                access_token="expired-token",
                scope_request=None,
                ssh_auth_available=True,
            ),
        )

        bearer_unauthorized = urllib.error.HTTPError(
            url="http://localhost:9999/api/v1/test",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )
        bearer_unauthorized.read = MagicMock(return_value=b'{"detail":"expired"}')

        ssh_unauthorized = urllib.error.HTTPError(
            url="http://localhost:9999/api/v1/test",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )
        ssh_unauthorized.read = MagicMock(return_value=b'{"detail":"ssh signature invalid"}')

        with (
            patch(
                "roar.integrations.glaas.client.make_auth_header",
                return_value="Signature test-signature",
            ) as make_auth_header,
            patch("roar.integrations.glaas.transport._get_logger") as mock_get_logger,
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            mock_urlopen.side_effect = [bearer_unauthorized, ssh_unauthorized]

            result, error = client._request("GET", "/api/v1/test", None)

            assert result is None
            assert error == (
                "HTTP 401: ssh signature invalid "
                "(bearer authentication was tried first and also failed)"
            )
            assert mock_urlopen.call_count == 2

            first_request = mock_urlopen.call_args_list[0][0][0]
            retry_request = mock_urlopen.call_args_list[1][0][0]
            assert first_request.get_header("Authorization") == "Bearer expired-token"
            assert retry_request.get_header("Authorization") == "Signature test-signature"
            assert make_auth_header.call_count == 1

            # The rejection warning is the only user-visible trace of the bearer
            # attempt once the final error text is inspected in isolation, so a
            # regression that silently drops it should fail this test.
            mock_get_logger.return_value.warning.assert_called_once_with(
                "GLaaS bearer authentication was rejected; retrying with SSH signature auth."
            )

            # Bearer auth is still permanently disabled for later requests on this
            # client instance, same as the single-failure path (intentional).
            assert client._bearer_auth_rejected is True

    def test_bearer_401_then_ssh_fallback_connection_error_returns_ssh_error(self):
        """A non-HTTP failure (e.g. network error) on the SSH fallback attempt should
        also be surfaced cleanly rather than crashing or masking as the bearer error."""
        client = GlaasClient(
            base_url="http://localhost:9999",
            publish_auth=PublishAuthContext(
                access_token="expired-token",
                scope_request=None,
                ssh_auth_available=True,
            ),
        )

        bearer_unauthorized = urllib.error.HTTPError(
            url="http://localhost:9999/api/v1/test",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )
        bearer_unauthorized.read = MagicMock(return_value=b'{"detail":"expired"}')

        ssh_connection_error = urllib.error.URLError("Connection refused")

        with (
            patch(
                "roar.integrations.glaas.client.make_auth_header",
                return_value="Signature test-signature",
            ),
            patch("roar.integrations.glaas.transport._get_logger") as mock_get_logger,
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            mock_urlopen.side_effect = [bearer_unauthorized, ssh_connection_error]

            result, error = client._request("GET", "/api/v1/test", None)

            assert result is None
            assert error == (
                "Connection error: <urlopen error Connection refused> "
                "(bearer authentication was tried first and also failed)"
            )
            assert mock_urlopen.call_count == 2
            mock_get_logger.return_value.warning.assert_called_once_with(
                "GLaaS bearer authentication was rejected; retrying with SSH signature auth."
            )

    def test_request_returns_original_401_when_anonymous_retry_also_fails(self):
        """If optional-auth fallback still fails, surface the original auth failure."""
        client = _optional_auth_client()

        unauthorized = urllib.error.HTTPError(
            url="http://localhost:9999/api/v1/test",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )
        unauthorized.read = MagicMock(return_value=b'{"detail":"Unauthorized"}')

        forbidden = urllib.error.HTTPError(
            url="http://localhost:9999/api/v1/test",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=None,
        )
        forbidden.read = MagicMock(return_value=b'{"detail":"Forbidden"}')

        with (
            patch(
                "roar.integrations.glaas.client.make_auth_header",
                return_value="SSH-SIG test-signature",
            ),
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            mock_urlopen.side_effect = [unauthorized, forbidden]

            result, error = client._request("GET", "/api/v1/test", None)

            assert result is None
            assert error == "HTTP 403: Forbidden"
            assert mock_urlopen.call_count == 2

    def test_request_extracts_nested_api_error_message(self):
        """HTTP errors should surface nested API error.message details."""
        client = _optional_auth_client()

        unauthorized = urllib.error.HTTPError(
            url="http://localhost:9999/api/v1/test",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )
        unauthorized.read = MagicMock(
            return_value=b'{"success": false, "error": {"message": "Unknown key"}}'
        )

        with (
            patch("roar.integrations.glaas.client.make_auth_header", return_value=None),
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            mock_urlopen.side_effect = unauthorized

            result, error = client._request("GET", "/api/v1/test", None)

            assert result is None
            assert error == "HTTP 401: Unknown key"

    def test_anonymous_fallback_is_cached_after_first_auth_failure(self):
        """Once auth is rejected, future requests should skip auth and the probe."""
        client = _optional_auth_client()

        unauthorized = urllib.error.HTTPError(
            url="http://localhost:9999/api/v1/sessions?limit=1",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )
        unauthorized.read = MagicMock(return_value=b'{"detail":"Unknown key"}')

        with (
            patch(
                "roar.integrations.glaas.client.make_auth_header",
                return_value="SSH-SIG test-signature",
            ),
            patch("roar.integrations.glaas.transport._get_logger") as mock_get_logger,
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            mock_urlopen.side_effect = [
                unauthorized,
                self._json_response(b'{"success": true, "data": {"id": 1}}'),
                self._json_response(b'{"success": true, "data": {"id": 2}}'),
            ]

            first_result, first_error = client._request("GET", "/api/v1/test", None)
            second_result, second_error = client._request("GET", "/api/v1/test-2", None)

            assert first_error is None
            assert second_error is None
            assert first_result == {"id": 1}
            assert second_result == {"id": 2}
            assert mock_urlopen.call_count == 3
            assert mock_urlopen.call_args_list[0][0][0].full_url.endswith(
                "/api/v1/sessions?limit=1"
            )
            assert mock_urlopen.call_args_list[1][0][0].full_url.endswith("/api/v1/test")
            assert mock_urlopen.call_args_list[1][0][0].get_header("Authorization") is None
            assert mock_urlopen.call_args_list[2][0][0].full_url.endswith("/api/v1/test-2")
            assert mock_urlopen.call_args_list[2][0][0].get_header("Authorization") is None
            mock_get_logger.return_value.warning.assert_called_once()

    def test_probe_publish_auth_rejection_allows_anonymous_registration_session_create(self):
        """Rejected SSH auth should not poison anonymous-public registration-session creation."""
        client = _optional_auth_client()

        unauthorized = urllib.error.HTTPError(
            url="http://localhost:9999/api/v1/sessions?limit=1",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )
        unauthorized.read = MagicMock(return_value=b'{"detail":"Unknown key"}')

        with (
            patch(
                "roar.integrations.glaas.client.make_auth_header",
                return_value="SSH-SIG test-signature",
            ),
            patch("roar.integrations.glaas.transport._get_logger"),
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            mock_urlopen.side_effect = [
                unauthorized,
                self._json_response(
                    b'{"success": true, "data": {'
                    b'"registration_session_id": "reg-123", '
                    b'"registration_session_token": "token-123", '
                    b'"mode": "anonymous_public"}}'
                ),
            ]

            assert client.probe_publish_auth() is False
            result, error = client.create_registration_session(mode="anonymous_public")

            assert error is None
            assert result == {
                "registration_session_id": "reg-123",
                "registration_session_token": "token-123",
                "mode": "anonymous_public",
            }
            assert mock_urlopen.call_count == 2
            create_request = mock_urlopen.call_args_list[1][0][0]
            assert create_request.full_url == "http://localhost:9999/api/v1/registration-sessions"
            assert create_request.get_header("Authorization") is None
            assert create_request.data == b'{"mode": "anonymous_public"}'


class TestRegisterJobsBatch:
    """Test register_jobs_batch client method."""

    def test_empty_jobs_returns_empty(self):
        """Empty jobs list returns empty results without making request."""
        client = _optional_auth_client()

        job_ids, errors, error = client.register_jobs_batch(
            session_hash="session123",
            jobs=[],
        )

        assert job_ids == []
        assert errors == []
        assert error is None

    def test_batch_propagates_overall_error(self):
        """Overall request error is returned for all jobs."""
        client = _optional_auth_client()

        with patch.object(client, "_request") as mock_request:
            mock_request.return_value = (None, "HTTP 500: Internal Server Error")

            job_ids, errors, error = client.register_jobs_batch(
                session_hash="session123",
                jobs=[{"job_uid": "j1"}, {"job_uid": "j2"}],
            )

            assert job_ids == []
            assert len(errors) == 2
            assert error == "HTTP 500: Internal Server Error"


class TestParentJobUidPayload:
    """Ensure optional parent_job_uid is serialized correctly."""

    def test_register_job_includes_parent_job_uid_when_present(self):
        client = _optional_auth_client()

        with patch.object(client, "_request") as mock_request:
            mock_request.return_value = ({"id": 1}, None)

            client.register_job(
                session_hash="sess123",
                command="ray_task:train",
                timestamp=1.0,
                job_uid="job12345",
                git_commit="abc123",
                git_branch="main",
                duration_seconds=2.5,
                exit_code=0,
                job_type="ray_task",
                step_number=1,
                parent_job_uid="parent-abc",
            )

            body = mock_request.call_args[0][2]
            assert body["parent_job_uid"] == "parent-abc"

    def test_register_job_omits_parent_job_uid_when_absent(self):
        client = _optional_auth_client()

        with patch.object(client, "_request") as mock_request:
            mock_request.return_value = ({"id": 1}, None)

            client.register_job(
                session_hash="sess123",
                command="python train.py",
                timestamp=1.0,
                job_uid="job12345",
                git_commit="abc123",
                git_branch="main",
                duration_seconds=2.5,
                exit_code=0,
                job_type="run",
                step_number=1,
            )

            body = mock_request.call_args[0][2]
            assert "parent_job_uid" not in body

    def test_register_jobs_batch_preserves_parent_job_uid_fields(self):
        client = _optional_auth_client()

        with patch.object(client, "_request") as mock_request:
            mock_request.return_value = (
                {"job_ids": ["id-1", "id-2"], "errors": []},
                None,
            )

            jobs = [
                {
                    "command": "ray_task:a",
                    "timestamp": 1.0,
                    "job_uid": "j1",
                    "git_commit": "abc",
                    "git_branch": "main",
                    "duration_seconds": 1.0,
                    "exit_code": 0,
                    "job_type": "ray_task",
                    "step_number": 1,
                    "parent_job_uid": "parent-abc",
                },
                {
                    "command": "python train.py",
                    "timestamp": 2.0,
                    "job_uid": "j2",
                    "git_commit": "abc",
                    "git_branch": "main",
                    "duration_seconds": 2.0,
                    "exit_code": 0,
                    "job_type": "run",
                    "step_number": 1,
                },
            ]
            client.register_jobs_batch(session_hash="sess123", jobs=jobs)

            body = mock_request.call_args[0][2]
            assert body["jobs"][0]["parent_job_uid"] == "parent-abc"
            assert "parent_job_uid" not in body["jobs"][1]


class TestSessionReproductionWrapper:
    """Test thin client wrapper for lineage/session reproduction."""


class TestCompositeAndLabelWrappers:
    """Test thin client wrappers for composite artifact endpoints."""


class TestArtifactHashMapping:
    """Verify that register_job_inputs/outputs map internal hash keys to API payloads."""

    def test_map_artifacts_for_api_uses_artifact_hash(self):
        mapped = GlaasClient._map_artifacts_for_api(
            [
                {"artifact_hash": "abc123def456", "path": "/data/input.csv"},
            ]
        )
        assert mapped == [{"hash": "abc123def456", "path": "/data/input.csv"}]

    def test_map_artifacts_for_api_accepts_legacy_artifact_id(self):
        mapped = GlaasClient._map_artifacts_for_api(
            [
                {"artifact_id": "abc123def456", "path": "/data/input.csv"},
            ]
        )
        assert mapped == [{"hash": "abc123def456", "path": "/data/input.csv"}]

    def test_map_artifacts_for_api_preserves_byte_ranges(self):
        mapped = GlaasClient._map_artifacts_for_api(
            [
                {"artifact_hash": "abc123", "path": "/data/f.csv", "byte_ranges": [[0, 99]]},
            ]
        )
        assert mapped == [{"hash": "abc123", "path": "/data/f.csv", "byte_ranges": [[0, 99]]}]

    def test_map_artifacts_for_api_falls_back_to_hash_key(self):
        mapped = GlaasClient._map_artifacts_for_api(
            [
                {"hash": "abc123def456", "path": "/data/input.csv"},
            ]
        )
        assert mapped == [{"hash": "abc123def456", "path": "/data/input.csv"}]

    def test_register_job_inputs_sends_mapped_payload(self):
        client = _optional_auth_client()
        with patch.object(client, "_request") as mock_request:
            mock_request.return_value = ({"inputs_linked": 1}, None)

            client.register_job_inputs(
                session_hash="sess123",
                job_uid="job-001",
                artifacts=[{"artifact_hash": "hash123abc", "path": "/input.csv"}],
            )

            call_body = mock_request.call_args[0][2]
            assert call_body == {"artifacts": [{"hash": "hash123abc", "path": "/input.csv"}]}

    def test_register_job_outputs_sends_mapped_payload(self):
        client = _optional_auth_client()
        with patch.object(client, "_request") as mock_request:
            mock_request.return_value = ({"outputs_linked": 1}, None)

            client.register_job_outputs(
                session_hash="sess123",
                job_uid="job-001",
                artifacts=[{"artifact_hash": "hash456def", "path": "/output.csv"}],
            )

            call_body = mock_request.call_args[0][2]
            assert call_body == {"artifacts": [{"hash": "hash456def", "path": "/output.csv"}]}
