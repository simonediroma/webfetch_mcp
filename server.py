"""
WebFetch MCP Server
Replaces Claude's built-in WebFetch tool with support for domain-scoped
custom HTTP headers (e.g. provider-specific authentication tokens), retry logic,
configurable timeouts, per-domain proxies, and flexible output formats.

Configuration is loaded from a YAML file (WEBFETCH_CONFIG env var) or falls
back to the legacy WEBFETCH_HEADERS / WEBFETCH_OUTPUT environment variables.
"""

import asyncio
import ipaddress
import json
import logging
import os
import re
import time
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

# Default response-body cap (10 MB).  Prevents OOM when a remote server returns
# an unexpectedly large payload.  Pass max_bytes=-1 to disable explicitly.
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024

_VALID_SANITIZE_MODES = frozenset({"flag", "strip"})
_VALID_BOT_BLOCK_MODES = frozenset({"report", "retry"})

_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+all\s+previous\s+instructions?", re.I),
    re.compile(r"you\s+are\s+now\s+(?:a|an)\s+\w+", re.I),
    re.compile(r"act\s+as\s+(?:a|an)\s+\w+", re.I),
    re.compile(r"disregard\s+(?:all\s+)?(?:previous|prior)\s+instructions?", re.I),
    re.compile(r"new\s+instructions?:\s*\n", re.I),
    re.compile(r"system\s+prompt\s*:", re.I),
    re.compile(r"<\|(?:system|user|assistant)\|>", re.I),
]

_BOT_BLOCK_STATUS_CODES = frozenset({403, 429, 503})
_BOT_BLOCK_HEADER_SIGNALS = {"cf-ray", "cf-mitigated"}
_BOT_BLOCK_BODY_PATTERNS = [
    re.compile(r"cloudflare", re.I),
    re.compile(r"captcha", re.I),
    re.compile(r"access\s+denied", re.I),
    re.compile(r"please\s+verify", re.I),
    re.compile(r"bot\s+detection", re.I),
    re.compile(r"are\s+you\s+human", re.I),
]
_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)

_DEFAULT_GLOBAL: dict = {
    "headers": {},
    "output_format": "raw",
    "timeout": 30.0,
    "retry": {"attempts": 1, "backoff": 2.0},
    "proxy": None,
    "extract_metadata": False,
    "sanitize_content": False,
    "bot_block_detection": False,
    "css_selector": None,
    "allowed_domains": [],
    "denied_domains": [],
}

# ---------------------------------------------------------------------------
# SSRF protection
# ---------------------------------------------------------------------------

# RFC-1918 private ranges, loopback, link-local (AWS metadata endpoint), and
# IPv6 loopback / unique-local.  Requests resolving to any of these are blocked
# unless the operator explicitly configures an allowlist that includes the host.
_PRIVATE_IP_RANGES = [
    ipaddress.ip_network("127.0.0.0/8"),     # loopback IPv4
    ipaddress.ip_network("10.0.0.0/8"),      # RFC-1918 class A
    ipaddress.ip_network("172.16.0.0/12"),   # RFC-1918 class B
    ipaddress.ip_network("192.168.0.0/16"),  # RFC-1918 class C
    ipaddress.ip_network("169.254.0.0/16"),  # link-local / AWS metadata service
    ipaddress.ip_network("0.0.0.0/8"),       # "this" network
    ipaddress.ip_network("::1/128"),         # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),        # IPv6 unique-local (fc00:: and fd00::)
    ipaddress.ip_network("fe80::/10"),       # IPv6 link-local
]

_ALLOWED_SCHEMES = frozenset({"http", "https"})

# Hostnames that are always blocked regardless of how they resolve.
_BLOCKED_HOSTNAMES = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})


