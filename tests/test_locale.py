"""Tests for locale support in Odoo MCP Server.

Covers session locale (mutable), per-call lang overrides, jsonb translation
helpers, env-var based config loading, and invalid-locale fallback.
"""

import xmlrpc.client
from unittest.mock import MagicMock, patch

import pytest

from mcp_server_odoo.config import OdooConfig, load_config
from mcp_server_odoo.odoo_connection import OdooConnection, OdooConnectionError


class TestLocaleSupport:
    """Test locale/language support."""

    @pytest.fixture
    def config_with_locale(self):
        """Create test configuration with Spanish locale."""
        return OdooConfig(
            url="https://test.odoo.com",
            api_key="test_key",
            username="test",
            database="test_db",
            locale="es_ES",
            yolo_mode="true",  # Use YOLO mode for testing
        )

    @pytest.fixture
    def config_with_ar_locale(self):
        """Create test configuration with Argentine Spanish locale."""
        return OdooConfig(
            url="https://test.odoo.com",
            api_key="test_key",
            username="test",
            database="test_db",
            locale="es_AR",
            yolo_mode="true",
        )

    @pytest.fixture
    def config_without_locale(self):
        """Create test configuration without locale."""
        return OdooConfig(
            url="https://test.odoo.com",
            api_key="test_key",
            username="test",
            database="test_db",
            yolo_mode="true",
        )

    def test_locale_injected_in_execute_kw(self, config_with_locale):
        """Test that locale is injected into context when executing operations."""
        conn = OdooConnection(config_with_locale)

        # Mock the connection
        conn._connected = True
        conn._authenticated = True
        conn._uid = 1
        conn._database = "test_db"
        conn._auth_method = "api_key"

        # Mock the object proxy
        mock_proxy = MagicMock()
        mock_proxy.execute_kw.return_value = [{"id": 1, "name": "Test"}]
        conn._object_proxy = mock_proxy

        # Execute a search operation
        kwargs = {}
        conn.execute_kw("res.partner", "search_read", [[]], kwargs)

        # Verify that locale was injected into context
        # execute_kw is called with: (database, uid, password, model, method, args, kwargs)
        call_args = mock_proxy.execute_kw.call_args
        passed_kwargs = call_args[0][6]  # kwargs is the 7th positional argument (index 6)

        assert "context" in passed_kwargs
        assert "lang" in passed_kwargs["context"]
        assert passed_kwargs["context"]["lang"] == "es_ES"

    def test_argentine_locale_injected(self, config_with_ar_locale):
        """Test that Argentine Spanish locale is properly injected."""
        conn = OdooConnection(config_with_ar_locale)

        conn._connected = True
        conn._authenticated = True
        conn._uid = 1
        conn._database = "test_db"
        conn._auth_method = "api_key"

        mock_proxy = MagicMock()
        mock_proxy.execute_kw.return_value = []
        conn._object_proxy = mock_proxy

        kwargs = {}
        conn.execute_kw("res.partner", "search", [[]], kwargs)

        call_args = mock_proxy.execute_kw.call_args
        passed_kwargs = call_args[0][6]  # kwargs is the 7th positional argument

        assert passed_kwargs["context"]["lang"] == "es_AR"

    def test_no_locale_when_not_configured(self, config_without_locale):
        """Test that no locale is injected when not configured."""
        conn = OdooConnection(config_without_locale)

        conn._connected = True
        conn._authenticated = True
        conn._uid = 1
        conn._database = "test_db"
        conn._auth_method = "api_key"

        mock_proxy = MagicMock()
        mock_proxy.execute_kw.return_value = []
        conn._object_proxy = mock_proxy

        kwargs = {}
        conn.execute_kw("res.partner", "search", [[]], kwargs)

        call_args = mock_proxy.execute_kw.call_args
        passed_kwargs = call_args[0][6]  # kwargs is the 7th positional argument

        # Context should not be added if it wasn't there and no locale is set
        # OR if context exists, it shouldn't have 'lang'
        if "context" in passed_kwargs:
            assert "lang" not in passed_kwargs["context"]

    def test_locale_preserves_existing_context(self, config_with_locale):
        """Test that locale injection preserves existing context values."""
        conn = OdooConnection(config_with_locale)

        conn._connected = True
        conn._authenticated = True
        conn._uid = 1
        conn._database = "test_db"
        conn._auth_method = "api_key"

        mock_proxy = MagicMock()
        mock_proxy.execute_kw.return_value = []
        conn._object_proxy = mock_proxy

        # Pass existing context with some values
        kwargs = {"context": {"active_test": False, "tz": "America/Argentina/Buenos_Aires"}}
        conn.execute_kw("res.partner", "search_read", [[]], kwargs)

        call_args = mock_proxy.execute_kw.call_args
        passed_kwargs = call_args[0][6]  # kwargs is the 7th positional argument

        # Verify existing context values are preserved
        assert passed_kwargs["context"]["active_test"] is False
        assert passed_kwargs["context"]["tz"] == "America/Argentina/Buenos_Aires"
        # And locale was added
        assert passed_kwargs["context"]["lang"] == "es_ES"

    def test_locale_from_environment_variable(self):
        """Test loading locale from ODOO_LOCALE environment variable."""
        with patch.dict("os.environ", {"ODOO_LOCALE": "fr_FR"}):
            from mcp_server_odoo.config import load_config

            with patch.dict(
                "os.environ",
                {
                    "ODOO_URL": "https://test.odoo.com",
                    "ODOO_USER": "test",
                    "ODOO_PASSWORD": "test",
                    "ODOO_YOLO": "true",
                },
            ):
                config = load_config()
                assert config.locale == "fr_FR"

    def test_common_locales_accepted(self):
        """Test that common locale codes are accepted."""
        common_locales = [
            "es_ES",  # Spanish (Spain)
            "es_AR",  # Spanish (Argentina)
            "es_MX",  # Spanish (Mexico)
            "en_US",  # English (US)
            "en_GB",  # English (UK)
            "fr_FR",  # French
            "pt_BR",  # Portuguese (Brazil)
            "de_DE",  # German
            "it_IT",  # Italian
        ]

        for locale_code in common_locales:
            config = OdooConfig(
                url="https://test.odoo.com",
                api_key="test_key",
                username="test",
                locale=locale_code,
                yolo_mode="true",
            )
            assert config.locale == locale_code


