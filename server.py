"""
WebFetch MCP Server
Replaces Claude's built-in WebFetch tool with support for domain-scoped
custom HTTP headers (e.g. Akamai bot-defender authentication).
"""

import json
import logging
import os
import re
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

mcp = FastMCP("webfetch")
_log = logging.getLogger(__name__)


def _load_header_config() -> dict[str, dict[str, str]]:
    """
    Load WEBFETCH_HEADERS from the environment.

    Expected format: a JSON object whose keys are domain suffixes or "*" (global).
    Example:
        {
          "*":           {"User-Agent": "MyBot/1.0"},
          "example.com":  {"X-Akamai-Token": "token-for-example"},
          "example2.com": {"X-Akamai-Token": "token-for-example2"}
        }

    Returns an empty dict if the variable is missing or invalid.
    """
    raw = os.getenv("WEBFETCH_HEADERS", "")
    if not raw:
        return {}
    try:
        config = json.loads(raw)
        if not isinstance(config, dict):
            raise ValueError("WEBFETCH_HEADERS must be a JSON object")
        return config
    except (json.JSONDecodeError, ValueError) as exc:
        _log.error("Failed to parse WEBFETCH_HEADERS: %s", exc)
        raise RuntimeError(f"Invalid WEBFETCH_HEADERS value: {exc}") from exc


# Load once at startup
_HEADER_CONFIG: dict[str, dict[str, str]] = _load_header_config()
_log.info(
    "webfetch startup: %d domain(s) configured",
    len([k for k in _HEADER_CONFIG if k != "*"]),
)

_VALID_OUTPUT_FORMATS = frozenset({"raw", "markdown", "trafilatura"})


def _load_output_config() -> dict[str, str]:
    """
    Load WEBFETCH_OUTPUT from the environment.

    Expected format: a JSON object whose keys are domain suffixes or "*" (global),
    and whose values are output format strings: "raw", "markdown", or "trafilatura".
    Example:
        {
          "*":           "raw",
          "example.com": "trafilatura",
          "news.com":    "markdown"
        }

    Returns an empty dict if the variable is missing.
    """
    raw = os.getenv("WEBFETCH_OUTPUT", "")
    if not raw:
        return {}
    try:
        config = json.loads(raw)
        if not isinstance(config, dict):
            raise ValueError("WEBFETCH_OUTPUT must be a JSON object")
        for domain_key, fmt in config.items():
            if fmt not in _VALID_OUTPUT_FORMATS:
                raise ValueError(
                    f"Invalid output format {fmt!r} for key {domain_key!r}. "
                    f"Must be one of: {sorted(_VALID_OUTPUT_FORMATS)}"
                )
        return config
    except (json.JSONDecodeError, ValueError) as exc:
        _log.error("Failed to parse WEBFETCH_OUTPUT: %s", exc)
        raise RuntimeError(f"Invalid WEBFETCH_OUTPUT value: {exc}") from exc


_OUTPUT_CONFIG: dict[str, str] = _load_output_config()
_log.info(
    "webfetch startup: %d output format domain(s) configured",
    len([k for k in _OUTPUT_CONFIG if k != "*"]),
)


def _resolve_headers(hostname: str, extra_headers: dict[str, str] | None) -> dict[str, str]:
    """
    Build the final headers dict for a given hostname.

    Merge order (later entries win on key conflict):
      1. Global headers ("*")
      2. Most-specific matching domain headers
      3. Per-call extra_headers
    """
    headers: dict[str, str] = {}

    # 1. Global headers
    headers.update(_HEADER_CONFIG.get("*", {}))

    # 2. Domain-specific headers — find all matching keys, sort by specificity
    #    (longer key = more specific), apply in ascending order so the most
    #    specific one wins last.
    matching = [
        key for key in _HEADER_CONFIG
        if key != "*" and (hostname == key or hostname.endswith("." + key))
    ]
    matching.sort(key=len)
    for domain_key in matching:
        headers.update(_HEADER_CONFIG[domain_key])

    # 3. Per-call headers
    if extra_headers:
        headers.update(extra_headers)

    return headers


def _resolve_output_format(hostname: str, per_call_format: str | None) -> str:
    """
    Determine the effective output format for a given hostname.

    Precedence (later wins):
      1. "raw" (hardcoded default)
      2. Global config ("*") from WEBFETCH_OUTPUT
      3. Most-specific matching domain config (longer key = more specific)
      4. per_call_format argument (None means "don't override")

    Note: extract_text=True is handled separately in fetch() and always wins.
    """
    fmt = _OUTPUT_CONFIG.get("*", "raw")

    matching = [
        key for key in _OUTPUT_CONFIG
        if key != "*" and (hostname == key or hostname.endswith("." + key))
    ]
    matching.sort(key=len)
    for domain_key in matching:
        fmt = _OUTPUT_CONFIG[domain_key]

    if per_call_format is not None:
        fmt = per_call_format

    return fmt