def _validate_url(url: str, allowed_domains: list, denied_domains: list) -> None:
    """Raise ValueError if *url* is disallowed.

    Checks performed (in order):
    1. URL scheme must be http or https.
    2. Hostname must not be empty.
    3. Hostname must not be in the blocked-hostname list.
    4. If the hostname is a bare IP address it must not fall within any
       private / reserved range.
    5. If *denied_domains* is non-empty, the hostname must not match any entry.
    6. If *allowed_domains* is non-empty, the hostname must match at least one
       entry (suffix match, same logic as domain-header resolution).
    """
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(
            f"Disallowed URL scheme {scheme!r}. Only http and https are permitted."
        )

    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("URL has no hostname.")

    if host in _BLOCKED_HOSTNAMES:
        raise ValueError(f"Disallowed host {host!r}: loopback/localhost is not permitted.")

    try:
        addr = ipaddress.ip_address(host)
        for net in _PRIVATE_IP_RANGES:
            if addr in net:
                raise ValueError(
                    f"Disallowed IP address {host!r}: falls within reserved range {net}."
                )
    except ValueError as exc:
        if "Disallowed" in str(exc):
            raise
        # host is a domain name — not an IP literal; continue with domain checks

    if denied_domains:
        for entry in denied_domains:
            entry_lower = entry.lower()
            if host == entry_lower or host.endswith("." + entry_lower):
                raise ValueError(
                    f"Host {host!r} is in the denied_domains list ({entry!r})."
                )

    if allowed_domains:
        for entry in allowed_domains:
            entry_lower = entry.lower()
            if host == entry_lower or host.endswith("." + entry_lower):
                return  # host is explicitly allowed
        raise ValueError(
            f"Host {host!r} is not in the allowed_domains list."
        )

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

    if "extract_metadata" in source:
        val = source["extract_metadata"]
        if not isinstance(val, bool):
            raise RuntimeError(
                f"'{context}.extract_metadata' must be a boolean, got {type(val).__name__!r}"
            )
        target["extract_metadata"] = val

    if "sanitize_content" in source:
        val = source["sanitize_content"]
        if val is not False and val not in _VALID_SANITIZE_MODES:
            raise RuntimeError(
                f"'{context}.sanitize_content' is {val!r}; "
                f"must be false or one of {sorted(_VALID_SANITIZE_MODES)}"
            )
        target["sanitize_content"] = val

    if "bot_block_detection" in source:
        val = source["bot_block_detection"]
        if val is not False and val not in _VALID_BOT_BLOCK_MODES:
            raise RuntimeError(
                f"'{context}.bot_block_detection' is {val!r}; "
                f"must be false or one of {sorted(_VALID_BOT_BLOCK_MODES)}"
            )
        target["bot_block_detection"] = val

    if "css_selector" in source:
        val = source["css_selector"]
        target["css_selector"] = str(val) if val is not None else None

    for list_key in ("allowed_domains", "denied_domains"):
        if list_key in source:
            val = source[list_key]
            if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
                raise RuntimeError(
                    f"'{context}.{list_key}' must be a list of strings, got {val!r}"
                )
            target[list_key] = [str(v).lower() for v in val]


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

    # --- WEBFETCH_ALLOWED_DOMAINS / WEBFETCH_DENIED_DOMAINS ---
    for env_key, cfg_key in (
        ("WEBFETCH_ALLOWED_DOMAINS", "allowed_domains"),
        ("WEBFETCH_DENIED_DOMAINS", "denied_domains"),
    ):
        raw_domains_val = os.getenv(env_key, "")
        if raw_domains_val:
            entries = [d.strip().lower() for d in raw_domains_val.split(",") if d.strip()]
            config["global"][cfg_key] = entries

    # --- WEBFETCH_SELECTORS ---
    raw_selectors = os.getenv("WEBFETCH_SELECTORS", "")
    if raw_selectors:
        try:
            selector_cfg = json.loads(raw_selectors)
            if not isinstance(selector_cfg, dict):
                raise ValueError("WEBFETCH_SELECTORS must be a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(f"Invalid WEBFETCH_SELECTORS: {exc}") from exc

        for key, sel in selector_cfg.items():
            if key == "*":
                config["global"]["css_selector"] = str(sel)
            else:
                config["domains"].setdefault(key, {})["css_selector"] = str(sel)

    return config


# Load once at startup
try:
    _CONFIG: dict = _load_config()
except RuntimeError as _startup_exc:
    _log.critical("webfetch: fatal config error — %s", _startup_exc)
    raise SystemExit(1) from _startup_exc
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


def _resolve_extract_metadata(hostname: str) -> bool:
    """Return effective extract_metadata flag for *hostname*."""
    val = _CONFIG["global"].get("extract_metadata", False)
    for key in _matching_domain_keys(hostname, _CONFIG["domains"]):
        domain = _CONFIG["domains"][key]
        if "extract_metadata" in domain:
            val = domain["extract_metadata"]
    return bool(val)


def _resolve_sanitize_content(hostname: str) -> str | bool:
    """Return effective sanitize_content mode for *hostname* (False, 'flag', or 'strip')."""
    val: str | bool = _CONFIG["global"].get("sanitize_content", False)
    for key in _matching_domain_keys(hostname, _CONFIG["domains"]):
        domain = _CONFIG["domains"][key]
        if "sanitize_content" in domain:
            val = domain["sanitize_content"]
    return val


def _resolve_bot_block_detection(hostname: str) -> str | bool:
    """Return effective bot_block_detection mode for *hostname* (False, 'report', or 'retry')."""
    val: str | bool = _CONFIG["global"].get("bot_block_detection", False)
    for key in _matching_domain_keys(hostname, _CONFIG["domains"]):
        domain = _CONFIG["domains"][key]
        if "bot_block_detection" in domain:
            val = domain["bot_block_detection"]
    return val


def _resolve_allowed_denied_domains() -> tuple[list[str], list[str]]:
    """Return (allowed_domains, denied_domains) from global config."""
    allowed: list[str] = _CONFIG["global"].get("allowed_domains", [])
    denied: list[str] = _CONFIG["global"].get("denied_domains", [])
    return allowed, denied


def _resolve_css_selector(hostname: str, per_call_selector: str | None) -> str | None:
    """Return the effective CSS selector for *hostname*, or None.

    Precedence (later wins):
      1. Global config css_selector
      2. Most-specific matching domain css_selector
      3. per_call_selector (None = don't override)
    """
    val: str | None = _CONFIG["global"].get("css_selector")
    for key in _matching_domain_keys(hostname, _CONFIG["domains"]):
        domain = _CONFIG["domains"][key]
        if "css_selector" in domain:
            val = domain["css_selector"]
    if per_call_selector is not None:
        val = per_call_selector
    return val


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
    text = re.sub(r"<!--.*?-->", " ", html, flags=re.DOTALL)
    text = re.sub(r"<!\[CDATA\[.*?\]\]>", " ", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _apply_css_selector(html: str, selector: str) -> tuple[str, bool]:
    """Extract elements matching *selector* from *html* and return them as HTML.

    All matching elements are concatenated.  Falls back to the original HTML
    if no elements match or if BeautifulSoup raises an exception.

    Returns a ``(html, matched)`` tuple where *matched* is True only when at
    least one element was found and extracted.
    """
    try:
        from bs4 import BeautifulSoup  # lazy import
        soup = BeautifulSoup(html, "html.parser")
        elements = soup.select(selector)
        if not elements:
            _log.warning("css_selector %r matched nothing; using full HTML", selector)
            return html, False
        return "\n".join(str(el) for el in elements), True
    except Exception as exc:
        _log.warning("css_selector apply failed (%s); using full HTML", exc)
        return html, False


def _extract_trafilatura_metadata(raw_html: str) -> str | None:
    """Extract title/author/date/sitename from HTML via trafilatura.

    Returns a formatted markdown block, or None if no metadata was found.
    """
    try:
        import trafilatura  # lazy import
        meta = trafilatura.extract_metadata(raw_html)
        if meta is None:
            return None
        parts = []
        if meta.title:
            parts.append(f"**Title:** {meta.title}")
        if meta.author:
            parts.append(f"**Author:** {meta.author}")
        if meta.date:
            parts.append(f"**Date:** {meta.date}")
        if meta.sitename:
            parts.append(f"**Source:** {meta.sitename}")
        return "\n".join(parts) if parts else None
    except Exception as exc:
        _log.warning("trafilatura metadata extraction failed: %s", exc)
        return None


def _sanitize_content(content: str, mode: str) -> tuple[str, list[str]]:
    """Scan *content* for prompt-injection patterns.

    Returns ``(content, matched_patterns)`` where *content* is optionally
    modified (in ``"strip"`` mode) and *matched_patterns* lists the regex
    patterns that fired.
    """
    matched: list[str] = []
    for pat in _INJECTION_PATTERNS:
        if pat.search(content):
            matched.append(pat.pattern)
            if mode == "strip":
                content = pat.sub("[REMOVED]", content)
    return content, matched


def _detect_bot_block(status_code: int, resp_headers: dict, body: str) -> str | None:
    """Return a reason string if a bot-block or paywall is detected, else None.

    Checks status code, response headers, and the first 8 KB of the body.
    """
    reasons: list[str] = []
    if status_code in _BOT_BLOCK_STATUS_CODES:
        reasons.append(f"HTTP {status_code}")
    for sig in _BOT_BLOCK_HEADER_SIGNALS:
        if sig in resp_headers:
            reasons.append(f"header:{sig}")
    snippet = body[:8192]
    for pat in _BOT_BLOCK_BODY_PATTERNS:
        if pat.search(snippet):
            reasons.append(f"body:{pat.pattern!r}")
            break  # one body signal is sufficient
    return ", ".join(reasons) if reasons else None


# ---------------------------------------------------------------------------
# Header validation
# ---------------------------------------------------------------------------

_INVALID_HEADER_RE = re.compile(r"[\r\n\x00]")

# Headers that must not be overridden by callers to prevent HTTP request
# smuggling and host-header injection attacks.
_FORBIDDEN_HEADERS = frozenset({
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "upgrade",
    "te",
    "trailers",
})


def _validate_headers(headers: dict[str, str]) -> None:
    """Raise ValueError if any header is forbidden or contains control characters."""
    for name, value in headers.items():
        if name.lower() in _FORBIDDEN_HEADERS:
            raise ValueError(
                f"Header {name!r} cannot be set by callers "
                f"(potential HTTP request smuggling vector)."
            )
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
    css_selector: str | None = None,
    trace_redirects: bool = False,
    assert_status: int | None = None,
    assert_contains: str | None = None,
) -> str:
    """
    Fetch a URL and return its response, injecting domain-scoped authentication
    headers (e.g. provider-specific authentication tokens) automatically.

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
        css_selector:     CSS selector identifying the HTML element(s) to extract
                          before applying the output format.  All matching
                          elements are concatenated.  Overrides the domain-
                          scoped css_selector from config for this request only.
                          Only applied when Content-Type indicates HTML.
        trace_redirects:  If True, record and display the full redirect chain
                          (each hop's status code and URL) in the summary.
                          Requires follow_redirects=True (default).
        assert_status:    If set, raise an error when the final response status
                          code does not match this value. Useful for CI/CD
                          smoke tests (e.g. assert_status=200).
        assert_contains:  If set, raise an error when this string is not found
                          in the response body. Case-sensitive.
    """
    parsed = urlparse(url)
    hostname = parsed.hostname or ""

    allowed_domains, denied_domains = _resolve_allowed_denied_domains()
    _validate_url(url, allowed_domains, denied_domains)

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
    request_start = time.monotonic()

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

    elapsed_ms = int((time.monotonic() - request_start) * 1000)

    if response is None:
        # Should not happen, but satisfy type checker
        raise RuntimeError("No response received")

    if response.is_error:
        _log.warning("fetch %s %s returned HTTP %s", method.upper(), url, response.status_code)

    content = response.text
    raw_html = content  # preserve original for metadata extraction and bot-block scanning
    response_size = len(content)

    # --- Bot-block / paywall detection (Feature 3) ---
    bot_block_mode = _resolve_bot_block_detection(hostname)
    bot_block_reason: str | None = None
    chrome_retry_attempted = False

    if bot_block_mode and bot_block_mode in _VALID_BOT_BLOCK_MODES:
        bot_block_reason = _detect_bot_block(
            response.status_code,
            dict(response.headers),
            raw_html,
        )
        if bot_block_reason and bot_block_mode == "retry":
            chrome_retry_attempted = True
            retry_headers = {**headers, "User-Agent": _CHROME_UA}
            _log.info("bot-block detected (%s); retrying with Chrome UA", bot_block_reason)
            async with httpx.AsyncClient(**client_kwargs) as chrome_client:
                try:
                    chrome_resp = await chrome_client.request(
                        method=method.upper(),
                        url=url,
                        headers=retry_headers,
                        content=body.encode() if body else None,
                    )
                    chrome_block = _detect_bot_block(
                        chrome_resp.status_code,
                        dict(chrome_resp.headers),
                        chrome_resp.text[:8192],
                    )
                    if not chrome_block:
                        # Chrome retry succeeded — use its response
                        response = chrome_resp
                        content = chrome_resp.text
                        raw_html = content
                        response_size = len(content)
                        bot_block_reason = None
                    else:
                        _log.warning("Chrome UA retry also blocked: %s", chrome_block)
                except (httpx.TransportError, httpx.TimeoutException) as exc:
                    _log.warning("Chrome UA retry failed: %s", exc)

    # --- Redirect chain ---
    redirect_chain_str: str | None = None
    if trace_redirects and response.history:
        lines = [
            f"  {r.status_code}  {r.url}  →  {r.headers.get('location', '?')}"
            for r in response.history
        ]
        lines.append(f"  {response.status_code}  {response.url}  (final)")
        redirect_chain_str = "\n".join(lines)

    # Detect JSON from Content-Type when no explicit format is set
    content_type = response.headers.get("content-type", "")
    effective_fmt: str
    if extract_text:
        effective_fmt = "text"
    else:
        effective_fmt = _resolve_output_format(hostname, output_format)
        # Auto-detect JSON only when the caller did not explicitly request a format
        # and neither global nor domain config specified one (resolved to "raw" by
        # default). Explicit output_format="raw" preserves the raw response.
        if output_format is None and effective_fmt == "raw" and "application/json" in content_type:
            effective_fmt = "json"

    # --- CSS selector extraction ---
    effective_selector = _resolve_css_selector(hostname, css_selector)
    css_selector_applied = False
    css_selector_matched = False
    if effective_selector and "html" in content_type.lower():
        content, css_selector_matched = _apply_css_selector(content, effective_selector)
        css_selector_applied = True

    content = _apply_output_format(content, effective_fmt)

    # --- Metadata extraction (Feature 1) ---
    extract_meta = _resolve_extract_metadata(hostname)
    metadata_block: str | None = None
    if extract_meta and effective_fmt == "trafilatura":
        metadata_block = _extract_trafilatura_metadata(raw_html)
        if metadata_block:
            content = metadata_block + "\n\n---\n\n" + content

    # Apply size cap: explicit max_bytes > 0 overrides default; -1 disables entirely.
    effective_max_bytes: int
    if max_bytes == -1:
        effective_max_bytes = 0  # disabled
    elif max_bytes > 0:
        effective_max_bytes = max_bytes
    else:
        effective_max_bytes = _DEFAULT_MAX_BYTES

    if effective_max_bytes > 0:
        content = content[:effective_max_bytes]

    # --- Response assertions ---
    assertion_failures: list[str] = []
    if assert_status is not None and response.status_code != assert_status:
        assertion_failures.append(
            f"assert_status failed: expected {assert_status}, got {response.status_code}"
        )
    if assert_contains is not None and assert_contains not in content:
        assertion_failures.append(
            f"assert_contains failed: {assert_contains!r} not found in response body"
        )
    if assertion_failures:
        raise ValueError("Assertion failed: " + "; ".join(assertion_failures))

    # --- Prompt-injection sanitization (Feature 2) ---
    sanitize_mode = _resolve_sanitize_content(hostname)
    injection_warnings: list[str] = []
    if sanitize_mode and sanitize_mode in _VALID_SANITIZE_MODES:
        content, injection_warnings = _sanitize_content(content, sanitize_mode)
        if injection_warnings and sanitize_mode == "flag":
            warning = (
                "\n\n⚠️ **PROMPT INJECTION WARNING:** "
                "Suspicious patterns detected in fetched content."
            )
            content = warning + "\n\n" + content

    injected = ", ".join(applied_header_names) if applied_header_names else "none"
    if effective_max_bytes > 0 and len(content) == effective_max_bytes:
        truncated_str = f"yes (cap={effective_max_bytes})"
    elif max_bytes == -1:
        truncated_str = "no (cap disabled)"
    elif effective_max_bytes > 0:
        truncated_str = f"no (cap={effective_max_bytes})"
    else:
        truncated_str = "no"
    proxy_str = proxy or "none"
    retry_str = f"{actual_attempts}/{attempts}" if attempts > 1 else "disabled"

    # Build optional extra summary lines for new features
    extra_lines = ""
    if bot_block_mode:
        reason_str = bot_block_reason if bot_block_reason else "none"
        extra_lines += f"\nBot block:        {reason_str}"
        if bot_block_mode == "retry":
            extra_lines += f"\nChrome retry:     {'yes' if chrome_retry_attempted else 'no'}"
    if extract_meta:
        extra_lines += f"\nMetadata:         {'extracted' if metadata_block else 'no'}"
    if sanitize_mode:
        extra_lines += (
            f"\nSanitization:     {sanitize_mode} "
            f"({len(injection_warnings)} pattern(s) found)"
        )
    if effective_selector:
        if not css_selector_applied:
            applied_str = "skipped (non-HTML content)"
        elif css_selector_matched:
            applied_str = "applied"
        else:
            applied_str = "no match (full HTML used)"
        extra_lines += f"\nCSS selector:     {effective_selector!r} ({applied_str})"
    if trace_redirects:
        if redirect_chain_str:
            extra_lines += f"\nRedirect chain:\n{redirect_chain_str}"
        else:
            extra_lines += "\nRedirect chain:   none (no redirects)"
    if assert_status is not None:
        extra_lines += f"\nassert_status:    {assert_status} (passed)"
    if assert_contains is not None:
        extra_lines += f"\nassert_contains:  {assert_contains!r} (passed)"

    summary = (
        f"--- Request Summary ---\n"
        f"URL:              {url}\n"
        f"Method:           {method.upper()}\n"
        f"Injected headers: {injected}\n"
        f"Status:           {response.status_code} {response.reason_phrase}\n"
        f"Elapsed:          {elapsed_ms}ms\n"
        f"Response size:    {response_size} bytes\n"
        f"Output format:    {effective_fmt}\n"
        f"Text extracted:   {'yes' if extract_text else 'no'}\n"
        f"Truncated:        {truncated_str}\n"
        f"Timeout:          {timeout}s\n"
        f"Proxy:            {proxy_str}\n"
        f"Retry:            {retry_str}"
        f"{extra_lines}\n"
        f"---"
    )
    return f"{summary}\n\n{content}"


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    mcp.run()