def _bootstrap_authenticated_connection(config: OdooConfig) -> tuple:
    """Build a fake authenticated connection wired to a mock proxy."""
    conn = OdooConnection(config)
    conn._connected = True
    conn._authenticated = True
    conn._uid = 1
    conn._database = "test_db"
    conn._auth_method = "api_key"

    mock_proxy = MagicMock()
    mock_proxy.execute_kw.return_value = []
    conn._object_proxy = mock_proxy
    return conn, mock_proxy


class TestPerCallLangOverride:
    """Per-call ``lang`` parameter takes precedence over session locale."""

    def _config(self, locale: str = "es_ES") -> OdooConfig:
        return OdooConfig(
            url="https://test.odoo.com",
            api_key="test_key",
            username="test",
            database="test_db",
            locale=locale,
            yolo_mode="true",
        )

    def test_caller_provided_lang_in_context_is_respected(self):
        """If the caller passed ``context.lang`` directly, it must NOT be overwritten."""
        conn, mock_proxy = _bootstrap_authenticated_connection(self._config("es_ES"))

        kwargs = {"context": {"lang": "fr_FR"}}
        conn.execute_kw("res.partner", "search", [[]], kwargs)

        passed_kwargs = mock_proxy.execute_kw.call_args[0][6]
        assert passed_kwargs["context"]["lang"] == "fr_FR"

    def test_read_lang_param_overrides_session(self):
        """``OdooConnection.read(lang=...)`` overrides the session locale for one call."""
        conn, mock_proxy = _bootstrap_authenticated_connection(self._config("es_ES"))
        mock_proxy.execute_kw.return_value = [{"id": 7, "name": "Hi"}]

        conn.read("res.partner", [7], ["name"], lang="en_US")

        passed_kwargs = mock_proxy.execute_kw.call_args[0][6]
        assert passed_kwargs["context"]["lang"] == "en_US"

    def test_search_read_lang_param_overrides_session(self):
        conn, mock_proxy = _bootstrap_authenticated_connection(self._config("es_ES"))

        conn.search_read("res.partner", [], lang="fr_FR")

        passed_kwargs = mock_proxy.execute_kw.call_args[0][6]
        assert passed_kwargs["context"]["lang"] == "fr_FR"

    def test_write_lang_param_overrides_session(self):
        conn, mock_proxy = _bootstrap_authenticated_connection(self._config("es_ES"))
        mock_proxy.execute_kw.return_value = True

        conn.write("res.partner", [1], {"name": "Foo"}, lang="en_US")

        passed_kwargs = mock_proxy.execute_kw.call_args[0][6]
        assert passed_kwargs["context"]["lang"] == "en_US"

    def test_create_lang_param_overrides_session(self):
        conn, mock_proxy = _bootstrap_authenticated_connection(self._config("es_ES"))
        mock_proxy.execute_kw.return_value = 42

        conn.create("res.partner", {"name": "Foo"}, lang="en_US")

        passed_kwargs = mock_proxy.execute_kw.call_args[0][6]
        assert passed_kwargs["context"]["lang"] == "en_US"

    def test_no_lang_param_falls_back_to_session_locale(self):
        conn, mock_proxy = _bootstrap_authenticated_connection(self._config("es_ES"))
        mock_proxy.execute_kw.return_value = [{"id": 1, "name": "Hi"}]

        conn.read("res.partner", [1], ["name"])

        passed_kwargs = mock_proxy.execute_kw.call_args[0][6]
        assert passed_kwargs["context"]["lang"] == "es_ES"


