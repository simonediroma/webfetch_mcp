"""
Tests for server.py

Run with:
    pytest tests/test_server.py -v
"""
import importlib
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reload_server(monkeypatch, env_value, output_value=None):
    """
    Re-import server with controlled WEBFETCH_HEADERS and WEBFETCH_OUTPUT env values.
    Necessary because _HEADER_CONFIG and _OUTPUT_CONFIG are set at module load time.
    """
    if env_value is None:
        monkeypatch.delenv("WEBFETCH_HEADERS", raising=False)
    else:
        monkeypatch.setenv("WEBFETCH_HEADERS", env_value)
    if output_value is None:
        monkeypatch.delenv("WEBFETCH_OUTPUT", raising=False)
    else:
        monkeypatch.setenv("WEBFETCH_OUTPUT", output_value)
    # Remove cached module so it fully re-executes on import
    sys.modules.pop("server", None)
    import server
    return server


def _make_mock_response(status_code=200, text="<html>hello</html>"):
    response = MagicMock()
    response.status_code = status_code
    response.text = text
    response.is_error = status_code >= 400
    return response


def _make_async_client_mock(response):
    client_mock = AsyncMock()
    client_mock.request = AsyncMock(return_value=response)
    async_cm = MagicMock()
    async_cm.__aenter__ = AsyncMock(return_value=client_mock)
    async_cm.__aexit__ = AsyncMock(return_value=False)
    return async_cm, client_mock


# ---------------------------------------------------------------------------
# _load_header_config
# ---------------------------------------------------------------------------

class TestLoadHeaderConfig:

    def test_valid_json(self, monkeypatch):
        srv = _reload_server(monkeypatch, '{"*": {"User-Agent": "Bot"}}')
        assert srv._HEADER_CONFIG == {"*": {"User-Agent": "Bot"}}

    def test_missing_env_var_returns_empty(self, monkeypatch):
        srv = _reload_server(monkeypatch, None)
        assert srv._HEADER_CONFIG == {}

    def test_invalid_json_raises(self, monkeypatch):
        monkeypatch.setenv("WEBFETCH_HEADERS", "not-valid-json{{{")
        sys.modules.pop("server", None)
        with pytest.raises((RuntimeError, Exception)):
            import server  # noqa: F401

    def test_non_dict_json_raises(self, monkeypatch):
        monkeypatch.setenv("WEBFETCH_HEADERS", '["a", "b"]')
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

    def _set_config(self, config):
        self.monkeypatch.setattr(self.server, "_HEADER_CONFIG", config)

    def test_global_only(self):
        self._set_config({"*": {"User-Agent": "GlobalBot"}})
        result = self.server._resolve_headers("example.com", None)
        assert result == {"User-Agent": "GlobalBot"}

    def test_domain_match_exact(self):
        self._set_config({"*": {"User-Agent": "Bot"}, "example.com": {"X-Token": "abc"}})
        result = self.server._resolve_headers("example.com", None)
        assert result["X-Token"] == "abc"
        assert result["User-Agent"] == "Bot"

    def test_subdomain_match(self):
        self._set_config({"example.com": {"X-Token": "abc"}})
        result = self.server._resolve_headers("sub.example.com", None)
        assert result["X-Token"] == "abc"

    def test_subdomain_does_not_match_unrelated_domain(self):
        self._set_config({"example.com": {"X-Token": "abc"}})
        result = self.server._resolve_headers("notexample.com", None)
        assert "X-Token" not in result

    def test_specificity_order_longer_key_wins(self):
        self._set_config({
            "example.com": {"X-Token": "base"},
            "sub.example.com": {"X-Token": "specific"},
        })
        result = self.server._resolve_headers("sub.example.com", None)
        assert result["X-Token"] == "specific"

    def test_per_call_override_wins_over_domain(self):
        self._set_config({"example.com": {"X-Token": "domain-value"}})
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
# fetch() tool — mocked httpx
# ---------------------------------------------------------------------------