def _apply_output_format(content: str, fmt: str) -> str:
    """
    Convert *content* (raw HTML string) to the requested output format.

    Formats:
      "raw"         — return as-is
      "text"        — regex tag-strip (internal alias for extract_text=True)
      "markdown"    — convert full HTML to Markdown via markdownify
      "trafilatura" — extract main content as Markdown via trafilatura;
                      falls back to raw HTML if trafilatura returns None
    """
    if fmt == "raw":
        return content
    if fmt == "text":
        return _extract_text(content)
    if fmt == "markdown":
        import markdownify  # lazy import: only needed when format is used
        return markdownify.markdownify(content, strip=["script", "style"])
    if fmt == "trafilatura":
        import trafilatura  # lazy import: only needed when format is used
        extracted = trafilatura.extract(content, output_format="markdown")
        if extracted is None:
            _log.warning("trafilatura returned None; falling back to raw HTML")
            return content
        return extracted
    _log.error("Unknown output format %r; returning raw content", fmt)
    return content


def _extract_text(html: str) -> str:
    """Strip HTML tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


_INVALID_HEADER_RE = re.compile(r"[\r\n\x00]")


def _validate_headers(headers: dict[str, str]) -> None:
    """Raise ValueError if any header name or value contains \\r, \\n, or NUL."""
    for name, value in headers.items():
        if _INVALID_HEADER_RE.search(name):
            raise ValueError(f"Invalid header name contains control character: {name!r}")
        if _INVALID_HEADER_RE.search(str(value)):
            raise ValueError(f"Invalid value for header {name!r} contains control character")


@mcp.tool()
async def fetch(
    url: str,
    method: str = "GET",
    body: str | None = None,
    extra_headers: dict | None = None,
    extract_text: bool = False,
    max_bytes: int = 0,
    follow_redirects: bool = True,
    output_format: str | None = None,
) -> str:
    """
    Fetch a URL and return its response, injecting domain-scoped authentication
    headers (e.g. Akamai bot-defender tokens) automatically.

    Args:
        url:              The URL to request.
        method:           HTTP method (GET, POST, PUT, DELETE, …). Default: GET.
        body:             Optional request body string (for POST/PUT).
        extra_headers:    Additional headers to send for this request only.
                          These are merged on top of the base domain headers.
        extract_text:     If True, strip HTML tags and return clean readable text.
                          Legacy parameter — takes priority over output_format.
        max_bytes:        Truncate the response body to this many characters.
                          0 means no limit.
        follow_redirects: Follow HTTP redirects automatically. Default: True.
        output_format:    Override the output format for this request only.
                          Accepted values: "raw" (default), "markdown", "trafilatura".
                          Takes precedence over WEBFETCH_OUTPUT domain config.
                          Ignored if extract_text=True is also passed (legacy compat).
    """
    parsed = urlparse(url)
    hostname = parsed.hostname or ""

    if output_format is not None and output_format not in _VALID_OUTPUT_FORMATS:
        raise ValueError(
            f"Invalid output_format {output_format!r}. "
            f"Must be one of: {sorted(_VALID_OUTPUT_FORMATS)}"
        )

    headers = _resolve_headers(hostname, extra_headers)
    _validate_headers(headers)
    applied_header_names = list(headers.keys())

    _log.info(
        "fetch %s %s (hostname=%s, injected=%s)",
        method.upper(),
        url,
        hostname,
        applied_header_names or "none",
    )

    async with httpx.AsyncClient(follow_redirects=follow_redirects) as client:
        response = await client.request(
            method=method.upper(),
            url=url,
            headers=headers,
            content=body.encode() if body else None,
        )

    if response.is_error:
        _log.warning("fetch %s %s returned HTTP %s", method.upper(), url, response.status_code)

    content = response.text

    # extract_text=True is the legacy override; it wins over output_format and domain config.
    if extract_text:
        effective_fmt = "text"
    else:
        effective_fmt = _resolve_output_format(hostname, output_format)

    content = _apply_output_format(content, effective_fmt)

    if max_bytes > 0:
        content = content[:max_bytes]

    injected = ", ".join(applied_header_names) if applied_header_names else "none"
    return f"Status: {response.status_code}\nInjected headers: {injected}\n\n{content}"


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    mcp.run()