class TestSessionLocale:
    """Mutable per-session locale state."""

    def _config(self, locale=None) -> OdooConfig:
        return OdooConfig(
            url="https://test.odoo.com",
            api_key="test_key",
            username="test",
            database="test_db",
            locale=locale,
            yolo_mode="true",
        )

    def test_session_locale_initialized_from_config(self):
        conn = OdooConnection(self._config(locale="es_ES"))
        assert conn.get_session_locale() == "es_ES"

    def test_session_locale_none_when_not_configured(self):
        conn = OdooConnection(self._config(locale=None))
        assert conn.get_session_locale() is None

    def test_set_session_locale_changes_subsequent_calls(self):
        conn, mock_proxy = _bootstrap_authenticated_connection(self._config("es_ES"))
        conn.set_session_locale("fr_FR")

        conn.execute_kw("res.partner", "search", [[]], {})
        passed_kwargs = mock_proxy.execute_kw.call_args[0][6]
        assert passed_kwargs["context"]["lang"] == "fr_FR"

    def test_set_session_locale_clears_when_none(self):
        conn, mock_proxy = _bootstrap_authenticated_connection(self._config("es_ES"))
        conn.set_session_locale(None)

        conn.execute_kw("res.partner", "search", [[]], {})
        passed_kwargs = mock_proxy.execute_kw.call_args[0][6]
        # No lang should be injected
        if "context" in passed_kwargs:
            assert "lang" not in passed_kwargs["context"]

    def test_set_session_locale_clears_when_empty_string(self):
        conn, _ = _bootstrap_authenticated_connection(self._config("es_ES"))
        conn.set_session_locale("   ")
        assert conn.get_session_locale() is None


class TestTranslationHelpers:
    """``get_field_translations`` / ``update_field_translations`` wrappers."""

    def _config(self) -> OdooConfig:
        return OdooConfig(
            url="https://test.odoo.com",
            api_key="test_key",
            username="test",
            database="test_db",
            yolo_mode="true",
        )

    def test_get_field_translations_returns_per_record_payload(self):
        conn, mock_proxy = _bootstrap_authenticated_connection(self._config())
        mock_proxy.execute_kw.return_value = [
            [
                {"lang": "en_US", "source": "Sofa", "value": "Sofa"},
                {"lang": "es_AR", "source": "Sofa", "value": "Sofá"},
            ],
            {"translated_field": {"string": "Name"}},
        ]

        result = conn.get_field_translations("product.template", [42], "name")

        assert 42 in result
        # Field metadata (second tuple element) is dropped
        assert isinstance(result[42], list)
        assert {entry["lang"] for entry in result[42]} == {"en_US", "es_AR"}

    def test_get_field_translations_passes_langs_filter(self):
        conn, mock_proxy = _bootstrap_authenticated_connection(self._config())
        mock_proxy.execute_kw.return_value = [
            [{"lang": "es_AR", "source": "Sofa", "value": "Sofá"}],
            {},
        ]

        conn.get_field_translations("product.template", [42], "name", langs=["es_AR"])

        # Verify langs forwarded as kwargs to Odoo's get_field_translations
        call_args = mock_proxy.execute_kw.call_args
        passed_kwargs = call_args[0][6]
        assert passed_kwargs.get("langs") == ["es_AR"]

    def test_update_field_translations_calls_per_field(self):
        conn, mock_proxy = _bootstrap_authenticated_connection(self._config())
        mock_proxy.execute_kw.return_value = True

        ok = conn.update_field_translations(
            "product.template",
            [42],
            {
                "name": {"en_US": "Sofa", "es_AR": "Sofá"},
                "description_sale": {"en_US": "x", "es_AR": "y"},
            },
        )

        assert ok is True
        # One call per translated field
        assert mock_proxy.execute_kw.call_count == 2
        methods = [call.args[4] for call in mock_proxy.execute_kw.call_args_list]
        assert methods == ["update_field_translations", "update_field_translations"]

    def test_update_field_translations_rejects_non_dict_payload(self):
        from mcp_server_odoo.odoo_connection import OdooConnectionError

        conn, _ = _bootstrap_authenticated_connection(self._config())

        with pytest.raises(OdooConnectionError):
            conn.update_field_translations("product.template", [42], {"name": "not-a-dict"})


