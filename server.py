"""
WebFetch MCP Server
Replaces Claude's built-in WebFetch tool with support for domain-scoped
custom HTTP headers (e.g. Akamai bot-defender authentication), retry logic,
configurable timeouts, per-domain proxies, and flexible output formats.

Configuration is loaded from a YAML file (WEBFETCH_CONFIG env var) or falls
back to the legacy WEBFETCH_HEADERS / WEBFETCH_OUTPUT environment variables.
"""

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

mcp = FastMCP("webfetch")
_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_OUTPUT_FORMATS = frozenset({"raw", "markdown", "trafilatura", "json"})

_DEFAULT_GLOBAL: dict = {
    "headers": {},
    "output_format": "raw",
    "timeout": 30.0,
    "retry": {"attempts": 1, "backoff": 2.0},
    "proxy": None,
}

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    """
    Load configuration from a YAML file or fall back to environment variables.

    Priority:
      1. Hardcoded defaults
      2. Environment variables (WEBFETCH_HEADERS, WEBFETCH_OUTPUT) — legacy
      3. YAML file pointed to by WEBFETCH_CONFIG — fully overrides env vars

    Returns a dict with keys "global" and "domains".
    """
    yaml_path_str = os.getenv("WEBFETCH_CONFIG", "")
    if yaml_path_str:
        return _load_yaml_config(yaml_path_str)
    return _load_env_config()


def _load_yaml_config(path_str: str) -> dict:
    """Load and validate config from a YAML file."""
    import yaml  # lazy import: only needed when YAML config is used

    path = Path(path_str)
    if not path.exists():
        raise RuntimeError(f"WEBFETCH_CONFIG file not found: {path}")

    try:
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Invalid YAML in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise RuntimeError(f"YAML config must be a mapping, got {type(raw).__name__}")

    return _normalise_config(raw)


def _normalise_config(raw: dict) -> dict:
    """Validate and normalise a raw YAML/dict config into the canonical form."""
    config: dict = {"global": dict(_DEFAULT_GLOBAL), "domains": {}}
    config["global"]["retry"] = dict(_DEFAULT_GLOBAL["retry"])

    raw_global = raw.get("global", {})
    if not isinstance(raw_global, dict):
        raise RuntimeError("'global' section must be a mapping")

    _merge_domain_section(config["global"], raw_global, context="global")

    raw_domains = raw.get("domains", {})
    if not isinstance(raw_domains, dict):
        raise RuntimeError("'domains' section must be a mapping")

    for domain_key, domain_val in raw_domains.items():
        if domain_val is None:
            domain_val = {}
        if not isinstance(domain_val, dict):
            raise RuntimeError(f"Domain entry {domain_key!r} must be a mapping")
        section: dict = {}
        _merge_domain_section(section, domain_val, context=f"domains.{domain_key}")
        config["domains"][domain_key] = section

    return config


def _merge_domain_section(target: dict, source: dict, *, context: str) -> None:
    """Copy recognised fields from *source* into *target*, validating each."""
    if "headers" in source:
        val = source["headers"]
        if not isinstance(val, dict):
            raise RuntimeError(f"'{context}.headers' must be a mapping")
        target["headers"] = {str(k): str(v) for k, v in val.items()}

    if "output_format" in source:
        val = source["output_format"]
        if val not in _VALID_OUTPUT_FORMATS:
            raise RuntimeError(
                f"'{context}.output_format' is {val!r}; "
                f"must be one of {sorted(_VALID_OUTPUT_FORMATS)}"
            )
        target["output_format"] = val

    if "timeout" in source:
        val = source["timeout"]
        try:
            target["timeout"] = float(val)
        except (TypeError, ValueError):
            raise RuntimeError(f"'{context}.timeout' must be a number, got {val!r}")

    if "proxy" in source:
        val = source["proxy"]
        target["proxy"] = str(val) if val is not None else None

    if "retry" in source:
        val = source["retry"]
        if not isinstance(val, dict):
            raise RuntimeError(f"'{context}.retry' must be a mapping")
        retry: dict = {}
        if "attempts" in val:
            try:
                retry["attempts"] = int(val["attempts"])
            except (TypeError, ValueError):
                raise RuntimeError(
                    f"'{context}.retry.attempts' must be an integer"
                )
        if "backoff" in val:
            try:
                retry["backoff"] = float(val["backoff"])
            except (TypeError, ValueError):
                raise RuntimeError(
                    f"'{context}.retry.backoff' must be a number"
                )
        target["retry"] = retry