class TestFetch:

    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        import server
        self.server = server
        monkeypatch.setattr(server, "_HEADER_CONFIG", {})

    async def test_basic_get_returns_status_and_body(self):
        response = _make_mock_response(200, "hello world")
        async_cm, _ = _make_async_client_mock(response)
        with patch("httpx.AsyncClient", return_value=async_cm):
            result = await self.server.fetch("http://example.com/")
        assert "Status: 200" in result
        assert "hello world" in result

    async def test_injected_headers_appear_in_output_and_are_sent(self, monkeypatch):
        monkeypatch.setattr(self.server, "_HEADER_CONFIG", {"example.com": {"X-Token": "tok123"}})
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
        mock_cls.assert_called_once_with(follow_redirects=False)

    async def test_follow_redirects_default_is_true(self):
        response = _make_mock_response(200, "ok")
        async_cm, _ = _make_async_client_mock(response)
        with patch("httpx.AsyncClient", return_value=async_cm) as mock_cls:
            await self.server.fetch("http://example.com/")
        mock_cls.assert_called_once_with(follow_redirects=True)

    async def test_header_injection_raises_value_error(self, monkeypatch):
        monkeypatch.setattr(
            self.server, "_HEADER_CONFIG",
            {"*": {"X-Evil": "val\r\nHost: attacker.com"}}
        )
        with pytest.raises(ValueError):
            await self.server.fetch("http://example.com/")

    async def test_no_headers_shows_none_in_output(self):
        response = _make_mock_response(200, "body")
        async_cm, _ = _make_async_client_mock(response)
        with patch("httpx.AsyncClient", return_value=async_cm):
            result = await self.server.fetch("http://example.com/")
        assert "Injected headers: none" in result

    async def test_output_format_raw_returns_html(self):
        html = "<p>hello</p>"
        response = _make_mock_response(200, html)
        async_cm, _ = _make_async_client_mock(response)
        with patch("httpx.AsyncClient", return_value=async_cm):
            result = await self.server.fetch("http://example.com/", output_format="raw")
        assert "<p>hello</p>" in result

    async def test_output_format_markdown_applied(self):
        response = _make_mock_response(200, "<h1>Title</h1>")
        async_cm, _ = _make_async_client_mock(response)
        with patch("httpx.AsyncClient", return_value=async_cm):
            with patch("markdownify.markdownify", return_value="# Title\n"):
                result = await self.server.fetch("http://example.com/", output_format="markdown")
        assert "# Title" in result

    async def test_output_format_trafilatura_applied(self):
        response = _make_mock_response(200, "<html><body><article>Content</article></body></html>")
        async_cm, _ = _make_async_client_mock(response)
        with patch("httpx.AsyncClient", return_value=async_cm):
            with patch("trafilatura.extract", return_value="# Content\n"):
                result = await self.server.fetch("http://example.com/", output_format="trafilatura")
        assert "# Content" in result

    async def test_output_format_domain_config_applied(self, monkeypatch):
        monkeypatch.setattr(self.server, "_OUTPUT_CONFIG", {"example.com": "markdown"})
        response = _make_mock_response(200, "<h1>Title</h1>")
        async_cm, _ = _make_async_client_mock(response)
        with patch("httpx.AsyncClient", return_value=async_cm):
            with patch("markdownify.markdownify", return_value="# Title\n") as mock_md:
                await self.server.fetch("http://example.com/")
        mock_md.assert_called_once()

    async def test_extract_text_true_overrides_output_format(self):
        response = _make_mock_response(200, "<p>clean text</p>")
        async_cm, _ = _make_async_client_mock(response)
        with patch("httpx.AsyncClient", return_value=async_cm):
            with patch("markdownify.markdownify") as mock_md:
                result = await self.server.fetch(
                    "http://example.com/", extract_text=True, output_format="markdown"
                )
        mock_md.assert_not_called()
        assert "<p>" not in result
        assert "clean text" in result

    async def test_invalid_output_format_raises_value_error(self):
        response = _make_mock_response(200, "body")
        async_cm, _ = _make_async_client_mock(response)
        with patch("httpx.AsyncClient", return_value=async_cm):
            with pytest.raises(ValueError, match="Invalid output_format"):
                await self.server.fetch("http://example.com/", output_format="html")


# ---------------------------------------------------------------------------
# _load_output_config
# ---------------------------------------------------------------------------