def _make_connected(conn):
    """Compatibility helper for the fixture-based upstream tests below."""
    conn._connected = True
    conn._authenticated = True
    conn._uid = 1
    conn._database = "test_db"
    conn._auth_method = "api_key"
    mock_proxy = MagicMock()
    mock_proxy.execute_kw.return_value = []
    conn._object_proxy = mock_proxy
    return mock_proxy


@pytest.fixture
def config_with_locale():
    return OdooConfig(
        url="https://test.odoo.com",
        api_key="test_key",
        username="test",
        database="test_db",
        locale="es_ES",
        yolo_mode="true",
    )


@pytest.fixture
def config_without_locale():
    return OdooConfig(
        url="https://test.odoo.com",
        api_key="test_key",
        username="test",
        database="test_db",
        yolo_mode="true",
    )


class TestLocaleInjectionFromConfig:
    def test_no_locale_when_not_configured(self, config_without_locale):
        conn = OdooConnection(config_without_locale)
        mock_proxy = _make_connected(conn)

        conn.execute_kw("res.partner", "search", [[]], {})

        passed_kwargs = mock_proxy.execute_kw.call_args[0][6]
        assert "lang" not in passed_kwargs.get("context", {})

    def test_locale_preserves_existing_context(self, config_with_locale):
        conn = OdooConnection(config_with_locale)
        mock_proxy = _make_connected(conn)

        conn.execute_kw(
            "res.partner",
            "search_read",
            [[]],
            {"context": {"active_test": False, "tz": "Europe/Berlin"}},
        )

        passed_kwargs = mock_proxy.execute_kw.call_args[0][6]
        assert passed_kwargs["context"]["active_test"] is False
        assert passed_kwargs["context"]["tz"] == "Europe/Berlin"
        assert passed_kwargs["context"]["lang"] == "es_ES"

    def test_caller_lang_takes_precedence(self, config_with_locale):
        """Explicit lang in caller context should not be overwritten by ODOO_LOCALE."""
        conn = OdooConnection(config_with_locale)
        mock_proxy = _make_connected(conn)

        conn.execute_kw(
            "res.partner",
            "search_read",
            [[]],
            {"context": {"lang": "de_DE"}},
        )

        passed_kwargs = mock_proxy.execute_kw.call_args[0][6]
        assert passed_kwargs["context"]["lang"] == "de_DE"

    def test_locale_works_through_convenience_methods(self, config_with_locale):
        """Locale should be injected when using search/read/search_read helpers."""
        conn = OdooConnection(config_with_locale)
        mock_proxy = _make_connected(conn)

        conn.search_read("res.partner", [["is_company", "=", True]], fields=["name"])

        passed_kwargs = mock_proxy.execute_kw.call_args[0][6]
        assert passed_kwargs["context"]["lang"] == "es_ES"

    def test_locale_does_not_mutate_shared_kwargs(self, config_with_locale):
        """Ensure locale injection doesn't leak between calls via shared dicts."""
        conn = OdooConnection(config_with_locale)
        _make_connected(conn)

        shared_kwargs = {"limit": 5}
        conn.execute_kw("res.partner", "search", [[]], shared_kwargs)

        conn2 = OdooConnection(
            OdooConfig(
                url="https://test.odoo.com",
                api_key="test_key",
                username="test",
                database="test_db",
                yolo_mode="true",
            )
        )
        mock_proxy2 = _make_connected(conn2)
        fresh_kwargs = {"limit": 5}
        conn2.execute_kw("res.partner", "search", [[]], fresh_kwargs)

        passed_kwargs2 = mock_proxy2.execute_kw.call_args[0][6]
        assert "context" not in passed_kwargs2 or "lang" not in passed_kwargs2.get("context", {})


