# WebFetch MCP Server

Local Python MCP server that replaces Claude's built-in WebFetch tool.
Main purpose: inject **domain-scoped custom HTTP headers** into every outbound request,
used to authenticate against Akamai bot-defender on specific domains.

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

`WEBFETCH_HEADERS` is a **single-line JSON object** with domain-scoped headers:

```env
WEBFETCH_HEADERS={"*": {"User-Agent": "MyBot/1.0"}, "example.com": {"X-Akamai-Token": "TOKEN"}}
```

| Key | Meaning |
|-----|---------|
| `"*"` | Applied to **every** request (global) |
| `"example.com"` | Applied only when hostname ends with `example.com` |

Merge order (later wins): `*` → domain-specific → per-call `extra_headers`.

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
    extract_text: bool = False,  # strip HTML → clean text
    max_bytes: int = 0,          # truncate response (0 = unlimited)
) -> str
```

Response format:
```
Status: 200
Injected headers: User-Agent, X-Akamai-Token

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

## Key implementation notes (`server.py`)

- `_load_header_config()` — parses `WEBFETCH_HEADERS` at startup; raises `RuntimeError` on invalid JSON.
- `_resolve_headers(hostname, extra_headers)` — merges global + domain + per-call headers.
  Domain matching: `hostname == key or hostname.endswith("." + key)`.
  Multiple matches are applied longest-key-last (most specific wins).
- `_extract_text(html)` — regex tag stripping + whitespace collapse.
- Uses `httpx.AsyncClient` with `follow_redirects=True`.

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `mcp[cli]` | MCP server framework (FastMCP) |
| `httpx` | Async HTTP client |
| `python-dotenv` | Load `.env` at startup |
