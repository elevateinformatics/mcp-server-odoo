"""Tests for connection reconnection logic.

These tests verify that the OdooConnection class properly handles
stale connections and automatically reconnects when needed.
"""

import os
import socket
from unittest.mock import MagicMock

import pytest

from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.odoo_connection import OdooConnection, OdooConnectionError


@pytest.fixture
def test_config():
    """Create test configuration."""
    return OdooConfig(
        url=os.getenv("ODOO_URL", "http://localhost:8069"),
        api_key="test_api_key",
        database=os.getenv("ODOO_DB", "test_db"),
        log_level="INFO",
        default_limit=10,
        max_limit=100,
    )


class TestReconnectableErrors:
    """Test identification of reconnectable errors."""

    def test_is_reconnectable_error_remote_closed(self, test_config):
        """Test detection of 'Remote end closed connection' error."""
        conn = OdooConnection(test_config)

        error = Exception("Remote end closed connection without response")
        assert conn._is_reconnectable_error(error) is True

    def test_is_reconnectable_error_connection_reset(self, test_config):
        """Test detection of connection reset errors."""
        conn = OdooConnection(test_config)

        # Test various connection reset error messages
        errors = [
            Exception("Connection reset by peer"),
            Exception("[Errno 104] Connection reset by peer"),
            Exception("[Errno 10054] An existing connection was forcibly closed"),
            Exception("[WinError 10054] An existing connection was forcibly closed"),
        ]

        for error in errors:
            assert conn._is_reconnectable_error(error) is True, f"Should detect: {error}"

    def test_is_reconnectable_error_connection_aborted(self, test_config):
        """Test detection of connection aborted errors (Windows)."""
        conn = OdooConnection(test_config)

        # Test connection aborted error messages
        errors = [
            Exception("[Errno 10053] An established connection was aborted by the software"),
            Exception("[WinError 10053] An established connection was aborted"),
            Exception("established connection was aborted by the software in your host machine"),
        ]

        for error in errors:
            assert conn._is_reconnectable_error(error) is True, f"Should detect: {error}"

    def test_is_reconnectable_error_connection_refused(self, test_config):
        """Test detection of connection refused error."""
        conn = OdooConnection(test_config)

        error = Exception("Connection refused")
        assert conn._is_reconnectable_error(error) is True

    def test_is_reconnectable_error_broken_pipe(self, test_config):
        """Test detection of broken pipe error."""
        conn = OdooConnection(test_config)

        error = Exception("Broken pipe")
        assert conn._is_reconnectable_error(error) is True

    def test_is_reconnectable_error_eof(self, test_config):
        """Test detection of EOF error."""
        conn = OdooConnection(test_config)

        error = Exception("EOF occurred in violation of protocol")
        assert conn._is_reconnectable_error(error) is True

    def test_is_not_reconnectable_error_business_logic(self, test_config):
        """Test that business logic errors are not reconnectable."""
        conn = OdooConnection(test_config)

        errors = [
            Exception("Record not found"),
            Exception("Access denied"),
            Exception("ValidationError: Invalid field value"),
            Exception("UserError: Cannot delete record"),
        ]

        for error in errors:
            assert conn._is_reconnectable_error(error) is False, f"Should NOT detect: {error}"


class TestRefreshProxies:
    """Test proxy refresh functionality."""

    def test_refresh_proxies_gets_new_connections(self, test_config):
        """Test that _refresh_proxies gets new connections from pool."""
        conn = OdooConnection(test_config)

        # Mock the performance manager
        mock_proxy1 = MagicMock(name="proxy1")
        mock_proxy2 = MagicMock(name="proxy2")
        mock_proxy3 = MagicMock(name="proxy3")

        conn._performance_manager = MagicMock()
        conn._performance_manager.get_optimized_connection.side_effect = [
            mock_proxy1,
            mock_proxy2,
            mock_proxy3,
        ]

        # Call refresh
        conn._refresh_proxies()

        # Verify new proxies were set
        assert conn._db_proxy == mock_proxy1
        assert conn._common_proxy == mock_proxy2
        assert conn._object_proxy == mock_proxy3

        # Verify get_optimized_connection was called for each endpoint
        assert conn._performance_manager.get_optimized_connection.call_count == 3