class TestLocaleInvalidFallback:
    def test_invalid_locale_falls_back_and_retries(self, config_with_locale):
        """Odoo rejects invalid locale → disable locale, retry succeeds."""
        conn = OdooConnection(config_with_locale)
        mock_proxy = _make_connected(conn)

        fault = xmlrpc.client.Fault(1, "Invalid language code: es_ES")
        mock_proxy.execute_kw.side_effect = [fault, [{"id": 1, "name": "Test"}]]

        result = conn.execute_kw("res.partner", "search_read", [[]], {})

        assert result == [{"id": 1, "name": "Test"}]
        # Session locale (mutable) is cleared on fallback; immutable config stays
        assert conn.get_session_locale() is None
        assert mock_proxy.execute_kw.call_count == 2

    def test_retry_does_not_include_lang(self, config_with_locale):
        """After fallback, the retry call should not have lang in context."""
        conn = OdooConnection(config_with_locale)
        mock_proxy = _make_connected(conn)

        fault = xmlrpc.client.Fault(1, "Invalid language code: es_ES")
        mock_proxy.execute_kw.side_effect = [fault, []]

        conn.execute_kw("res.partner", "search", [[]], {})

        # Second call (the retry) should not have lang
        retry_kwargs = mock_proxy.execute_kw.call_args_list[1][0][6]
        assert "lang" not in retry_kwargs.get("context", {})

    def test_invalid_locale_code_cleared_and_retried(self, config_without_locale):
        """Setting an invalid locale triggers fallback: locale cleared, call retried."""
        conn = OdooConnection(config_without_locale)
        mock_proxy = _make_connected(conn)

        # Simulate an invalid locale set at runtime via the mutable session locale
        conn.set_session_locale("invalid_XX")

        fault = xmlrpc.client.Fault(1, "Invalid language code: invalid_XX")
        mock_proxy.execute_kw.side_effect = [fault, [{"id": 7}]]

        result = conn.execute_kw("res.partner", "search_read", [[]], {})

        assert result == [{"id": 7}]
        assert conn.get_session_locale() is None
        assert mock_proxy.execute_kw.call_count == 2

    def test_other_faults_still_raise(self, config_with_locale):
        """Non-locale faults should propagate as OdooConnectionError."""
        conn = OdooConnection(config_with_locale)
        mock_proxy = _make_connected(conn)

        fault = xmlrpc.client.Fault(2, "Access denied")
        mock_proxy.execute_kw.side_effect = fault

        with pytest.raises(OdooConnectionError, match="Operation failed"):
            conn.execute_kw("res.partner", "search", [[]], {})

        # Session locale should NOT be cleared for unrelated faults
        assert conn.get_session_locale() == "es_ES"


class TestLocaleConfig:
    def test_locale_from_env(self):
        with patch.dict(
            "os.environ",
            {
                "ODOO_URL": "https://test.odoo.com",
                "ODOO_USER": "test",
                "ODOO_PASSWORD": "test",
                "ODOO_LOCALE": "fr_FR",
                "ODOO_YOLO": "true",
            },
        ):
            config = load_config()
            assert config.locale == "fr_FR"

    def test_no_locale_by_default(self):
        with patch.dict(
            "os.environ",
            {
                "ODOO_URL": "https://test.odoo.com",
                "ODOO_USER": "test",
                "ODOO_PASSWORD": "test",
                "ODOO_YOLO": "true",
            },
            clear=True,
        ):
            config = load_config()
            assert config.locale is None

    def test_empty_locale_treated_as_none(self):
        with patch.dict(
            "os.environ",
            {
                "ODOO_URL": "https://test.odoo.com",
                "ODOO_USER": "test",
                "ODOO_PASSWORD": "test",
                "ODOO_LOCALE": "  ",
                "ODOO_YOLO": "true",
            },
        ):
            config = load_config()
            assert config.locale is None
