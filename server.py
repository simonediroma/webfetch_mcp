"""
WebFetch MCP Server
Replaces Claude's built-in WebFetch tool with support for domain-scoped
custom HTTP headers (e.g. Akamai bot-defender authentication).
"""

import json
import os
import re
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

mcp = FastMCP("webfetch")


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
        raise RuntimeError(f"Invalid WEBFETCH_HEADERS value: {exc}") from exc


# Load once at startup
_HEADER_CONFIG: dict[str, dict[str, str]] = _load_header_config()


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


def _extract_text(html: str) -> str:
    """Strip HTML tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


@mcp.tool()
async def fetch(
    url: str,
    method: str = "GET",
    body: str | None = None,
    extra_headers: dict | None = None,
    extract_text: bool = False,
    max_bytes: int = 0,
) -> str:
    """
    Fetch a URL and return its response, injecting domain-scoped authentication
    headers (e.g. Akamai bot-defender tokens) automatically.

    Args:
        url:           The URL to request.
        method:        HTTP method (GET, POST, PUT, DELETE, …). Default: GET.
        body:          Optional request body string (for POST/PUT).
        extra_headers: Additional headers to send for this request only.
                       These are merged on top of the base domain headers.
        extract_text:  If True, strip HTML tags and return clean readable text.
        max_bytes:     Truncate the response body to this many characters.
                       0 means no limit.
    """
    parsed = urlparse(url)
    hostname = parsed.hostname or ""

    headers = _resolve_headers(hostname, extra_headers)
    applied_header_names = list(headers.keys())

    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.request(
            method=method.upper(),
            url=url,
            headers=headers,
            content=body.encode() if body else None,
        )

    content = response.text

    if extract_text:
        content = _extract_text(content)

    if max_bytes > 0:
        content = content[:max_bytes]

    injected = ", ".join(applied_header_names) if applied_header_names else "none"
    return f"Status: {response.status_code}\nInjected headers: {injected}\n\n{content}"


if __name__ == "__main__":
    mcp.run()
