# claude-webfetch-mcp

A local Python MCP server that replaces Claude's built-in `WebFetch` tool, adding support for **domain-scoped custom HTTP headers** — useful for authenticating against services like Akamai Bot Manager that require specific headers per domain.

## Why

Claude's built-in WebFetch tool sends requests without custom headers, which means it gets blocked by bot-protection systems (Akamai, etc.). This server acts as a drop-in replacement: it exposes the same `fetch` tool to Claude, but injects the right authentication headers automatically based on the target domain.

## Features

- **Domain-scoped headers** — different headers per domain, plus a global `*` fallback
- **Per-call headers** — Claude (or you) can pass extra headers at call time
- **HTML text extraction** — optional stripping of HTML tags for cleaner LLM context
- **Response size limit** — optional truncation to avoid blowing up Claude's context window
- **Env-based config** — secrets stay in `.env`, never in code

## Requirements

- Python 3.10+
- [Claude Code](https://claude.ai/code)

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/claude-webfetch-mcp.git
cd claude-webfetch-mcp

python -m venv .venv

# Windows
.venv\Scripts\pip install -r requirements.txt

# Mac / Linux
.venv/bin/pip install -r requirements.txt
```

## Configuration

Copy the example env file and fill in your tokens:

```bash
cp .env.example .env
```

Edit `.env` — the value must be **a single-line JSON object**:

```env
WEBFETCH_HEADERS={"*": {"User-Agent": "MyBot/1.0"}, "example.com": {"X-Akamai-Token": "your-token"}}
```

### Header scoping rules

| Key | When applied |
|-----|-------------|
| `"*"` | Every request |
| `"example.com"` | Requests whose hostname ends with `example.com` (matches `www.example.com` too) |

Multiple domain keys can coexist. Merge order (later wins on conflict):
**global `*`** → **domain-specific** → **per-call `extra_headers`**

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

> **Windows:** use `.venv\Scripts\python.exe`

Restart Claude Code. The tool will be available as `mcp__webfetch__fetch`.

## Tool reference

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | `str` | — | URL to fetch |
| `method` | `str` | `"GET"` | HTTP verb |
| `body` | `str \| None` | `None` | Request body (POST/PUT) |
| `extra_headers` | `dict \| None` | `None` | Additional per-call headers |
| `extract_text` | `bool` | `False` | Strip HTML, return clean text |
| `max_bytes` | `int` | `0` | Truncate response (0 = unlimited) |

### Example response

```
Status: 200
Injected headers: User-Agent, X-Akamai-Token

<!DOCTYPE html>...
```

## Security

- `.env` is git-ignored — tokens never leave your machine
- Headers are only injected for matching domains — unrelated requests get only global headers

## License

MIT