class TestReconnect:
    """Test reconnection functionality."""

    def test_reconnect_success(self, test_config):
        """Test successful reconnection."""
        conn = OdooConnection(test_config)
        conn._connected = True

        # Mock the methods
        conn._refresh_proxies = MagicMock()
        conn._test_connection = MagicMock()

        result = conn._reconnect()

        assert result is True
        conn._refresh_proxies.assert_called_once()
        conn._test_connection.assert_called_once()

    def test_reconnect_failure_on_refresh(self, test_config):
        """Test reconnection failure when refresh fails."""
        conn = OdooConnection(test_config)
        conn._connected = True

        # Mock refresh to fail
        conn._refresh_proxies = MagicMock(side_effect=Exception("Refresh failed"))
        conn._test_connection = MagicMock()

        result = conn._reconnect()

        assert result is False
        conn._test_connection.assert_not_called()

    def test_reconnect_failure_on_test(self, test_config):
        """Test reconnection failure when test connection fails."""
        conn = OdooConnection(test_config)
        conn._connected = True

        # Mock refresh to succeed but test to fail
        conn._refresh_proxies = MagicMock()
        conn._test_connection = MagicMock(side_effect=Exception("Test failed"))

        result = conn._reconnect()

        assert result is False


class TestExecuteKwRetry:
    """Test execute_kw retry behavior."""

    def test_execute_kw_success_first_attempt(self, test_config):
        """Test successful execution on first attempt."""
        conn = OdooConnection(test_config)
        conn._connected = True
        conn._authenticated = True
        conn._auth_method = "api_key"
        conn._database = "test_db"
        conn._uid = 1

        # Mock object_proxy
        mock_proxy = MagicMock()
        mock_proxy.execute_kw.return_value = [{"id": 1, "name": "Test"}]
        conn._object_proxy = mock_proxy

        result = conn.execute_kw("res.partner", "search_read", [[]], {})

        assert result == [{"id": 1, "name": "Test"}]
        assert mock_proxy.execute_kw.call_count == 1

    def test_execute_kw_retry_on_stale_connection(self, test_config):
        """Test retry and reconnection on stale connection error."""
        conn = OdooConnection(test_config)
        conn._connected = True
        conn._authenticated = True
        conn._auth_method = "api_key"
        conn._database = "test_db"
        conn._uid = 1

        # Mock object_proxy to fail first, succeed second
        mock_proxy = MagicMock()
        mock_proxy.execute_kw.side_effect = [
            Exception("Remote end closed connection without response"),
            [{"id": 1, "name": "Test"}],
        ]
        conn._object_proxy = mock_proxy

        # Mock reconnect to succeed
        conn._reconnect = MagicMock(return_value=True)

        result = conn.execute_kw("res.partner", "search_read", [[]], {})

        assert result == [{"id": 1, "name": "Test"}]
        assert mock_proxy.execute_kw.call_count == 2
        conn._reconnect.assert_called_once()

    def test_execute_kw_no_retry_on_xmlrpc_fault(self, test_config):
        """Test that XML-RPC faults don't trigger retry."""
        import xmlrpc.client

        conn = OdooConnection(test_config)
        conn._connected = True
        conn._authenticated = True
        conn._auth_method = "api_key"
        conn._database = "test_db"
        conn._uid = 1

        # Mock object_proxy to raise XML-RPC fault
        mock_proxy = MagicMock()
        mock_proxy.execute_kw.side_effect = xmlrpc.client.Fault(1, "ValidationError: Invalid field")
        conn._object_proxy = mock_proxy

        # Mock reconnect
        conn._reconnect = MagicMock(return_value=True)

        with pytest.raises(OdooConnectionError):
            conn.execute_kw("res.partner", "write", [[1], {"invalid": "field"}], {})

        # Should NOT have tried to reconnect
        conn._reconnect.assert_not_called()
        # Should only have called execute_kw once
        assert mock_proxy.execute_kw.call_count == 1

    def test_execute_kw_max_retries_exceeded(self, test_config):
        """Test that we stop after MAX_RETRIES attempts."""
        conn = OdooConnection(test_config)
        conn._connected = True
        conn._authenticated = True
        conn._auth_method = "api_key"
        conn._database = "test_db"
        conn._uid = 1

        # Mock object_proxy to always fail with reconnectable error
        mock_proxy = MagicMock()
        mock_proxy.execute_kw.side_effect = Exception(
            "Remote end closed connection without response"
        )
        conn._object_proxy = mock_proxy

        # Mock reconnect to always succeed
        conn._reconnect = MagicMock(return_value=True)

        with pytest.raises(OdooConnectionError):
            conn.execute_kw("res.partner", "search_read", [[]], {})

        # Should have tried MAX_RETRIES + 1 times (initial + retries)
        assert mock_proxy.execute_kw.call_count == OdooConnection.MAX_RETRIES + 1
        # Should have reconnected MAX_RETRIES times
        assert conn._reconnect.call_count == OdooConnection.MAX_RETRIES

    def test_execute_kw_reconnect_fails(self, test_config):
        """Test behavior when reconnection fails."""
        conn = OdooConnection(test_config)
        conn._connected = True
        conn._authenticated = True
        conn._auth_method = "api_key"
        conn._database = "test_db"
        conn._uid = 1

        # Mock object_proxy to fail with reconnectable error
        mock_proxy = MagicMock()
        mock_proxy.execute_kw.side_effect = Exception(
            "Remote end closed connection without response"
        )
        conn._object_proxy = mock_proxy

        # Mock reconnect to fail
        conn._reconnect = MagicMock(return_value=False)

        with pytest.raises(OdooConnectionError):
            conn.execute_kw("res.partner", "search_read", [[]], {})

        # Should have called reconnect once (then given up)
        conn._reconnect.assert_called_once()
        # Should only have tried execute_kw once (original attempt)
        assert mock_proxy.execute_kw.call_count == 1

    def test_execute_kw_retry_on_socket_timeout(self, test_config):
        """Test retry on socket timeout."""
        conn = OdooConnection(test_config)
        conn._connected = True
        conn._authenticated = True
        conn._auth_method = "api_key"
        conn._database = "test_db"
        conn._uid = 1

        # Mock object_proxy to timeout first, succeed second
        mock_proxy = MagicMock()
        mock_proxy.execute_kw.side_effect = [socket.timeout("Timeout"), [{"id": 1, "name": "Test"}]]
        conn._object_proxy = mock_proxy

        # Mock reconnect to succeed
        conn._reconnect = MagicMock(return_value=True)

        result = conn.execute_kw("res.partner", "search_read", [[]], {})

        assert result == [{"id": 1, "name": "Test"}]
        assert mock_proxy.execute_kw.call_count == 2
        conn._reconnect.assert_called_once()


class TestConnectionConstants:
    """Test connection constants."""

    def test_max_retries_default(self, test_config):
        """Test MAX_RETRIES has a reasonable default."""
        assert OdooConnection.MAX_RETRIES >= 1
        assert OdooConnection.MAX_RETRIES <= 5  # Not too many

    def test_reconnectable_errors_list(self, test_config):
        """Test RECONNECTABLE_ERRORS contains expected errors."""
        errors = OdooConnection.RECONNECTABLE_ERRORS

        # Should contain the main error from the issue
        assert any("Remote end closed" in e for e in errors)

        # Should contain common connection errors
        assert any("Connection reset" in e for e in errors)
        assert any("Broken pipe" in e for e in errors)


@pytest.mark.odoo_required
class TestReconnectIntegration:
    """Integration tests for reconnection with real Odoo server."""

    def test_reconnect_refreshes_working_proxies(self, test_config):
        """Test that reconnect actually gets working proxies."""
        conn = OdooConnection(test_config)

        try:
            conn.connect()
            assert conn.is_connected

            # Force reconnect
            result = conn._reconnect()

            assert result is True

            # Verify connection still works
            is_healthy, _ = conn.check_health()
            assert is_healthy
        finally:
            conn.disconnect()
