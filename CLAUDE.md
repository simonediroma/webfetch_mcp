# WebFetch MCP Server

---

## IMPORTANT — How to use this tool correctly

**Always use `mcp__webfetch__fetch`** to fetch URLs. Never use the built-in `WebFetch` tool — it ignores all custom headers and output format configuration.

**Never pass `output_format` or `extract_text` unless the user explicitly asks for a specific format.**
The server is configured with a default output format (currently `trafilatura`).
Passing `output_format="raw"` or `extract_text=True` overrides that configured default and produces lower-quality output.

**Rules:**
- ✅ `mcp__webfetch__fetch(url="https://example.com")` — correct, uses configured defaults
- ❌ `mcp__webfetch__fetch(url="...", output_format="raw")` — wrong, overrides trafilatura default
- ❌ `mcp__webfetch__fetch(url="...", extract_text=True)` — wrong, produces noisy CSS-contaminated text
- ✅ `mcp__webfetch__fetch(url="...", output_format="markdown")` — ok only if the user explicitly asked for markdown

---


Local Python MCP server that replaces the AI assistant's built-in WebFetch tool.
Main purpose: inject **domain-scoped custom HTTP headers** into every outbound request,
used to inject provider-specific authentication headers on specific domains.

---

## Project structure

```
webfetch_mcp/
├── server.py            # MCP server — single entrypoint
├── requirements.txt     # Python dependencies
├── .env.example         # Header config template (copy to .env)
└── .claude/
    └── launch.json      # Dev server config for Claude Code preview_start
```

---

## Setup

```bash
python -m venv .venv

# Windows
.venv\Scripts\pip install -r requirements.txt

# Mac / Linux
.venv/bin/pip install -r requirements.txt

cp .env.example .env   # then edit .env with real tokens
```

---

## Configuration (`.env`)

### `WEBFETCH_HEADERS` — domain-scoped request headers

`WEBFETCH_HEADERS` is a **single-line JSON object** with domain-scoped headers:

```env
WEBFETCH_HEADERS={"*": {"User-Agent": "MyBot/1.0"}, "example.com": {"X-Auth-Token": "TOKEN"}}
```

| Key | Meaning |
|-----|---------|
| `"*"` | Applied to **every** request (global) |
| `"example.com"` | Applied only when hostname ends with `example.com` |

Merge order (later wins): `*` → domain-specific → per-call `extra_headers`.

### `WEBFETCH_OUTPUT` — domain-scoped output format

`WEBFETCH_OUTPUT` is a **single-line JSON object** controlling how the response body is returned:

```env
WEBFETCH_OUTPUT={"*": "raw", "example.com": "trafilatura", "news.com": "markdown"}
```

| Value | Behaviour |
|-------|-----------|
| `"raw"` | Return raw HTML as-is (default) |
| `"markdown"` | Convert full HTML to Markdown via `markdownify` |
| `"trafilatura"` | Extract main content and return as Markdown via `trafilatura` (falls back to raw if extraction fails) |
| `"lighthtml"` | Strip `<style>`, `<script>` (except JSON-LD), comments, and all tag attributes; returns minimal HTML structure |

Merge order (later wins): `*` → domain-specific → per-call `output_format` parameter.

### `WEBFETCH_SELECTORS` — domain-scoped CSS selector

`WEBFETCH_SELECTORS` is a **single-line JSON object** specifying a CSS selector per domain.
The selector is applied to the raw HTML **before** any output format conversion, so only the
matched element(s) are passed to `markdownify` / `trafilatura` / etc.

```env
WEBFETCH_SELECTORS={"example.com": "article.main-content", "news.com": "div#article-body"}
```

| Key | Meaning |
|-----|---------|
| `"*"` | Applied to **every** response (global) |
| `"example.com"` | Applied only when hostname ends with `example.com` |

- All elements matching the selector are concatenated as HTML.
- If the selector matches nothing, the full HTML is used as fallback (with a warning logged).
- Uses `beautifulsoup4` with Python's built-in `html.parser` (lazy import).

Merge order (later wins): `*` → domain-specific → per-call `css_selector` parameter.

