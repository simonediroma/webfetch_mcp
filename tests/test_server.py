"""
Tests for server.py

Run with:
    pytest tests/test_server.py -v
"""
import importlib
import sys
import textwrap
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reload_server(monkeypatch, env_value=None, output_value=None, config_path=None):
    """
    Re-import server with controlled environment values.
    Necessary because module-level config is loaded at import time.
    """
    monkeypatch.delenv("WEBFETCH_HEADERS", raising=False)
    monkeypatch.delenv("WEBFETCH_OUTPUT", raising=False)
    monkeypatch.delenv("WEBFETCH_CONFIG", raising=False)

    if env_value is not None:
        monkeypatch.setenv("WEBFETCH_HEADERS", env_value)
    if output_value is not None:
        monkeypatch.setenv("WEBFETCH_OUTPUT", output_value)
    if config_path is not None:
        monkeypatch.setenv("WEBFETCH_CONFIG", str(config_path))

    sys.modules.pop("server", None)
    import server
    return server


def _make_mock_response(status_code=200, text="<html>hello</html>", content_type="text/html"):
    from http import HTTPStatus
    response = MagicMock()
    response.status_code = status_code
    response.text = text
    response.is_error = status_code >= 400
    response.headers = {"content-type": content_type}
    try:
        response.reason_phrase = HTTPStatus(status_code).phrase
    except ValueError:
        response.reason_phrase = "Unknown"
    return response


def _make_async_client_mock(response):
    client_mock = AsyncMock()
    client_mock.request = AsyncMock(return_value=response)
    async_cm = MagicMock()
    async_cm.__aenter__ = AsyncMock(return_value=client_mock)
    async_cm.__aexit__ = AsyncMock(return_value=False)
    return async_cm, client_mock


# ---------------------------------------------------------------------------
# _load_config / env backward compat
# ---------------------------------------------------------------------------

class TestLoadConfigEnv:

    def test_valid_headers_json(self, monkeypatch):
        srv = _reload_server(monkeypatch, env_value='{"*": {"User-Agent": "Bot"}}')
        assert srv._CONFIG["global"]["headers"] == {"User-Agent": "Bot"}

    def test_missing_env_vars_returns_defaults(self, monkeypatch):
        srv = _reload_server(monkeypatch)
        assert srv._CONFIG["global"]["headers"] == {}
        assert srv._CONFIG["global"]["output_format"] == "raw"
        assert srv._CONFIG["domains"] == {}

    def test_invalid_headers_json_raises(self, monkeypatch):
        monkeypatch.setenv("WEBFETCH_HEADERS", "not-valid-json{{{")
        monkeypatch.delenv("WEBFETCH_CONFIG", raising=False)
        sys.modules.pop("server", None)
        with pytest.raises((RuntimeError, Exception)):
            import server  # noqa: F401

    def test_non_dict_headers_raises(self, monkeypatch):
        monkeypatch.setenv("WEBFETCH_HEADERS", '["a", "b"]')
        monkeypatch.delenv("WEBFETCH_CONFIG", raising=False)
        sys.modules.pop("server", None)
        with pytest.raises((RuntimeError, Exception)):
            import server  # noqa: F401

    def test_domain_headers_stored_in_domains(self, monkeypatch):
        srv = _reload_server(
            monkeypatch,
            env_value='{"*": {"UA": "bot"}, "example.com": {"X-Token": "abc"}}',
        )
        assert srv._CONFIG["domains"]["example.com"]["headers"] == {"X-Token": "abc"}

    def test_output_format_global(self, monkeypatch):
        srv = _reload_server(monkeypatch, output_value='{"*": "markdown"}')
        assert srv._CONFIG["global"]["output_format"] == "markdown"

    def test_output_format_domain(self, monkeypatch):
        srv = _reload_server(
            monkeypatch,
            output_value='{"*": "raw", "news.com": "trafilatura"}',
        )
        assert srv._CONFIG["domains"]["news.com"]["output_format"] == "trafilatura"

    def test_invalid_output_format_raises(self, monkeypatch):
        monkeypatch.setenv("WEBFETCH_OUTPUT", '{"*": "nonexistent"}')
        monkeypatch.delenv("WEBFETCH_CONFIG", raising=False)
        sys.modules.pop("server", None)
        with pytest.raises((RuntimeError, Exception)):
            import server  # noqa: F401


