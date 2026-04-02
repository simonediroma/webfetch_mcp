# claude-webfetch-mcp

A local Python MCP server that replaces Claude's built-in `WebFetch` tool, adding support for **domain-scoped custom HTTP headers**, configurable output formats, per-domain timeouts, retries, and proxy routing.

## Why

Claude's built-in WebFetch tool sends requests without custom headers, which means it gets blocked by bot-protection systems (Akamai, Cloudflare, etc.). This server acts as a drop-in replacement: it exposes the same `fetch` tool to Claude, but automatically injects the right authentication headers based on the target domain — with no changes needed to your prompts.

## Features

- **Domain-scoped headers** — different headers per domain, plus a global `*` fallback
- **Per-call headers** — Claude (or you) can pass extra headers at call time
- **Multiple output formats** — return raw HTML, clean Markdown, extracted main content, or pretty-printed JSON
- **Per-domain timeout** — avoid hanging on slow domains
- **Retry with exponential backoff** — automatic retry on HTTP 5xx and transient network errors
- **Per-domain proxy** — route specific domains through a corporate or custom proxy
- **YAML config (recommended)** — all settings in one readable file
- **Legacy `.env` config** — simple setup for basic use cases
- **Header injection validation** — prevents CRLF/NUL injection attacks
- **Response size limit** — optional truncation to protect Claude's context window
- **Auto JSON detection** — pretty-prints JSON responses automatically

---

## Requirements

