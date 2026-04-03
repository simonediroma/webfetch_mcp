# webfetch-mcp

A local Python MCP server that replaces Claude's built-in `WebFetch` tool with a fully configurable HTTP client — supporting **domain-scoped headers, retries, proxies, timeouts, output formats, bot-block detection, and prompt-injection sanitization**, all without touching a single line of Claude's config beyond registering the server.

## Why

Claude's built-in `WebFetch` sends requests without custom headers, which means it gets blocked by bot-protection systems (Akamai, Cloudflare, paywalls, etc.) and can't authenticate against APIs that require domain-specific tokens.

This server is a drop-in replacement: it exposes the same `fetch` tool to Claude, but enriches every outbound request with the right headers, format, and retry strategy based on the target domain — automatically, without you having to ask Claude to add headers every time.

---

## Features

| Feature | Description |
|---------|-------------|
| **Domain-scoped headers** | Different auth headers per domain; global `*` fallback |
| **Per-call headers** | Claude (or you) can inject extra headers for a single request |
| **YAML config** | Single readable file controls headers, timeouts, retries, proxies, and output formats |
| **Configurable timeout** | Per-domain request timeout (default 30 s) |
| **Retry with backoff** | Auto-retry on HTTP 5xx or network errors, with exponential backoff |
| **Per-domain proxy** | Route traffic through a different proxy per domain |
| **Output formats** | `raw`, `markdown`, `trafilatura` (main content), `json` (pretty-print) |
| **JSON auto-detection** | Responses with `application/json` Content-Type are pretty-printed automatically |
| **Metadata extraction** | Extracts title, author, date, source via trafilatura (opt-in per domain) |
| **Bot-block detection** | Detects Cloudflare / CAPTCHA blocks; optionally retries with a Chrome User-Agent |
| **Prompt-injection sanitization** | Scans fetched content for injection patterns; `flag` or `strip` mode |
| **CSS selector extraction** | Extract specific HTML elements before format conversion, configurable per domain or per call |
| **Redirect tracing** | Optionally record and display the full redirect chain in the summary |
| **Response assertions** | `assert_status` / `assert_contains` raise an error on mismatch — useful for CI/CD smoke tests |
| **Header injection protection** | Validates headers for control characters (`\r`, `\n`, NUL) |
| **Response truncation** | `max_bytes` cap to avoid filling Claude's context window |
| **Detailed response summary** | Every response includes a structured summary (status, elapsed ms, injected headers, format, etc.) |

---

## Requirements