# ---------------------------------------------------------------------------
# _load_config / YAML
# ---------------------------------------------------------------------------

class TestLoadConfigYaml:

    def test_valid_yaml(self, monkeypatch, tmp_path):
        yaml_file = tmp_path / "webfetch.yaml"
        yaml_file.write_text(textwrap.dedent("""\
            global:
              headers:
                User-Agent: TestBot
              output_format: markdown
              timeout: 45
              retry:
                attempts: 3
                backoff: 1.5
              proxy: "http://proxy:8080"
            domains:
              example.com:
                headers:
                  X-Token: abc
                output_format: trafilatura
        """))
        srv = _reload_server(monkeypatch, config_path=str(yaml_file))
        assert srv._CONFIG["global"]["headers"] == {"User-Agent": "TestBot"}
        assert srv._CONFIG["global"]["output_format"] == "markdown"
        assert srv._CONFIG["global"]["timeout"] == 45.0
        assert srv._CONFIG["global"]["retry"] == {"attempts": 3, "backoff": 1.5}
        assert srv._CONFIG["global"]["proxy"] == "http://proxy:8080"
        assert srv._CONFIG["domains"]["example.com"]["headers"] == {"X-Token": "abc"}
        assert srv._CONFIG["domains"]["example.com"]["output_format"] == "trafilatura"

    def test_yaml_missing_file_raises(self, monkeypatch, tmp_path):
        monkeypatch.setenv("WEBFETCH_CONFIG", str(tmp_path / "nonexistent.yaml"))
        sys.modules.pop("server", None)
        with pytest.raises((RuntimeError, Exception)):
            import server  # noqa: F401

    def test_yaml_invalid_syntax_raises(self, monkeypatch, tmp_path):
        yaml_file = tmp_path / "bad.yaml"
        yaml_file.write_text("key: [unclosed")
        monkeypatch.setenv("WEBFETCH_CONFIG", str(yaml_file))
        sys.modules.pop("server", None)
        with pytest.raises((RuntimeError, Exception)):
            import server  # noqa: F401

    def test_yaml_minimal_empty(self, monkeypatch, tmp_path):
        yaml_file = tmp_path / "webfetch.yaml"
        yaml_file.write_text("{}\n")
        srv = _reload_server(monkeypatch, config_path=str(yaml_file))
        assert srv._CONFIG["domains"] == {}
        assert srv._CONFIG["global"]["output_format"] == "raw"

    def test_yaml_invalid_output_format_raises(self, monkeypatch, tmp_path):
        yaml_file = tmp_path / "webfetch.yaml"
        yaml_file.write_text("global:\n  output_format: bad_value\n")
        monkeypatch.setenv("WEBFETCH_CONFIG", str(yaml_file))
        sys.modules.pop("server", None)
        with pytest.raises((RuntimeError, Exception)):
            import server  # noqa: F401


# ---------------------------------------------------------------------------
# _resolve_headers
# ---------------------------------------------------------------------------