- Python 3.10+
- [Claude Code](https://claude.ai/code)

---

## Installation

```bash
git clone https://github.com/simonediroma/webfetch_mcp.git
cd webfetch_mcp

python -m venv .venv
```

Install dependencies:

```bash
# Windows
.venv\Scripts\pip install -r requirements.txt

# Mac / Linux
.venv/bin/pip install -r requirements.txt
```

---

## Configuration

There are two ways to configure the server. **YAML is recommended** for any non-trivial setup.

### Method 1 — YAML (recommended)

Copy the example file and edit it:

```bash
cp webfetch.yaml.example webfetch.yaml
```

Then set the environment variable pointing to its absolute path:

```bash
# Mac / Linux
export WEBFETCH_CONFIG=/absolute/path/to/webfetch.yaml

# Windows (PowerShell)
$env:WEBFETCH_CONFIG = "C:\absolute\path\to\webfetch.yaml"
```

Full structure of `webfetch.yaml`:

```yaml
# Global defaults — applied to every request unless overridden by a domain
global:
  headers:
    User-Agent: "MyBot/1.0"
  output_format: raw   # raw | markdown | trafilatura | json
  timeout: 30          # seconds
  retry:
    attempts: 1        # total tries (1 = no retry)
    backoff: 2.0       # delay multiplier between retries
  proxy: null          # e.g. "http://proxy.corp:8080"

# Per-domain overrides — only the fields present are overridden
domains:
  example.com:
    headers:
      X-Akamai-Token: "your-token-here"
    output_format: trafilatura
    timeout: 60
    retry:
      attempts: 3
      backoff: 2.0

  news.com:
    output_format: markdown

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

#### Available options per section

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `headers` | map | `{}` | HTTP headers to inject |
| `output_format` | string | `"raw"` | Response format (see table below) |
| `timeout` | number | `30` | Request timeout in seconds |
| `retry.attempts` | int | `1` | Total number of tries (1 = no retry) |
| `retry.backoff` | float | `2.0` | Delay multiplier; first wait is 1s, then multiplied each retry |
| `proxy` | string | `null` | Proxy URL (`http://host:port`) |

#### Output formats

| Value | Description | Best for |
|-------|-------------|----------|
| `raw` | Return body as-is (default) | HTML inspection, debugging |
| `markdown` | Convert full HTML to Markdown via `markdownify` | Documentation, web pages |
| `trafilatura` | Extract main article content as Markdown | News articles, blogs |
| `json` | Pretty-print JSON (auto-detected from `Content-Type` even if not set) | REST APIs |

#### Domain matching rules

- `"example.com"` matches both `example.com` and `www.example.com` (suffix match)
- When multiple domain keys match, the **most specific (longest) key wins**
- Merge order: `global` → domain-specific → per-call `extra_headers`

---

### Method 2 — Environment variables (simple / legacy)

For minimal setups, two env vars are sufficient. Create a `.env` file from the example:

```bash
cp .env.example .env
```

Edit `.env`:

```env
# Single-line JSON: domain → headers map
WEBFETCH_HEADERS={"*": {"User-Agent": "MyBot/1.0"}, "example.com": {"X-Akamai-Token": "your-token"}}

# Single-line JSON: domain → output format map
WEBFETCH_OUTPUT={"*": "raw", "example.com": "trafilatura", "news.com": "markdown"}
```

> **Note:** `WEBFETCH_CONFIG` takes precedence over `WEBFETCH_HEADERS` / `WEBFETCH_OUTPUT`. If both are set, the YAML file is used and the env vars are ignored.

---

## Registering with Claude Code

Add the server to `~/.claude/settings.json`:

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

If you are using the legacy `.env` method (no YAML), you can omit the `"env"` block — the server loads `.env` automatically at startup.

Restart Claude Code after saving. The tool will be available as **`mcp__webfetch__fetch`**.

---

## Testing

### 1. Verify the server starts

Run the server directly to confirm it loads your config without errors:

```bash
# Mac / Linux
WEBFETCH_CONFIG=/absolute/path/to/webfetch.yaml .venv/bin/python server.py

# Windows (PowerShell)
$env:WEBFETCH_CONFIG="C:\path\to\webfetch.yaml"; .venv\Scripts\python.exe server.py
```

Expected output (no errors):

```
INFO:__main__:Loaded config from YAML: /path/to/webfetch.yaml
INFO:__main__:Global headers: User-Agent
INFO:__main__:Domains configured: example.com, news.com
```

The server then waits for MCP messages on stdin. Press `Ctrl+C` to stop.

### 2. Run the automated test suite

```bash
# Mac / Linux
.venv/bin/pytest tests/ -v

# Windows
.venv\Scripts\pytest.exe tests\ -v
```

Expected output — all tests should pass (green):

```
tests/test_server.py::test_load_env_config_headers PASSED
tests/test_server.py::test_load_yaml_config PASSED
tests/test_server.py::test_resolve_headers_specificity PASSED
tests/test_server.py::test_resolve_output_format PASSED
tests/test_server.py::test_retry_on_5xx PASSED
...
```

The test suite covers: config loading, header resolution, output format selection, timeout/proxy/retry, header validation, text extraction, JSON formatting, and the full fetch lifecycle with mocked HTTP.

### 3. Test a live fetch manually

You can invoke the tool logic directly without running Claude Code:

```python
# quick_test.py
import asyncio, os
os.environ["WEBFETCH_CONFIG"] = "/absolute/path/to/webfetch.yaml"

from server import fetch

async def main():
    result = await fetch(url="https://httpbin.org/get")
    print(result)

asyncio.run(main())
```

Run it:

```bash
.venv/bin/python quick_test.py
```

### 4. Verify integration in Claude Code

1. Open Claude Code and start a new conversation.
2. Type: `Which MCP tools do you have available?`
3. Claude should list `mcp__webfetch__fetch` among the tools.
4. Test it with: `Fetch https://httpbin.org/get and show me the raw response.`
5. Check that the response shows `Injected headers:` with your configured values.

---

## Using with Claude

The tool is named `mcp__webfetch__fetch`. Claude uses it automatically when asked to fetch a URL. You do not need to reference the tool name in your prompts.

### Tool parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | `str` | — | URL to fetch (required) |
| `method` | `str` | `"GET"` | HTTP verb (`GET`, `POST`, `PUT`, etc.) |
| `body` | `str \| None` | `None` | Request body for `POST`/`PUT` |
| `extra_headers` | `dict \| None` | `None` | Additional per-call headers (merged on top of config) |
| `extract_text` | `bool` | `False` | Strip all HTML tags and return clean text (legacy) |
| `max_bytes` | `int` | `0` | Truncate response at N bytes (0 = unlimited) |
| `follow_redirects` | `bool` | `True` | Follow HTTP redirects |
| `output_format` | `str \| None` | `None` | Override format for this call: `"raw"` \| `"markdown"` \| `"trafilatura"` \| `"json"` |

### Response format

```
Status: 200
Injected headers: User-Agent, X-Akamai-Token
Retry attempts: 1/3

<!DOCTYPE html>...
```

---

## Use cases

### Case 1 — Scraping a site protected by Akamai Bot Manager

Some e-commerce or media sites reject requests without specific Akamai authentication headers.

`webfetch.yaml`:
```yaml
global:
  headers:
    User-Agent: "Mozilla/5.0 (compatible; MyResearchBot/1.0)"

domains:
  shop.example.com:
    headers:
      X-Akamai-Token: "your-akamai-sensor-data"
      X-Akamai-Session-Id: "your-session-id"
    output_format: trafilatura
    timeout: 60
    retry:
      attempts: 3
      backoff: 2.0
```

Prompt Claude:
> "Fetch the product page at https://shop.example.com/product/123 and summarize the key specs and price."

Claude will automatically use the injected Akamai headers, and the `trafilatura` format will extract only the main product content.

---

### Case 2 — Reading news articles as clean Markdown

News sites have heavy HTML with ads and navigation. Use `trafilatura` to extract just the article body.

`webfetch.yaml`:
```yaml
global:
  output_format: raw

domains:
  bbc.com:
    output_format: trafilatura
  nytimes.com:
    output_format: trafilatura
  medium.com:
    output_format: markdown
```

Prompt Claude:
> "Read the article at https://www.bbc.com/news/... and give me a 3-point summary."

The extracted Markdown uses far fewer tokens than raw HTML, leaving more context space for Claude's analysis.

---

### Case 3 — Calling a REST API with Bearer token authentication

`webfetch.yaml`:
```yaml
domains:
  api.myservice.com:
    headers:
      Authorization: "Bearer eyJhbGciOiJIUzI1NiIs..."
      Accept: "application/json"
    output_format: json
    timeout: 10
    retry:
      attempts: 3
      backoff: 1.5
```

Prompt Claude:
> "Call https://api.myservice.com/v1/users and list all users with their email addresses."

The `json` output format pretty-prints the response, making it easier for Claude to parse structured data.

---

### Case 4 — Accessing an internal corporate network via proxy

For resources only reachable through a corporate HTTP proxy:

`webfetch.yaml`:
```yaml
domains:
  internal.corp:
    proxy: "http://proxy.corp:8080"
    headers:
      Authorization: "Bearer my-intranet-token"
    output_format: markdown
    timeout: 45
```

Prompt Claude:
> "Fetch the internal wiki page at https://wiki.internal.corp/project-spec and extract the requirements section."

---

### Case 5 — Multi-step research pipeline across multiple domains

When Claude needs to gather information from several sites in one session, each domain gets its own config automatically — no manual switching needed.

Example session:
> "Research the latest news about topic X. Check bbc.com for breaking news, then look at the official API at api.datasource.com for raw data, and finally summarize everything."

Claude will:
1. Fetch BBC with `trafilatura` format (clean article text)
2. Fetch the API with `json` format and Bearer token
3. Combine and summarize — each request used the correct domain config transparently

---

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| `Invalid JSON in WEBFETCH_HEADERS` | Multi-line or malformed JSON | Ensure the value is a single-line valid JSON object |
| Tool not visible in Claude Code | Wrong path in `settings.json` | Use absolute paths; verify with `which python` or `where python` |
| Tool not visible in Claude Code | Server failed to start | Run the server manually (see Testing §1) and check for startup errors |
| Requests still getting blocked | Headers not applied | Confirm domain suffix matches; check server startup log for `Domains configured:` |
| Slow or hanging requests | Timeout too low | Increase `timeout` in YAML for that domain |
| `RuntimeError: invalid header value` | Control characters in header value | Remove `\r`, `\n`, or NUL characters from header values |
| Server fails to start | Python version too old | Run `python --version`; must be 3.10 or newer |
| `ModuleNotFoundError` | Dependencies not installed | Run `pip install -r requirements.txt` inside the venv |

---

## Security

- `webfetch.yaml` and `.env` are git-ignored — tokens never enter version control
- Headers are only injected for matching domains — unrelated requests receive only global headers
- All header names and values are validated to prevent CRLF/NUL injection before sending
- Secrets stay on your local machine; the server communicates over stdio, not a network port

---

## License

MIT