- Python 3.10+
- [Claude Code](https://claude.ai/code) (CLI, desktop app, or IDE extension)

---

## Installation

```bash
git clone https://github.com/simonediroma/webfetch_mcp.git
cd webfetch_mcp

python -m venv .venv

# Windows
.venv\Scripts\pip install -r requirements.txt

# Mac / Linux
.venv/bin/pip install -r requirements.txt
```

---

## Configuration

There are two ways to configure the server. **YAML is recommended** — it supports all options. The legacy environment variable approach still works for simple cases.

### Option A — YAML config file (recommended)

Copy the example and edit it:

```bash
cp webfetch.yaml.example webfetch.yaml
```

Point the server at it:

```bash
# In your shell profile, or in the MCP server env block (see Registration below)
export WEBFETCH_CONFIG=/absolute/path/to/webfetch.yaml
```

#### Full YAML reference

```yaml
# Global defaults — applied to every request unless overridden
global:
  headers:
    User-Agent: "MyBot/1.0"
  output_format: raw       # raw | markdown | trafilatura | json
  timeout: 30              # seconds
  retry:
    attempts: 1            # 1 = no retry
    backoff: 2.0           # exponential multiplier (1s → 2s → 4s …)
  proxy: null              # e.g. "http://proxy.corp:8080"
  extract_metadata: false  # true = prepend title/author/date to content
  sanitize_content: false  # false | "flag" | "strip"
  bot_block_detection: false  # false | "report" | "retry"
  css_selector: null       # CSS selector to extract element(s) before format conversion

# Per-domain overrides — only the fields you list are overridden
domains:
  example.com:
    headers:
      X-Akamai-Token: "your-token-here"
    output_format: trafilatura
    timeout: 60
    retry:
      attempts: 3
      backoff: 2.0

  news-site.com:
    output_format: markdown
    bot_block_detection: retry   # auto-retry with Chrome UA if blocked
    css_selector: "article.main-content"  # extract only the article body

  internal.corp:
    proxy: "http://proxy.corp:8080"
    headers:
      Authorization: "Bearer my-internal-token"

  api.example.com:
    output_format: json
    timeout: 10
    retry:
      attempts: 5
      backoff: 1.5
```

Domain matching uses **suffix rules**: `example.com` matches both `example.com` and `www.example.com`. When multiple domains match, the most specific (longest) key wins. Global settings are always applied first, then overridden by increasingly specific domain rules.

---

### Option B — Environment variables (legacy)

Copy `.env.example` and fill in your values:

```bash
cp .env.example .env
```

**`WEBFETCH_HEADERS`** — domain-scoped request headers (single-line JSON):

```env
WEBFETCH_HEADERS={"*": {"User-Agent": "MyBot/1.0"}, "example.com": {"X-Auth-Token": "your-token"}}
```

**`WEBFETCH_OUTPUT`** — domain-scoped output format (single-line JSON):

```env
WEBFETCH_OUTPUT={"*": "raw", "example.com": "trafilatura", "news.com": "markdown"}
```

**`WEBFETCH_SELECTORS`** — domain-scoped CSS selector (single-line JSON):

```env
WEBFETCH_SELECTORS={"example.com": "article.main-content", "news.com": "div#article-body"}
```

> When `WEBFETCH_CONFIG` is set, the env vars above are ignored entirely.

---

## Registering with Claude Code

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "webfetch": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/server.py"],
      "env": {
        "WEBFETCH_CONFIG": "/absolute/path/to/webfetch.yaml"
      }
    }
  }
}
```

> **Windows:** use `.venv\Scripts\python.exe`

Restart Claude Code after saving. The tool appears as **`mcp__webfetch__fetch`** and Claude will use it automatically for web requests.

---

## Tool API

All parameters are optional except `url`.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | `str` | — | URL to fetch |
| `method` | `str` | `"GET"` | HTTP verb (GET, POST, PUT, DELETE, …) |
| `body` | `str \| None` | `None` | Request body for POST/PUT |
| `extra_headers` | `dict \| None` | `None` | Per-call headers merged on top of domain headers |
| `extract_text` | `bool` | `False` | Strip HTML tags, return plain text (legacy; overrides `output_format`) |
| `max_bytes` | `int` | `0` | Truncate response to N characters (0 = unlimited) |
| `follow_redirects` | `bool` | `True` | Follow HTTP redirects |
| `output_format` | `str \| None` | `None` | Per-call format override: `"raw"`, `"markdown"`, `"trafilatura"`, `"json"` |
| `css_selector` | `str \| None` | `None` | CSS selector to extract HTML element(s) before format conversion (e.g. `"article"`, `"#main"`) |
| `trace_redirects` | `bool` | `False` | Display the full redirect chain in the summary |
| `assert_status` | `int \| None` | `None` | Raise an error if the response status code does not match this value |
| `assert_contains` | `str \| None` | `None` | Raise an error if this string is not found in the response body (case-sensitive) |

### Response format

Every response starts with a structured summary block:

```
--- Request Summary ---
URL:              https://example.com/article
Method:           GET
Injected headers: User-Agent, X-Akamai-Token
Status:           200 OK
Elapsed:          843ms
Response size:    42381 bytes
Output format:    trafilatura
Text extracted:   no
Truncated:        no
Timeout:          60.0s
Proxy:            none
Retry:            disabled
Bot block:        none
Metadata:         extracted
Sanitization:     flag (0 pattern(s) found)
CSS selector:     "article.main-content" (applied)
---