class TestResolveHeaders:

    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        import server
        self.server = server
        self.monkeypatch = monkeypatch

    def _set_config(self, global_headers, domains=None):
        self.monkeypatch.setattr(
            self.server,
            "_CONFIG",
            {
                "global": {**self.server._DEFAULT_GLOBAL, "headers": global_headers},
                "domains": {
                    k: {"headers": v} for k, v in (domains or {}).items()
                },
            },
        )

    def test_global_only(self):
        self._set_config({"User-Agent": "GlobalBot"})
        result = self.server._resolve_headers("example.com", None)
        assert result == {"User-Agent": "GlobalBot"}

    def test_domain_match_exact(self):
        self._set_config({"User-Agent": "Bot"}, {"example.com": {"X-Token": "abc"}})
        result = self.server._resolve_headers("example.com", None)
        assert result["X-Token"] == "abc"
        assert result["User-Agent"] == "Bot"

    def test_subdomain_match(self):
        self._set_config({}, {"example.com": {"X-Token": "abc"}})
        result = self.server._resolve_headers("sub.example.com", None)
        assert result["X-Token"] == "abc"

    def test_subdomain_does_not_match_unrelated_domain(self):
        self._set_config({}, {"example.com": {"X-Token": "abc"}})
        result = self.server._resolve_headers("notexample.com", None)
        assert "X-Token" not in result

    def test_specificity_order_longer_key_wins(self):
        self._set_config(
            {},
            {"example.com": {"X-Token": "base"}, "sub.example.com": {"X-Token": "specific"}},
        )
        result = self.server._resolve_headers("sub.example.com", None)
        assert result["X-Token"] == "specific"

    def test_per_call_override_wins_over_domain(self):
        self._set_config({}, {"example.com": {"X-Token": "domain-value"}})
        result = self.server._resolve_headers("example.com", {"X-Token": "call-value"})
        assert result["X-Token"] == "call-value"

    def test_no_config_no_extra_returns_empty(self):
        self._set_config({})
        result = self.server._resolve_headers("example.com", None)
        assert result == {}

    def test_extra_headers_only(self):
        self._set_config({})
        result = self.server._resolve_headers("example.com", {"X-Custom": "yes"})
        assert result == {"X-Custom": "yes"}


# ---------------------------------------------------------------------------
# _resolve_timeout
# ---------------------------------------------------------------------------

class TestResolveTimeout:

    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        import server
        self.server = server
        self.monkeypatch = monkeypatch

    def _set_config(self, global_timeout, domains=None):
        self.monkeypatch.setattr(
            self.server,
            "_CONFIG",
            {
                "global": {**self.server._DEFAULT_GLOBAL, "timeout": global_timeout},
                "domains": {k: v for k, v in (domains or {}).items()},
            },
        )

    def test_global_default(self):
        self._set_config(30.0)
        assert self.server._resolve_timeout("example.com") == 30.0

    def test_domain_override(self):
        self._set_config(30.0, {"example.com": {"timeout": 60.0}})
        assert self.server._resolve_timeout("example.com") == 60.0

    def test_global_used_when_no_domain_match(self):
        self._set_config(15.0, {"other.com": {"timeout": 60.0}})
        assert self.server._resolve_timeout("example.com") == 15.0

    def test_subdomain_inherits_domain_timeout(self):
        self._set_config(30.0, {"example.com": {"timeout": 45.0}})
        assert self.server._resolve_timeout("www.example.com") == 45.0


# ---------------------------------------------------------------------------
# _resolve_proxy
# ---------------------------------------------------------------------------

class TestResolveProxy:

    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        import server
        self.server = server
        self.monkeypatch = monkeypatch

    def _set_config(self, global_proxy, domains=None):
        self.monkeypatch.setattr(
            self.server,
            "_CONFIG",
            {
                "global": {**self.server._DEFAULT_GLOBAL, "proxy": global_proxy},
                "domains": {k: v for k, v in (domains or {}).items()},
            },
        )

    def test_no_proxy_by_default(self):
        self._set_config(None)
        assert self.server._resolve_proxy("example.com") is None

    def test_global_proxy(self):
        self._set_config("http://proxy:8080")
        assert self.server._resolve_proxy("example.com") == "http://proxy:8080"

    def test_domain_proxy_override(self):
        self._set_config(None, {"example.com": {"proxy": "http://domain-proxy:3128"}})
        assert self.server._resolve_proxy("example.com") == "http://domain-proxy:3128"

    def test_domain_proxy_null_overrides_global(self):
        self._set_config("http://global:8080", {"example.com": {"proxy": None}})
        assert self.server._resolve_proxy("example.com") is None


