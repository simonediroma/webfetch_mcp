# webfetch-mcp

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![MCP](https://img.shields.io/badge/MCP-compatible-purple)
[![simonediroma/webfetch_mcp MCP server](https://glama.ai/mcp/servers/simonediroma/webfetch_mcp/badges/score.svg)](https://glama.ai/mcp/servers/simonediroma/webfetch_mcp)

A local Python MCP server that replaces your AI assistant's built-in `WebFetch` tool with a fully configurable HTTP client — supporting **domain-scoped headers, retries, proxies, timeouts, output formats, bot-block detection, and prompt-injection sanitization**, all without touching a single line of your assistant's config beyond registering the server.

## Why

The built-in `WebFetch` tool available in most AI assistants (Claude Code, Cursor, Continue, Zed, etc.) sends requests without custom headers, which means it gets blocked by bot-protection systems (Akamai, Cloudflare, paywalls, etc.) and can't authenticate against APIs that require domain-specific tokens.

This server is a drop-in replacement: it exposes the same `fetch` tool to any MCP-compatible AI assistant, but enriches every outbound request with the right headers, format, and retry strategy based on the target domain — automatically, without you having to configure headers every time.

---

## Features

| Feature | Description |
|---------|-------------|
| **Domain-scoped headers** | Different auth headers per domain; global `*` fallback |
| **Per-call headers** | The client (or you) can inject extra headers for a single request |
| **YAML config** | Single readable file controls headers, timeouts, retries, proxies, and output formats |
| **Configurable timeout** | Per-domain request timeout (default 30 s) |
| **Retry with backoff** | Auto-retry on HTTP 5xx or network errors, with exponential backoff |
| **Per-domain proxy** | Route traffic through a different proxy per domain |
| **Output formats** | `raw`, `markdown`, `trafilatura` (main content), `json` (pretty-print), `lighthtml` (minimal HTML) |
| **JSON auto-detection** | Responses with `application/json` Content-Type are pretty-printed automatically |
| **Metadata extraction** | Extracts title, author, date, source via trafilatura (opt-in per domain) |
| **Bot-block detection** | Detects Cloudflare / CAPTCHA blocks; optionally retries with a Chrome User-Agent |
| **Prompt-injection sanitization** | Scans fetched content for injection patterns; `flag` or `strip` mode |
| **CSS selector extraction** | Extract specific HTML elements before format conversion, configurable per domain or per call |
| **Redirect tracing** | Optionally record and display the full redirect chain in the summary |
| **Response assertions** | `assert_status` / `assert_contains` raise an error on mismatch — useful for CI/CD smoke tests |
| **Header injection protection** | Validates headers for control characters (`\r`, `\n`, NUL) |
| **Response truncation** | `max_bytes` cap to avoid filling the assistant's context window |
| **Detailed response summary** | Every response includes a structured summary (status, elapsed ms, injected headers, format, etc.) |
| **JS rendering (Playwright)** | Render JavaScript-heavy SPAs with headless Chromium before extracting content; configurable globally, per-domain, or per-call |
| **lighthtml output format** | Strips `<style>`, `<script>` (except JSON-LD), comments, and all tag attributes — returns minimal bare HTML structure |

---

## Requirements

- Python 3.10+
- Any MCP-compatible AI assistant (Claude Code, Cursor, Continue, Zed, etc.)

---

## Quick start

```bash
git clone https://github.com/simonediroma/webfetch_mcp.git
cd webfetch_mcp

# Mac / Linux
python -m venv .venv && .venv/bin/pip install -r requirements.txt
# Windows
python -m venv .venv && .venv\Scripts\pip install -r requirements.txt

cp webfetch.yaml.example webfetch.yaml   # then edit with your tokens
```

Then [register the server](#registering-with-your-ai-assistant) in your AI assistant config and restart. Done.

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

`requirements.txt` installs:

```
mcp[cli]>=1.0.0
httpx>=0.27.0
python-dotenv>=1.0.0
markdownify>=0.12.0
trafilatura>=1.12.0
pyyaml>=6.0
beautifulsoup4>=4.12.0
```

**Optional — JS rendering** requires Playwright:

```bash
pip install playwright && playwright install chromium
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
  output_format: raw       # raw | markdown | trafilatura | json | lighthtml
  timeout: 30              # seconds
  retry:
    attempts: 1            # 1 = no retry
    backoff: 2.0           # exponential multiplier (1s → 2s → 4s …)
  proxy: null              # e.g. "http://proxy.corp:8080"
  extract_metadata: false  # true = prepend title/author/date to content
  sanitize_content: false  # false | "flag" | "strip"
  bot_block_detection: false  # false | "report" | "retry"
  css_selector: null       # CSS selector to extract element(s) before format conversion
  render_js: false           # true = render JS via headless Chromium (requires playwright)

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

## Registering with your AI assistant

Most AI assistants use a `mcpServers` block in a JSON settings file. The format is the same across assistants — only the file location differs.

### Claude Code

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

### Cursor

Add to `~/.cursor/mcp.json` (or the project-level `.cursor/mcp.json`):

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

### Claude Desktop (Mac / Windows)

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` on Mac, or `%APPDATA%\Claude\claude_desktop_config.json` on Windows:

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

> **Windows:** use `.venv\Scripts\python.exe` as the `command` value.

### Other assistants (Continue, Zed, etc.)

Consult your assistant's MCP documentation for the exact config file location. The server block is the same — only the file path differs.

> **Windows:** use `.venv\Scripts\python.exe` instead of `.venv/bin/python`

Restart your client after saving. The tool is registered as **`mcp__webfetch__fetch`**.

---

## Verifying the server is active

After registering and restarting your client, confirm the tool is loaded:

- **Claude Code**: run `/mcp` in the chat — `webfetch` should appear with status `connected` and `fetch` listed as an available tool.
- **Cursor**: open **Settings → MCP** and check that `webfetch` appears in the active server list.
- **Other clients**: look for an MCP tool panel or server list in settings.

If the server doesn't appear, check:
1. The Python path and `server.py` path in your config are **absolute** and correct.
2. The virtual environment has all dependencies installed (`pip install -r requirements.txt`).
3. There are no errors in your YAML/env config — run `python server.py` directly in a terminal to see startup errors on stderr.

---

## Forcing your client to use webfetch instead of the native tool

Most AI assistants expose both their built-in WebFetch and any registered MCP tools. To ensure `mcp__webfetch__fetch` is always preferred:

### Claude Code

Add the following to your project's `CLAUDE.md` (or `~/.claude/CLAUDE.md` to apply it globally to all projects):

```markdown
Always use the `mcp__webfetch__fetch` tool for all HTTP requests and web browsing.
Do not use the built-in WebFetch tool.
```

Alternatively, add a `systemPrompt` entry to `~/.claude/settings.json`:

```json
{
  "systemPrompt": "Always use mcp__webfetch__fetch for all web requests. Do not use the built-in WebFetch tool.",
  "mcpServers": { "...": "..." }
}
```

### Other AI assistants

Consult your assistant's documentation for how to set a system prompt or custom instruction. The instruction to include is:

> Use `mcp__webfetch__fetch` for all web requests instead of any built-in fetch or browser tool.

---

## End-to-end example

Once installed and registered, open your AI assistant and try:

> **"Fetch https://example.com and return the main content"**

The assistant calls `mcp__webfetch__fetch` automatically, applying whatever headers and output format you configured for that domain. You'll see a response like:

```
--- Request Summary ---
URL:              https://example.com
Method:           GET
Injected headers: User-Agent
Status:           200 OK
Elapsed:          312ms
Output format:    trafilatura
---

[Extracted article content here]
```

If you configured domain-specific auth headers, the summary line `Injected headers` will list them — confirming they were sent. No extra prompting needed; the configuration is applied automatically on every request to that domain.

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
| `output_format` | `str \| None` | `None` | Per-call format override: `"raw"`, `"markdown"`, `"trafilatura"`, `"json"`, `"lighthtml"` |
| `css_selector` | `str \| None` | `None` | CSS selector to extract HTML element(s) before format conversion (e.g. `"article"`, `"#main"`) |
| `trace_redirects` | `bool` | `False` | Display the full redirect chain in the summary |
| `assert_status` | `int \| None` | `None` | Raise an error if the response status code does not match this value |
| `assert_contains` | `str \| None` | `None` | Raise an error if this string is not found in the response body (case-sensitive) |
| `render_js` | `bool \| None` | `None` | Render the page with headless Chromium (executes JS, waits for network idle). Requires `playwright`. |

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
JS rendering:     no
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

The server now fetches `mysite.com` pages with your session and extracts clean article text automatically.

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

### Protect against prompt-injection in untrusted pages

```yaml
global:
  sanitize_content: flag    # warn when suspicious patterns are found

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

Or pass it per-call:

```
fetch url="https://docs.example.com/guide" css_selector="section#quickstart"
```

If the selector matches nothing, the full HTML is used as fallback.

---

### Smoke test an endpoint (CI/CD style)

Use `assert_status` and `assert_contains` to make the tool raise an error if the response doesn't match expectations — useful for health checks and regression tests:

```
fetch url="https://api.example.com/health" assert_status=200 assert_contains='"status":"ok"'
```

If the check fails, the client receives a clear `ValueError` instead of silently returning a wrong response.

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

### Leverage Cloudflare content negotiation for LLM-ready Markdown

Cloudflare's [Markdown for Agents](https://blog.cloudflare.com/markdown-for-agents/) feature converts HTML to Markdown at the edge when the request includes an `Accept: text/markdown` header. This cuts token usage by ~80% compared to raw HTML — and the conversion happens server-side, so it's faster and more accurate than any local HTML-to-Markdown pipeline.

With webfetch_mcp you can inject that header automatically for every request, or only for specific domains:

```yaml
# Global — every request negotiates Markdown
global:
  headers:
    Accept: "text/markdown"

# Or per-domain — only for sites you know support it
domains:
  docs.example.com:
    headers:
      Accept: "text/markdown"
    output_format: raw          # Cloudflare already returns Markdown; skip local conversion
```

Cloudflare's response includes useful extra headers:

| Header | Description |
|--------|-------------|
| `x-markdown-tokens` | Estimated token count of the Markdown document — useful for context-window budgeting |
| `Content-Signal` | AI usage permissions (e.g. `ai-train=yes, search=yes, ai-input=yes`) |

Sites that don't support the feature simply ignore the header and return normal HTML, so it is safe to set globally. You can verify support with a quick curl:

```bash
curl -sI https://example.com -H "Accept: text/markdown" | grep -i content-type
# text/markdown → supported; text/html → not supported
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