class TestLoadOutputConfig:

    def test_valid_json(self, monkeypatch):
        srv = _reload_server(monkeypatch, None, '{"*": "raw", "example.com": "trafilatura"}')
        assert srv._OUTPUT_CONFIG == {"*": "raw", "example.com": "trafilatura"}

    def test_missing_env_var_returns_empty(self, monkeypatch):
        srv = _reload_server(monkeypatch, None, None)
        assert srv._OUTPUT_CONFIG == {}

    def test_invalid_json_raises(self, monkeypatch):
        monkeypatch.setenv("WEBFETCH_OUTPUT", "not-valid{{{")
        sys.modules.pop("server", None)
        with pytest.raises((RuntimeError, Exception)):
            import server  # noqa: F401

    def test_non_dict_json_raises(self, monkeypatch):
        monkeypatch.setenv("WEBFETCH_OUTPUT", '["raw", "markdown"]')
        sys.modules.pop("server", None)
        with pytest.raises((RuntimeError, Exception)):
            import server  # noqa: F401

    def test_invalid_format_value_raises(self, monkeypatch):
        monkeypatch.setenv("WEBFETCH_OUTPUT", '{"*": "html"}')
        sys.modules.pop("server", None)
        with pytest.raises((RuntimeError, Exception)):
            import server  # noqa: F401

    def test_text_mode_not_accepted_as_config_value(self, monkeypatch):
        # "text" is internal only and must not be accepted in WEBFETCH_OUTPUT
        monkeypatch.setenv("WEBFETCH_OUTPUT", '{"*": "text"}')
        sys.modules.pop("server", None)
        with pytest.raises((RuntimeError, Exception)):
            import server  # noqa: F401

    def test_all_valid_formats_accepted(self, monkeypatch):
        cfg = '{"*": "raw", "a.com": "markdown", "b.com": "trafilatura"}'
        srv = _reload_server(monkeypatch, None, cfg)
        assert srv._OUTPUT_CONFIG["*"] == "raw"
        assert srv._OUTPUT_CONFIG["a.com"] == "markdown"
        assert srv._OUTPUT_CONFIG["b.com"] == "trafilatura"


# ---------------------------------------------------------------------------
# _resolve_output_format
# ---------------------------------------------------------------------------

class TestResolveOutputFormat:

    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        import server
        self.server = server
        self.monkeypatch = monkeypatch

    def _set_output_config(self, config):
        self.monkeypatch.setattr(self.server, "_OUTPUT_CONFIG", config)

    def test_default_is_raw(self):
        self._set_output_config({})
        assert self.server._resolve_output_format("example.com", None) == "raw"

    def test_global_wildcard_applies(self):
        self._set_output_config({"*": "trafilatura"})
        assert self.server._resolve_output_format("example.com", None) == "trafilatura"

    def test_domain_match_exact(self):
        self._set_output_config({"*": "raw", "example.com": "markdown"})
        assert self.server._resolve_output_format("example.com", None) == "markdown"

    def test_subdomain_match(self):
        self._set_output_config({"example.com": "trafilatura"})
        assert self.server._resolve_output_format("www.example.com", None) == "trafilatura"

    def test_subdomain_no_false_match(self):
        self._set_output_config({"example.com": "trafilatura"})
        assert self.server._resolve_output_format("notexample.com", None) == "raw"

    def test_specificity_longer_key_wins(self):
        self._set_output_config({
            "example.com": "markdown",
            "sub.example.com": "trafilatura",
        })
        assert self.server._resolve_output_format("sub.example.com", None) == "trafilatura"

    def test_per_call_format_overrides_domain(self):
        self._set_output_config({"example.com": "trafilatura"})
        assert self.server._resolve_output_format("example.com", "markdown") == "markdown"

    def test_per_call_none_does_not_override(self):
        self._set_output_config({"example.com": "trafilatura"})
        assert self.server._resolve_output_format("example.com", None) == "trafilatura"


# ---------------------------------------------------------------------------
# _apply_output_format
# ---------------------------------------------------------------------------

class TestApplyOutputFormat:

    @pytest.fixture(autouse=True)
    def setup(self):
        import server
        self.server = server

    def test_raw_returns_unchanged(self):
        html = "<p>hello</p>"
        assert self.server._apply_output_format(html, "raw") == html

    def test_text_strips_tags(self):
        result = self.server._apply_output_format("<p>hello <b>world</b></p>", "text")
        assert "<" not in result
        assert "hello" in result and "world" in result

    def test_markdown_calls_markdownify(self):
        html = "<h1>Title</h1><p>Body</p>"
        with patch("markdownify.markdownify", return_value="# Title\n\nBody") as mock_md:
            result = self.server._apply_output_format(html, "markdown")
        mock_md.assert_called_once_with(html, strip=["script", "style"])
        assert result == "# Title\n\nBody"

    def test_trafilatura_returns_extracted(self):
        html = "<html><body><article>Content</article></body></html>"
        with patch("trafilatura.extract", return_value="## Content\n") as mock_tf:
            result = self.server._apply_output_format(html, "trafilatura")
        mock_tf.assert_called_once_with(html, output_format="markdown")
        assert result == "## Content\n"

    def test_trafilatura_none_falls_back_to_raw(self):
        html = "<html><body></body></html>"
        with patch("trafilatura.extract", return_value=None):
            result = self.server._apply_output_format(html, "trafilatura")
        assert result == html

    def test_unknown_format_returns_raw(self):
        html = "<p>hello</p>"
        result = self.server._apply_output_format(html, "unknown_format")
        assert result == html