# ---------------------------------------------------------------------------
# _resolve_retry
# ---------------------------------------------------------------------------

class TestResolveRetry:

    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        import server
        self.server = server
        self.monkeypatch = monkeypatch

    def _set_config(self, global_retry, domains=None):
        self.monkeypatch.setattr(
            self.server,
            "_CONFIG",
            {
                "global": {**self.server._DEFAULT_GLOBAL, "retry": global_retry},
                "domains": {k: v for k, v in (domains or {}).items()},
            },
        )

    def test_default_no_retry(self):
        self._set_config({"attempts": 1, "backoff": 2.0})
        result = self.server._resolve_retry("example.com")
        assert result == {"attempts": 1, "backoff": 2.0}

    def test_global_retry(self):
        self._set_config({"attempts": 3, "backoff": 1.5})
        result = self.server._resolve_retry("example.com")
        assert result == {"attempts": 3, "backoff": 1.5}

    def test_domain_retry_override(self):
        self._set_config(
            {"attempts": 1, "backoff": 2.0},
            {"example.com": {"retry": {"attempts": 5, "backoff": 3.0}}},
        )
        result = self.server._resolve_retry("example.com")
        assert result == {"attempts": 5, "backoff": 3.0}

    def test_domain_partial_retry_override(self):
        self._set_config(
            {"attempts": 3, "backoff": 2.0},
            {"example.com": {"retry": {"attempts": 5}}},
        )
        result = self.server._resolve_retry("example.com")
        assert result["attempts"] == 5
        assert result["backoff"] == 2.0


# ---------------------------------------------------------------------------
# _validate_headers
# ---------------------------------------------------------------------------

class TestValidateHeaders:

    @pytest.fixture(autouse=True)
    def setup(self):
        import server
        self.server = server

    def test_clean_headers_pass(self):
        self.server._validate_headers({"X-Token": "abc123", "User-Agent": "Bot/1"})

    def test_newline_in_value_raises(self):
        with pytest.raises(ValueError):
            self.server._validate_headers({"X-Injected": "value\r\nEvil: bad"})

    def test_carriage_return_in_name_raises(self):
        with pytest.raises(ValueError):
            self.server._validate_headers({"Bad\rName": "value"})

    def test_nul_byte_raises(self):
        with pytest.raises(ValueError):
            self.server._validate_headers({"X-Token": "val\x00ue"})

    def test_empty_headers_pass(self):
        self.server._validate_headers({})


# ---------------------------------------------------------------------------
# _extract_text
# ---------------------------------------------------------------------------

class TestExtractText:

    @pytest.fixture(autouse=True)
    def setup(self):
        import server
        self.server = server

    def test_strips_tags(self):
        result = self.server._extract_text("<p>Hello <b>world</b></p>")
        assert "<" not in result
        assert "Hello" in result
        assert "world" in result

    def test_collapses_whitespace(self):
        result = self.server._extract_text("<p>  lots   of   space  </p>")
        assert "  " not in result

    def test_empty_string(self):
        assert self.server._extract_text("") == ""

    def test_no_tags(self):
        result = self.server._extract_text("plain text here")
        assert result == "plain text here"

    def test_strips_tag_markers(self):
        result = self.server._extract_text("<script>alert(1)</script>")
        assert "<script>" not in result


# ---------------------------------------------------------------------------
# _apply_output_format — JSON
# ---------------------------------------------------------------------------