---

## Registering with Claude Code

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "webfetch": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/server.py"]
    }
  }
}
```

- **Windows**: use `.venv\Scripts\python.exe`
- **Mac/Linux**: use `.venv/bin/python`

Restart Claude Code after saving. The tool appears as `mcp__webfetch__fetch`.

---

## Tool API

```python
fetch(
    url: str,                    # required
    method: str = "GET",         # HTTP verb
    body: str | None = None,     # request body for POST/PUT
    extra_headers: dict | None,  # per-call headers (merged on top)
    extract_text: bool = False,  # legacy: strip HTML → clean text (wins over output_format)
    max_bytes: int = 0,          # 0 = apply default 10 MB cap; -1 = no cap; >0 = explicit cap
    follow_redirects: bool = True,
    output_format: str | None,   # "raw" | "markdown" | "trafilatura" | "json"
    css_selector: str | None,    # CSS selector to extract HTML element(s) before format conversion
    trace_redirects: bool = False,  # include full redirect chain in summary
    assert_status: int | None,   # raise ValueError if status code doesn't match
    assert_contains: str | None, # raise ValueError if string not found in body
) -> str
```

Response format:
```
Status: 200
Injected headers: User-Agent, X-Auth-Token

<body>
```

---

## Running locally (test / dev)

```bash
# Windows
.venv\Scripts\python.exe server.py

# Mac / Linux
.venv/bin/python server.py
```

The server communicates over **stdio** (standard MCP transport).
No HTTP port is used in production mode.

For Claude Code's `preview_start` (dev only), port 8000 is declared in
`.claude/launch.json`. On Mac/Linux update `runtimeExecutable` to
`.venv/bin/python`.

---

## Configuration precedence

When `WEBFETCH_CONFIG` is set, the YAML file is loaded and the legacy env vars
(`WEBFETCH_HEADERS`, `WEBFETCH_OUTPUT`, `WEBFETCH_SELECTORS`) are **ignored entirely**.

When `WEBFETCH_CONFIG` is not set, only the three legacy env vars are read.
Features available exclusively via YAML (not accessible through env vars):
`timeout`, `retry`, `proxy`, `bot_block_detection`, `sanitize_content`,
`extract_metadata`, `tls_verify`, `tls_ca_bundle`, `tls_min_version`,
`allowed_domains`, `denied_domains`.

---

## Key implementation notes (`server.py`)

- `_load_header_config()` — parses `WEBFETCH_HEADERS` at startup; raises `RuntimeError` on invalid JSON.
- `_resolve_headers(hostname, extra_headers)` — merges global + domain + per-call headers.
  Domain matching: `hostname == key or hostname.endswith("." + key)`.
  Multiple matches are applied longest-key-last (most specific wins).
- `_load_output_config()` — parses `WEBFETCH_OUTPUT` at startup; validates each value against `_VALID_OUTPUT_FORMATS`.
- `_resolve_output_format(hostname, per_call_format)` — same domain-matching logic as headers; returns effective format string.
- `_apply_output_format(content, fmt)` — dispatches to `markdownify` or `trafilatura` based on format; uses lazy imports.
- `_extract_text(html)` — regex tag stripping + whitespace collapse (internal, used by legacy `extract_text=True`).
- `_resolve_css_selector(hostname, per_call_selector)` — same domain-matching logic; returns effective CSS selector string or `None`.
- `_apply_css_selector(html, selector)` — extracts matching elements via `BeautifulSoup.select()`; concatenates outer HTML of all matches; falls back to full HTML if nothing matches. Applied **before** `_apply_output_format`.
- Uses `httpx.AsyncClient` with `follow_redirects=True`.

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `mcp[cli]` | MCP server framework (FastMCP) |
| `httpx` | Async HTTP client |
| `python-dotenv` | Load `.env` at startup |
| `markdownify` | HTML → Markdown conversion for `"markdown"` output format |
| `trafilatura` | Main content extraction for `"trafilatura"` output format |
| `beautifulsoup4` | CSS selector–based HTML element extraction for `css_selector` |