def _load_env_config() -> dict:
    """Build the canonical config dict from legacy environment variables."""
    config: dict = {
        "global": {
            "headers": {},
            "output_format": "raw",
            "timeout": 30.0,
            "retry": {"attempts": 1, "backoff": 2.0},
            "proxy": None,
        },
        "domains": {},
    }

    # --- WEBFETCH_HEADERS ---
    raw_headers = os.getenv("WEBFETCH_HEADERS", "")
    if raw_headers:
        try:
            header_cfg = json.loads(raw_headers)
            if not isinstance(header_cfg, dict):
                raise ValueError("WEBFETCH_HEADERS must be a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(f"Invalid WEBFETCH_HEADERS: {exc}") from exc

        if "*" in header_cfg:
            config["global"]["headers"] = header_cfg["*"]
        for key, val in header_cfg.items():
            if key == "*":
                continue
            config["domains"].setdefault(key, {})["headers"] = val

    # --- WEBFETCH_OUTPUT ---
    raw_output = os.getenv("WEBFETCH_OUTPUT", "")
    if raw_output:
        try:
            output_cfg = json.loads(raw_output)
            if not isinstance(output_cfg, dict):
                raise ValueError("WEBFETCH_OUTPUT must be a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(f"Invalid WEBFETCH_OUTPUT: {exc}") from exc

        for key, fmt in output_cfg.items():
            if fmt not in _VALID_OUTPUT_FORMATS:
                raise RuntimeError(
                    f"Invalid output format {fmt!r} for key {key!r}. "
                    f"Must be one of: {sorted(_VALID_OUTPUT_FORMATS)}"
                )
            if key == "*":
                config["global"]["output_format"] = fmt
            else:
                config["domains"].setdefault(key, {})["output_format"] = fmt

    return config


# Load once at startup
_CONFIG: dict = _load_config()
_log.info(
    "webfetch startup: %d domain(s) configured (source: %s)",
    len(_CONFIG["domains"]),
    "YAML" if os.getenv("WEBFETCH_CONFIG") else "env",
)

# ---------------------------------------------------------------------------
# Domain-matching resolution helpers
# ---------------------------------------------------------------------------

def _matching_domain_keys(hostname: str, domains: dict) -> list[str]:
    """Return domain keys that match *hostname*, sorted shortest-first."""
    matches = [
        key for key in domains
        if hostname == key or hostname.endswith("." + key)
    ]
    matches.sort(key=len)
    return matches


def _resolve_headers(hostname: str, extra_headers: dict[str, str] | None) -> dict[str, str]:
    """
    Build the final headers dict for a given hostname.

    Merge order (later entries win):
      1. Global headers
      2. Most-specific matching domain headers
      3. Per-call extra_headers
    """
    headers: dict[str, str] = {}
    headers.update(_CONFIG["global"].get("headers", {}))

    for key in _matching_domain_keys(hostname, _CONFIG["domains"]):
        headers.update(_CONFIG["domains"][key].get("headers", {}))

    if extra_headers:
        headers.update(extra_headers)

    return headers


def _resolve_output_format(hostname: str, per_call_format: str | None) -> str:
    """
    Determine the effective output format.

    Precedence (later wins):
      1. Global config output_format
      2. Most-specific matching domain output_format
      3. per_call_format (None = don't override)
    """
    fmt = _CONFIG["global"].get("output_format", "raw")

    for key in _matching_domain_keys(hostname, _CONFIG["domains"]):
        domain_fmt = _CONFIG["domains"][key].get("output_format")
        if domain_fmt is not None:
            fmt = domain_fmt

    if per_call_format is not None:
        fmt = per_call_format

    return fmt


def _resolve_timeout(hostname: str) -> float:
    """Return the effective timeout (seconds) for *hostname*."""
    timeout = _CONFIG["global"].get("timeout", 30.0)

    for key in _matching_domain_keys(hostname, _CONFIG["domains"]):
        domain_timeout = _CONFIG["domains"][key].get("timeout")
        if domain_timeout is not None:
            timeout = domain_timeout

    return float(timeout)


def _resolve_proxy(hostname: str) -> str | None:
    """Return the effective proxy URL for *hostname*, or None."""
    proxy = _CONFIG["global"].get("proxy")

    for key in _matching_domain_keys(hostname, _CONFIG["domains"]):
        domain = _CONFIG["domains"][key]
        if "proxy" in domain:
            proxy = domain["proxy"]

    return proxy


def _resolve_retry(hostname: str) -> dict:
    """Return the effective retry config for *hostname*."""
    global_retry = _CONFIG["global"].get("retry", {})
    attempts = global_retry.get("attempts", 1)
    backoff = global_retry.get("backoff", 2.0)

    for key in _matching_domain_keys(hostname, _CONFIG["domains"]):
        domain_retry = _CONFIG["domains"][key].get("retry", {})
        if "attempts" in domain_retry:
            attempts = domain_retry["attempts"]
        if "backoff" in domain_retry:
            backoff = domain_retry["backoff"]

    return {"attempts": int(attempts), "backoff": float(backoff)}


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _apply_output_format(content: str, fmt: str) -> str:
    """
    Convert *content* (raw response body) to the requested output format.

    Formats:
      "raw"         — return as-is
      "text"        — regex tag-strip (internal alias for extract_text=True)
      "markdown"    — convert full HTML to Markdown via markdownify
      "trafilatura" — extract main content as Markdown via trafilatura;
                      falls back to raw if extraction fails
      "json"        — pretty-print JSON body; falls back to raw if not valid JSON
    """
    if fmt == "raw":
        return content
    if fmt == "text":
        return _extract_text(content)
    if fmt == "markdown":
        import markdownify  # lazy import
        return markdownify.markdownify(content, strip=["script", "style"])
    if fmt == "trafilatura":
        import trafilatura  # lazy import
        extracted = trafilatura.extract(content, output_format="markdown")
        if extracted is None:
            _log.warning("trafilatura returned None; falling back to raw HTML")
            return content
        return extracted
    if fmt == "json":
        try:
            parsed = json.loads(content)
            return json.dumps(parsed, indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            _log.warning("output_format=json but response is not valid JSON; returning raw")
            return content
    _log.error("Unknown output format %r; returning raw content", fmt)
    return content


def _extract_text(html: str) -> str:
    """Strip HTML tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Header validation
# ---------------------------------------------------------------------------

_INVALID_HEADER_RE = re.compile(r"[\r\n\x00]")


def _validate_headers(headers: dict[str, str]) -> None:
    """Raise ValueError if any header name or value contains \\r, \\n, or NUL."""
    for name, value in headers.items():
        if _INVALID_HEADER_RE.search(name):
            raise ValueError(f"Invalid header name contains control character: {name!r}")
        if _INVALID_HEADER_RE.search(str(value)):
            raise ValueError(f"Invalid value for header {name!r} contains control character")


# ---------------------------------------------------------------------------
# MCP tool
# ---------------------------------------------------------------------------

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
                          Merged on top of the base domain headers.
        extract_text:     If True, strip HTML tags and return clean readable text.
                          Legacy parameter — takes priority over output_format.
        max_bytes:        Truncate the response body to this many characters.
                          0 means no limit.
        follow_redirects: Follow HTTP redirects automatically. Default: True.
        output_format:    Override the output format for this request only.
                          Accepted values: "raw" (default), "markdown",
                          "trafilatura", "json".
                          Ignored if extract_text=True (legacy compat).
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

    timeout = _resolve_timeout(hostname)
    proxy = _resolve_proxy(hostname)
    retry_cfg = _resolve_retry(hostname)
    attempts = retry_cfg["attempts"]
    backoff_mult = retry_cfg["backoff"]

    _log.info(
        "fetch %s %s (hostname=%s, injected=%s, timeout=%.1fs, attempts=%d)",
        method.upper(),
        url,
        hostname,
        applied_header_names or "none",
        timeout,
        attempts,
    )

    client_kwargs: dict = {
        "follow_redirects": follow_redirects,
        "timeout": timeout,
    }
    if proxy:
        client_kwargs["proxy"] = proxy

    response = None
    last_exc: Exception | None = None
    delay = 1.0
    actual_attempts = 0

    async with httpx.AsyncClient(**client_kwargs) as client:
        for attempt in range(attempts):
            actual_attempts = attempt + 1
            try:
                response = await client.request(
                    method=method.upper(),
                    url=url,
                    headers=headers,
                    content=body.encode() if body else None,
                )
                if response.status_code >= 500 and attempt < attempts - 1:
                    _log.warning(
                        "fetch attempt %d/%d returned HTTP %s; retrying in %.1fs",
                        attempt + 1, attempts, response.status_code, delay,
                    )
                    await asyncio.sleep(delay)
                    delay *= backoff_mult
                    continue
                break
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt < attempts - 1:
                    _log.warning(
                        "fetch attempt %d/%d failed (%s); retrying in %.1fs",
                        attempt + 1, attempts, exc, delay,
                    )
                    await asyncio.sleep(delay)
                    delay *= backoff_mult
                else:
                    _log.error(
                        "fetch %s %s failed after %d attempt(s): %s",
                        method.upper(), url, attempts, exc,
                    )
                    raise

    if response is None:
        # Should not happen, but satisfy type checker
        raise RuntimeError("No response received")

    if response.is_error:
        _log.warning("fetch %s %s returned HTTP %s", method.upper(), url, response.status_code)

    content = response.text
    response_size = len(content)

    # Detect JSON from Content-Type when no explicit format is set
    content_type = response.headers.get("content-type", "")
    effective_fmt: str
    if extract_text:
        effective_fmt = "text"
    else:
        effective_fmt = _resolve_output_format(hostname, output_format)
        if effective_fmt == "raw" and "application/json" in content_type:
            effective_fmt = "json"

    content = _apply_output_format(content, effective_fmt)

    if max_bytes > 0:
        content = content[:max_bytes]

    injected = ", ".join(applied_header_names) if applied_header_names else "none"
    truncated_str = f"yes (max_bytes={max_bytes})" if max_bytes > 0 else "no"
    proxy_str = proxy or "none"
    retry_str = f"{actual_attempts}/{attempts}" if attempts > 1 else "disabled"

    summary = (
        f"--- Request Summary ---\n"
        f"URL:              {url}\n"
        f"Method:           {method.upper()}\n"
        f"Injected headers: {injected}\n"
        f"Status:           {response.status_code} {response.reason_phrase}\n"
        f"Response size:    {response_size} bytes\n"
        f"Output format:    {effective_fmt}\n"
        f"Text extracted:   {'yes' if extract_text else 'no'}\n"
        f"Truncated:        {truncated_str}\n"
        f"Timeout:          {timeout}s\n"
        f"Proxy:            {proxy_str}\n"
        f"Retry:            {retry_str}\n"
        f"---"
    )
    return f"{summary}\n\n{content}"


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    mcp.run()