class TestApplyOutputFormatJson:

    @pytest.fixture(autouse=True)
    def setup(self):
        import server
        self.server = server

    def test_valid_json_pretty_prints(self):
        import json
        raw = '{"a":1,"b":[1,2,3]}'
        result = self.server._apply_output_format(raw, "json")
        parsed = json.loads(result)
        assert parsed == {"a": 1, "b": [1, 2, 3]}
        assert "\n" in result  # must be multi-line

    def test_invalid_json_returns_raw(self):
        raw = "<html>not json</html>"
        result = self.server._apply_output_format(raw, "json")
        assert result == raw

    def test_raw_format_unchanged(self):
        raw = "<html>body</html>"
        assert self.server._apply_output_format(raw, "raw") == raw


# ---------------------------------------------------------------------------
# fetch() tool — mocked httpx
# ---------------------------------------------------------------------------

class TestFetch:

    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        import server
        self.server = server
        monkeypatch.setattr(server, "_CONFIG", {
            "global": {**server._DEFAULT_GLOBAL},
            "domains": {},
        })

    async def test_basic_get_returns_status_and_body(self):
        response = _make_mock_response(200, "hello world")
        async_cm, _ = _make_async_client_mock(response)
        with patch("httpx.AsyncClient", return_value=async_cm):
            result = await self.server.fetch("http://example.com/")
        assert "--- Request Summary ---" in result
        assert "Status:           200 OK" in result
        assert "hello world" in result

    async def test_injected_headers_appear_in_output_and_are_sent(self, monkeypatch):
        monkeypatch.setattr(self.server, "_CONFIG", {
            "global": {**self.server._DEFAULT_GLOBAL},
            "domains": {"example.com": {"headers": {"X-Token": "tok123"}}},
        })
        response = _make_mock_response(200, "body")
        async_cm, client_mock = _make_async_client_mock(response)
        with patch("httpx.AsyncClient", return_value=async_cm):
            result = await self.server.fetch("http://example.com/page")
        assert "X-Token" in result
        call_kwargs = client_mock.request.call_args
        assert call_kwargs.kwargs["headers"].get("X-Token") == "tok123"

    async def test_extract_text_strips_html(self):
        response = _make_mock_response(200, "<p>clean text</p>")
        async_cm, _ = _make_async_client_mock(response)
        with patch("httpx.AsyncClient", return_value=async_cm):
            result = await self.server.fetch("http://example.com/", extract_text=True)
        assert "<p>" not in result
        assert "clean text" in result

    async def test_max_bytes_truncates(self):
        response = _make_mock_response(200, "A" * 1000)
        async_cm, _ = _make_async_client_mock(response)
        with patch("httpx.AsyncClient", return_value=async_cm):
            result = await self.server.fetch("http://example.com/", max_bytes=100)
        body = result.split("\n\n", 1)[1]
        assert len(body) <= 100

    async def test_follow_redirects_false_passed_to_client(self):
        response = _make_mock_response(200, "ok")
        async_cm, _ = _make_async_client_mock(response)
        with patch("httpx.AsyncClient", return_value=async_cm) as mock_cls:
            await self.server.fetch("http://example.com/", follow_redirects=False)
        call_kwargs = mock_cls.call_args.kwargs
        assert call_kwargs["follow_redirects"] is False

    async def test_follow_redirects_default_is_true(self):
        response = _make_mock_response(200, "ok")
        async_cm, _ = _make_async_client_mock(response)
        with patch("httpx.AsyncClient", return_value=async_cm) as mock_cls:
            await self.server.fetch("http://example.com/")
        call_kwargs = mock_cls.call_args.kwargs
        assert call_kwargs["follow_redirects"] is True

    async def test_header_injection_raises_value_error(self, monkeypatch):
        monkeypatch.setattr(
            self.server, "_CONFIG",
            {
                "global": {**self.server._DEFAULT_GLOBAL, "headers": {"X-Evil": "val\r\nHost: attacker.com"}},
                "domains": {},
            }
        )
        with pytest.raises(ValueError):
            await self.server.fetch("http://example.com/")

    async def test_no_headers_shows_none_in_output(self):
        response = _make_mock_response(200, "body")
        async_cm, _ = _make_async_client_mock(response)
        with patch("httpx.AsyncClient", return_value=async_cm):
            result = await self.server.fetch("http://example.com/")
        assert "Injected headers: none" in result

    async def test_summary_block_fields(self):
        response = _make_mock_response(200, "body text")
        async_cm, _ = _make_async_client_mock(response)
        with patch("httpx.AsyncClient", return_value=async_cm):
            result = await self.server.fetch("http://example.com/page", method="POST")
        assert "--- Request Summary ---" in result
        assert "URL:              http://example.com/page" in result
        assert "Method:           POST" in result
        assert "Status:           200 OK" in result
        assert f"Response size:    {len('body text')} bytes" in result
        assert "Text extracted:   no" in result
        assert "Truncated:        no" in result
        assert "Timeout:" in result
        assert "Proxy:" in result
        assert "Retry:" in result

    async def test_summary_shows_text_extracted_yes(self):
        response = _make_mock_response(200, "<p>hi</p>")
        async_cm, _ = _make_async_client_mock(response)
        with patch("httpx.AsyncClient", return_value=async_cm):
            result = await self.server.fetch("http://example.com/", extract_text=True)
        assert "Text extracted:   yes" in result

    async def test_summary_shows_truncated_yes(self):
        response = _make_mock_response(200, "A" * 1000)
        async_cm, _ = _make_async_client_mock(response)
        with patch("httpx.AsyncClient", return_value=async_cm):
            result = await self.server.fetch("http://example.com/", max_bytes=50)
        assert "Truncated:        yes (max_bytes=50)" in result
        assert "Response size:    1000 bytes" in result

    async def test_response_size_is_pre_truncation(self):
        response = _make_mock_response(200, "B" * 500)
        async_cm, _ = _make_async_client_mock(response)
        with patch("httpx.AsyncClient", return_value=async_cm):
            result = await self.server.fetch("http://example.com/", max_bytes=10)
        assert "Response size:    500 bytes" in result

    async def test_timeout_passed_to_client(self, monkeypatch):
        monkeypatch.setattr(self.server, "_CONFIG", {
            "global": {**self.server._DEFAULT_GLOBAL, "timeout": 42.0},
            "domains": {},
        })
        response = _make_mock_response(200, "ok")
        async_cm, _ = _make_async_client_mock(response)
        with patch("httpx.AsyncClient", return_value=async_cm) as mock_cls:
            await self.server.fetch("http://example.com/")
        call_kwargs = mock_cls.call_args.kwargs
        assert call_kwargs["timeout"] == 42.0

    async def test_proxy_passed_to_client(self, monkeypatch):
        monkeypatch.setattr(self.server, "_CONFIG", {
            "global": {**self.server._DEFAULT_GLOBAL, "proxy": "http://myproxy:8080"},
            "domains": {},
        })
        response = _make_mock_response(200, "ok")
        async_cm, _ = _make_async_client_mock(response)
        with patch("httpx.AsyncClient", return_value=async_cm) as mock_cls:
            await self.server.fetch("http://example.com/")
        call_kwargs = mock_cls.call_args.kwargs
        assert call_kwargs["proxy"] == "http://myproxy:8080"

    async def test_no_proxy_key_absent_from_client_call(self, monkeypatch):
        response = _make_mock_response(200, "ok")
        async_cm, _ = _make_async_client_mock(response)
        with patch("httpx.AsyncClient", return_value=async_cm) as mock_cls:
            await self.server.fetch("http://example.com/")
        call_kwargs = mock_cls.call_args.kwargs
        assert "proxy" not in call_kwargs

    async def test_json_content_type_auto_detected(self):
        import json
        payload = json.dumps({"key": "value"})
        response = _make_mock_response(200, payload, content_type="application/json; charset=utf-8")
        async_cm, _ = _make_async_client_mock(response)
        with patch("httpx.AsyncClient", return_value=async_cm):
            result = await self.server.fetch("http://api.example.com/data")
        assert '"key": "value"' in result
        assert "Output format:    json" in result

    async def test_output_format_json_explicit(self):
        import json
        payload = json.dumps({"x": 1})
        response = _make_mock_response(200, payload)
        async_cm, _ = _make_async_client_mock(response)
        with patch("httpx.AsyncClient", return_value=async_cm):
            result = await self.server.fetch("http://example.com/", output_format="json")
        assert '"x": 1' in result