**Title:** Example Article
**Author:** Jane Doe
**Date:** 2024-01-15
**Source:** Example News

---

[Main article content as Markdown …]
```

---

## Use cases

### Bypass Akamai bot protection on a specific domain

```yaml
# webfetch.yaml
domains:
  mysite.com:
    headers:
      X-Akamai-Token: "your-token"
      Cookie: "session=abc123"
    output_format: trafilatura
```

Claude now fetches `mysite.com` pages with your session and extracts clean article text automatically.

---

### Extract clean article content from news sites

```yaml
domains:
  theguardian.com:
    output_format: trafilatura
    extract_metadata: true

  reuters.com:
    output_format: markdown
```

---

### Consume JSON APIs reliably

```yaml
domains:
  api.example.com:
    output_format: json
    timeout: 10
    retry:
      attempts: 5
      backoff: 1.5
    headers:
      Authorization: "Bearer my-api-key"
```

Responses are pretty-printed JSON. If the endpoint returns `application/json` but you forget to set `output_format`, the server detects it automatically.

---

### Route corporate intranet traffic through a proxy

```yaml
domains:
  internal.corp:
    proxy: "http://proxy.corp:8080"
    headers:
      Authorization: "Bearer my-internal-token"
    timeout: 60
```

---

### Detect and recover from bot blocks automatically

```yaml
domains:
  news-site.com:
    bot_block_detection: retry   # retry once with a Chrome User-Agent
```

In `report` mode, the summary block flags the block without retrying. In `retry` mode, the server automatically issues a second request with a realistic Chrome User-Agent.

---

### Protect Claude from prompt-injection in untrusted pages

```yaml
global:
  sanitize_content: flag    # warn Claude when suspicious patterns are found

domains:
  untrusted-forum.com:
    sanitize_content: strip  # silently remove injection attempts
```

---

### Extract a specific section of a page with CSS selector

Configure it globally in the YAML for a domain:

```yaml
domains:
  docs.example.com:
    css_selector: "main article"   # only the article content, not nav/sidebar
    output_format: markdown
```

Or pass it per-call when asking Claude to fetch a page:

```
fetch url="https://docs.example.com/guide" css_selector="section#quickstart"
```

If the selector matches nothing, the full HTML is used as fallback.

---

### Smoke test an endpoint from Claude (CI/CD style)

Use `assert_status` and `assert_contains` to make the tool raise an error if the response doesn't match expectations — useful for health checks and regression tests:

```
fetch url="https://api.example.com/health" assert_status=200 assert_contains='"status":"ok"'
```

If the check fails, Claude receives a clear `ValueError` instead of silently returning a wrong response.

---

### Trace the redirect chain of a URL

```
fetch url="https://short.ly/abc123" trace_redirects=true
```

The summary will show each hop:

```
Redirect chain:
  301  https://short.ly/abc123  →  https://example.com/landing
  200  https://example.com/landing  (final)
```

---

## Security

- **Secrets stay local** — `.env` and `webfetch.yaml` are git-ignored; tokens never leave your machine.
- **Domain isolation** — headers are injected only for matching domains; unrelated requests receive only global headers.
- **Header injection protection** — the server validates all header names and values for control characters before sending.
- **Prompt-injection sanitization** — optionally scan and flag/strip patterns like "ignore all previous instructions" from fetched content.

---

## Running locally (development)

```bash
# Mac / Linux
.venv/bin/python server.py

# Windows
.venv\Scripts\python.exe server.py
```

The server communicates over **stdio** (standard MCP transport). No HTTP port is used.

Run the test suite:

```bash
pytest tests/ -v
```

---

## License

MIT
