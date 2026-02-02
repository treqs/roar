"""
Unit tests for GLaaS client error handling.

Verifies that the client raises appropriate exceptions instead of
returning (result, error) tuples.
"""

import pytest
from unittest.mock import patch, MagicMock
import urllib.error

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
        from roar.core.exceptions import RoarException, GlaasError
        
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