# ---------------------------------------------------------------------------
# fetch() — retry behaviour
# ---------------------------------------------------------------------------

class TestFetchRetry:

    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        import server
        self.server = server
        self.monkeypatch = monkeypatch

    def _set_retry_config(self, attempts, backoff=2.0):
        self.monkeypatch.setattr(self.server, "_CONFIG", {
            "global": {
                **self.server._DEFAULT_GLOBAL,
                "retry": {"attempts": attempts, "backoff": backoff},
            },
            "domains": {},
        })

    async def test_no_retry_on_success(self):
        self._set_retry_config(3)
        response = _make_mock_response(200, "ok")
        async_cm, client_mock = _make_async_client_mock(response)
        with patch("httpx.AsyncClient", return_value=async_cm), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await self.server.fetch("http://example.com/")
        mock_sleep.assert_not_called()
        assert client_mock.request.call_count == 1

    async def test_retries_on_500(self):
        self._set_retry_config(3)
        bad_response = _make_mock_response(500, "error")
        good_response = _make_mock_response(200, "ok")
        client_mock = AsyncMock()
        client_mock.request = AsyncMock(side_effect=[bad_response, bad_response, good_response])
        async_cm = MagicMock()
        async_cm.__aenter__ = AsyncMock(return_value=client_mock)
        async_cm.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=async_cm), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await self.server.fetch("http://example.com/")
        assert "200 OK" in result
        assert client_mock.request.call_count == 3

    async def test_returns_last_500_when_all_attempts_fail(self):
        self._set_retry_config(2)
        bad_response = _make_mock_response(500, "server error")
        client_mock = AsyncMock()
        client_mock.request = AsyncMock(return_value=bad_response)
        async_cm = MagicMock()
        async_cm.__aenter__ = AsyncMock(return_value=client_mock)
        async_cm.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=async_cm), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await self.server.fetch("http://example.com/")
        assert "500" in result
        assert client_mock.request.call_count == 2

    async def test_reraises_transport_error_after_all_attempts(self):
        import httpx as _httpx
        self._set_retry_config(2)
        client_mock = AsyncMock()
        client_mock.request = AsyncMock(side_effect=_httpx.TransportError("connection refused"))
        async_cm = MagicMock()
        async_cm.__aenter__ = AsyncMock(return_value=client_mock)
        async_cm.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=async_cm), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(_httpx.TransportError):
                await self.server.fetch("http://example.com/")
        assert client_mock.request.call_count == 2

    async def test_retry_disabled_shows_in_summary(self):
        self._set_retry_config(1)
        response = _make_mock_response(200, "ok")
        async_cm, _ = _make_async_client_mock(response)
        with patch("httpx.AsyncClient", return_value=async_cm):
            result = await self.server.fetch("http://example.com/")
        assert "Retry:            disabled" in result

    async def test_retry_enabled_shows_attempts_in_summary(self):
        self._set_retry_config(3)
        response = _make_mock_response(200, "ok")
        async_cm, _ = _make_async_client_mock(response)
        with patch("httpx.AsyncClient", return_value=async_cm):
            result = await self.server.fetch("http://example.com/")
        assert "Retry:            1/3" in result
