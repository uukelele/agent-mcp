# agent-mcp

[![tests](https://github.com/uukelele/agent-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/uukelele/agent-mcp/actions/workflows/tests.yml)

A cross-platform [FastMCP](https://gofastmcp.com/) server that exposes file,
search, shell, and web tools with the **same names and input fields** as the
built-in tools in [OpenCode](https://opencode.ai). It runs over **Streamable
HTTP** (with a static bearer token) or stdio, on Linux, macOS, and Windows.

Use it to give an agent controlled access to another machine, or to replace a
subset of local tools with a single authenticated MCP endpoint.

```text
OpenCode / any MCP client
        │  Authorization: Bearer <token>
        ▼
  http://127.0.0.1:8765/mcp
        │
        ├── read / write / edit / patch
        ├── glob / grep
        ├── shell
        └── webfetch / websearch
```

## Tool compatibility

| Tool | Parameters (same names as OpenCode) | Behavior |
|---|---|---|
| `read` | `path`, `offset?`, `limit?` | Numbered lines, 2,000-line / 50 KiB pages, directory listings, images/audio/PDF as MCP media |
| `glob` | `pattern`, `path?`, `hidden?`, `limit?` | Recursive glob, hidden filtering, newest first |
| `grep` | `pattern`, `path?`, `include?`, `caseSensitive?`, `literal?`, `limit?` | Regex or literal content search; uses `rg` when available, Python fallback |
| `edit` | `path`, `oldString`, `newString`, `replaceAll?` | Exact replacement; errors on ambiguous matches unless `replaceAll=true`; returns a diff |
| `write` | `path`, `content` | Create or replace a file, creating parents |
| `patch` | `patchText` | `*** Begin Patch` envelope: add, update, move, delete |
| `shell` | `command`, `workdir?`, `timeout?`, `background?` | Host shell, millisecond timeout, process-tree kill, background log |
| `webfetch` | `url`, `format?`, `timeout?` | `markdown`, `text`, or `html`; blocks private addresses by default |
| `websearch` | `query` | Brave (`BRAVE_API_KEY`) or Tavily (`TAVILY_API_KEY`) |

Not mirrored: `question`, `skill`, `subagent`, and `execute` are client-side
tools. `websearch` needs a provider key; it returns a clear error otherwise.

## Install

Requires Python 3.10+.

```bash
# from this checkout, in a project virtualenv
uv venv && uv pip install -e .

# or install the CLI in an isolated environment from the checkout
pipx install .
# uv tool install .

# or install directly from the public repository
pipx install "git+https://github.com/uukelele/agent-mcp.git"
```

## Run over HTTP

```bash
export AGENT_MCP_TOKEN="$(openssl rand -hex 32)"

agent-mcp \
  --root /srv/project \
  --host 127.0.0.1 \
  --port 8765 \
  --token "$AGENT_MCP_TOKEN"
```

Endpoint: `http://127.0.0.1:8765/mcp`.

- Every request must send `Authorization: Bearer <token>`.
- Binding a non-loopback address without a token is refused unless you pass
  `--no-auth` explicitly.
- `--stateless` removes MCP session IDs (useful behind buffered proxies).
- `--json-response` returns `application/json` instead of request-scoped SSE.
- `--allowed-host` / `--allowed-origin` extend FastMCP's Host/Origin guard when
  running behind a reverse proxy.

For a public deployment, terminate TLS in Caddy/nginx and forward to
`127.0.0.1:8765`; do not send bearer tokens over plain HTTP across a network.

## Run with Docker

```bash
export AGENT_MCP_TOKEN="$(openssl rand -hex 32)"
mkdir -p workspace
docker compose up -d
```

The compose file publishes only `127.0.0.1:8765` and mounts `./workspace` as
the sandbox root. The container runs as an unprivileged `mcp` user, but `shell`
still executes inside the container with that user's full access to the mounted
workspace. Add `--allow-outside-root`, `--allow-private-web`, or other flags to
the compose `command` only when you need them.

## Run over stdio

```bash
agent-mcp --transport stdio --root /srv/project
```

OpenCode starts it for you:

```jsonc
{
  "mcp": {
    "servers": {
      "tools": {
        "type": "local",
        "command": ["agent-mcp", "--transport", "stdio", "--root", "/srv/project"]
      }
    }
  }
}
```

## Connect from OpenCode (HTTP)

Because this server uses a static header token and does not advertise OAuth,
set `"oauth": false`:

```jsonc title="opencode.jsonc"
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "tools": {
        "type": "remote",
        "url": "http://127.0.0.1:8765/mcp",
        "oauth": false,
        "headers": {
          "Authorization": "Bearer {env:AGENT_MCP_TOKEN}"
        }
      }
    }
  }
}
```

CLI equivalent:

```bash
opencode mcp add tools --url http://127.0.0.1:8765/mcp \
  --header 'Authorization=Bearer {env:AGENT_MCP_TOKEN}'
# `mcp add` does not write oauth:false; add it to the generated entry.
opencode mcp list
```

Tool names are prefixed with the server name (for example `tools_read`,
`tools_shell`). Under OpenCode's default Code Mode they are grouped under the
server namespace. Set `"codemode": false` on the server if you want them exposed
directly in the native tool list.

## Authentication

- `--token` / `AGENT_MCP_TOKEN` / `MCP_AUTH_TOKEN` sets the only accepted
  bearer token. Tokens shorter than 16 characters are rejected.
- The token is compared with `secrets.compare_digest`.
- `--no-auth` is required to run HTTP without authentication. It is only
  sensible on loopback.
- `--print-config` shows the resolved settings with the token redacted.

## Cross-platform behavior

- **Shell:** on Linux/macOS the default is `$SHELL -lc` (falling back to
  `bash`/`sh`); on Windows it is PowerShell (`pwsh`/`powershell`) with
  `-NoProfile -NonInteractive -Command`, falling back to `cmd.exe /d /s /c`.
  Override with `--shell` or `AGENT_MCP_SHELL`. Background commands write
  their log to `<root>/.agent-mcp/shell/`, so `read` can access it
  inside the sandbox; add that directory to `.gitignore`.
- **Process trees:** timeouts kill the whole tree with `killpg` on POSIX and
  `taskkill /T` on Windows.
- **Encoding:** UTF-8, UTF-8/UTF-16/UTF-32 BOMs, BOM-less UTF-16, and the
  platform ANSI code page are detected. Edits round-trip the original encoding
  and BOM byte-for-byte.
- **Newlines:** CRLF, LF, and CR are preserved when editing or patching.
- **Paths:** relative paths resolve against `--root`; absolute paths are checked
  with `commonpath`, including Windows drive letters. Glob/grep display
  workspace-relative POSIX paths for consistent agent output.
- **Atomic writes:** temp file in the target directory, `fsync`, then
  `os.replace`; existing permissions are preserved.

## Patch format

```text
*** Begin Patch
*** Add File: hello.txt
+hello
*** Update File: hello.txt
@@
 hello
-world
+there
*** Update File: old.txt
*** Move to: new.txt
@@
-a
+b
*** Delete File: gone.txt
*** End Patch
```

Every operation is parsed and every hunk is matched **before** anything is
written. A missing context line aborts the whole patch. New files are written
as UTF-8 with LF; updates keep the file's existing encoding and newline style.

## Security model

- File access is confined to `--root` unless `--allow-outside-root` is set.
  Symlink targets are resolved before the containment check.
- `shell` is **not sandboxed**. It runs as the server user with that user's full
  privileges. Run the server as a dedicated, unprivileged account and restrict
  network access.
- `webfetch` blocks loopback, private, link-local, and reserved IPs by default
  (including redirect targets). `--allow-private-web` disables that guard.
- `websearch` only calls Brave or Tavily; no provider key means no request.
- `shell` inherits the server environment, including any provider API keys
  stored there. Run the service under a dedicated account and keep unrelated
  secrets out of its environment.
- Keep the token in an environment variable, not in a world-readable config.

## Limitations

- `read` returns PDFs as embedded resources rather than extracting text.
- `grep` uses Python's `re` engine in the fallback path: lookaround works, but
  `\p{...}` and some PCRE syntax do not. With ripgrep, matching uses ripgrep's
  Rust regex syntax (no lookaround unless `--pcre2` is added) but is faster.
  Ripgrep detects UTF-16 with a BOM; BOM-less UTF-16 is covered by the Python
  fallback. Anchor patterns or narrow with `include` for predictable results.
- `glob` returns files, not directories.
- Background shell commands return immediately; there is no completion
  notification. Poll the returned log path with `read`.
- The server does not implement OpenCode's permission prompts, snapshots, or
  `patch`-for-GPT-only policy. Your MCP client decides when to call a tool.

## Development

```bash
uv venv
uv pip install -e ".[dev]"   # or: uv pip install -e . pytest httpx
.venv/bin/pytest
.venv/bin/agent-mcp --print-config
```

## License

MIT
