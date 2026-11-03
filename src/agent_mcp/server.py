"""FastMCP server exposing OpenCode-compatible tools over HTTP or stdio."""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, AuthProvider
from fastmcp.utilities.types import File, Image
from mcp.types import ToolAnnotations

from .context import ToolError, Workspace
from .tools.files import edit as edit_file
from .tools.files import patch as patch_file
from .tools.files import read as read_file
from .tools.files import write as write_file
from .tools.search import glob_paths, grep_paths
from .tools.shell import DEFAULT_TIMEOUT_MS, ShellRunner
from .tools.web import webfetch as webfetch_tool
from .tools.web import websearch as websearch_tool

__version__ = "0.2.0"

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

INSTRUCTIONS = """\
This server mirrors the file, search, and execution tools built into OpenCode.

Paths are resolved against the server's workspace root. Relative paths are
joined to that root; absolute paths are accepted only inside it unless the
operator enabled outside-root access.

The patch tool accepts the OpenCode/Codex envelope:

  *** Begin Patch
  *** Add File: path
  +content
  *** Update File: path
  @@
   context
  -old
  +new
  *** End Patch

The shell tool runs with the server user's privileges. Background commands
return a PID and a log path that can be read with the read tool.
"""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class ServerConfig:
    root: Path = field(default_factory=lambda: Path.cwd())
    transport: str = "http"
    host: str = "127.0.0.1"
    port: int = 8765
    path: str = "/mcp"
    token: str | None = None
    no_auth: bool = False
    allow_outside_root: bool = False
    allow_private_web: bool = False
    shell: str | None = None
    max_read_bytes: int = 10 * 1024 * 1024
    stateless: bool = False
    json_response: bool = False
    allowed_hosts: list[str] = field(default_factory=list)
    allowed_origins: list[str] = field(default_factory=list)
    public_url: str | None = None
    log_level: str = "INFO"


# ---------------------------------------------------------------------------
# Static bearer auth
# ---------------------------------------------------------------------------


class StaticBearerAuth(AuthProvider):
    """A minimal MCP auth provider that accepts one static bearer token.

    FastMCP handles the 401 response and ``WWW-Authenticate`` header.  OAuth is
    deliberately not advertised: these deployments use a pre-shared token.
    """

    def __init__(self, token: str, base_url: str | None = None) -> None:
        super().__init__(base_url=base_url)
        if len(token) < 16:
            raise ValueError("The bearer token must be at least 16 characters")
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if secrets.compare_digest(token, self._token):
            return AccessToken(token=token, client_id="agent-mcp", scopes=[])
        return None


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def create_server(config: ServerConfig) -> FastMCP:
    workspace = Workspace(config.root, allow_outside=config.allow_outside_root)
    runner = ShellRunner(workspace, shell=config.shell)

    auth: AuthProvider | None = None
    if config.token and not config.no_auth:
        base_url = config.public_url or f"http://{config.host}:{config.port}"
        auth = StaticBearerAuth(config.token, base_url=base_url)

    mcp = FastMCP(
        name="agent-mcp",
        version=__version__,
        instructions=INSTRUCTIONS,
        auth=auth,
        mask_error_details=False,
        strict_input_validation=True,
    )

    read_only = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
    mutating = ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False
    )
    network = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

    # -- read ---------------------------------------------------------------
    @mcp.tool(
        name="read",
        description=(
            "Read a file or list a directory. Text is returned with line numbers. "
            "Use offset (1-based) and limit to page through large files."
        ),
        annotations=read_only,
        output_schema=None,
    )
    def read(
        path: str,
        offset: int | None = None,
        limit: int | None = None,
    ):
        return read_file(
            workspace, path, offset, limit, max_bytes=config.max_read_bytes
        )

    # -- glob ---------------------------------------------------------------
    @mcp.tool(
        name="glob",
        description=(
            "Find files by glob pattern such as '**/*.ts'. Hidden files are skipped "
            "unless hidden=true. Results are newest first."
        ),
        annotations=read_only,
    )
    def glob(
        pattern: str,
        path: str | None = None,
        hidden: bool = False,
        limit: int = 100,
    ) -> str:
        return glob_paths(workspace, pattern, path=path, hidden=hidden, limit=limit)

    # -- grep ---------------------------------------------------------------
    @mcp.tool(
        name="grep",
        description=(
            "Search file contents with a regular expression (or literal text). "
            "Narrow with path and include, e.g. include='*.ts'. Returns path:line: text."
        ),
        annotations=read_only,
    )
    def grep(
        pattern: str,
        path: str | None = None,
        include: str | None = None,
        caseSensitive: bool = True,
        literal: bool = False,
        limit: int = 100,
    ) -> str:
        return grep_paths(
            workspace,
            pattern,
            path=path,
            include=include,
            caseSensitive=caseSensitive,
            literal=literal,
            limit=limit,
        )

    # -- edit ---------------------------------------------------------------
    @mcp.tool(
        name="edit",
        description=(
            "Replace exact text in a file. oldString must match exactly once unless "
            "replaceAll=true. The file's encoding and newline style are preserved. "
            "Returns a unified diff."
        ),
        annotations=mutating,
    )
    def edit(
        path: str,
        oldString: str,
        newString: str,
        replaceAll: bool = False,
    ) -> str:
        return edit_file(workspace, path, oldString, newString, replaceAll)

    # -- write --------------------------------------------------------------
    @mcp.tool(
        name="write",
        description=(
            "Create or completely replace a text file. Missing parent directories "
            "are created. Prefer edit for small changes to existing files."
        ),
        annotations=mutating,
    )
    def write(path: str, content: str) -> str:
        return write_file(workspace, path, content)

    # -- patch --------------------------------------------------------------
    @mcp.tool(
        name="patch",
        description=(
            "Apply an OpenCode/Codex patch envelope with *** Begin Patch, "
            "*** Add File, *** Update File, *** Move to, and *** Delete File. "
            "All hunks are validated before any file is written."
        ),
        annotations=mutating,
    )
    def patch(patchText: str) -> str:
        return patch_file(workspace, patchText)

    # -- shell --------------------------------------------------------------
    @mcp.tool(
        name="shell",
        description=(
            "Run a command in the host shell. timeout is in milliseconds (0 disables "
            "it). Set background=true to start a long-running process; the result "
            "contains its PID and log path."
        ),
        annotations=mutating,
    )
    def shell(
        command: str,
        workdir: str | None = None,
        timeout: int = DEFAULT_TIMEOUT_MS,
        background: bool = False,
    ) -> str:
        return runner.run(
            command, workdir=workdir, timeout=timeout, background=background
        )

    # -- webfetch -----------------------------------------------------------
    @mcp.tool(
        name="webfetch",
        description=(
            "Read a web page as clean Markdown: title, author/date metadata, and "
            "the main content without navigation, ads, cookie banners or footers. "
            "Prefers Markdown versions, adds /llms.txt for site roots, and pages "
            "long results with offset. PDFs and images are returned as files. "
            "Private and loopback addresses are blocked unless the operator allows them."
        ),
        annotations=network,
        output_schema=None,
    )
    def webfetch(
        url: str,
        format: str = "markdown",
        timeout: int = 30,
        offset: int = 0,
    ) -> str | File | Image:
        return webfetch_tool(
            url,
            format=format,
            timeout=timeout,
            offset=offset,
            allow_private=config.allow_private_web,
        )

    # -- websearch ----------------------------------------------------------
    @mcp.tool(
        name="websearch",
        description=(
            "Search the web through DuckDuckGo. Returns ranked title, URL and "
            "snippet results. Use recency to limit results to the last day, week, "
            "month or year, or news=true for news articles. No API key is needed."
        ),
        annotations=network,
    )
    def websearch(
        query: str,
        max_results: int = 8,
        recency: Literal["day", "week", "month", "year"] | None = None,
        news: bool = False,
    ) -> str:
        return websearch_tool(query, max_results, recency, news)

    return mcp


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_token() -> str | None:
    return os.environ.get("AGENT_MCP_TOKEN") or os.environ.get("MCP_AUTH_TOKEN")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-mcp",
        description=(
            "OpenCode-compatible file, search, shell, and web tools over MCP. "
            "Defaults to Streamable HTTP on 127.0.0.1:8765/mcp."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--root",
        default=os.getcwd(),
        help="Workspace root for all file operations (default: current directory)",
    )
    parser.add_argument(
        "--transport",
        choices=("http", "stdio"),
        default="http",
        help="MCP transport (default: http)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind address")
    parser.add_argument("--port", type=int, default=8765, help="HTTP port")
    parser.add_argument("--path", default="/mcp", help="HTTP MCP endpoint path")
    parser.add_argument(
        "--token",
        default=_default_token(),
        help=(
            "Static bearer token. Defaults to AGENT_MCP_TOKEN or MCP_AUTH_TOKEN. "
            "Clients send 'Authorization: Bearer <token>'."
        ),
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="Explicitly run HTTP without authentication (loopback only)",
    )
    parser.add_argument(
        "--allow-outside-root",
        action="store_true",
        help="Allow paths outside --root (dangerous)",
    )
    parser.add_argument(
        "--allow-private-web",
        action="store_true",
        help=(
            "Allow webfetch to reach loopback/private addresses "
            "(cloud metadata addresses stay blocked)"
        ),
    )
    parser.add_argument(
        "--shell",
        default=os.environ.get("AGENT_MCP_SHELL"),
        help="Shell executable/command (default: platform shell)",
    )
    parser.add_argument(
        "--max-read-bytes",
        type=int,
        default=10 * 1024 * 1024,
        help="Largest file read will load (default: 10 MiB)",
    )
    parser.add_argument(
        "--stateless",
        action="store_true",
        help="Serve Streamable HTTP without MCP session IDs",
    )
    parser.add_argument(
        "--json-response",
        action="store_true",
        help="Return application/json instead of request-scoped SSE",
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help="Additional Host header value to accept (repeatable)",
    )
    parser.add_argument(
        "--allowed-origin",
        action="append",
        default=[],
        help="Additional browser Origin to accept (repeatable)",
    )
    parser.add_argument(
        "--public-url",
        default=None,
        help="Public base URL for auth metadata when behind a proxy",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        help="Log level for the HTTP server",
    )
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="Print the resolved configuration as JSON and exit",
    )
    return parser


def _redacted_config(config: ServerConfig) -> dict[str, object]:
    return {
        "root": str(config.root),
        "transport": config.transport,
        "host": config.host,
        "port": config.port,
        "path": config.path,
        "auth": bool(config.token) and not config.no_auth,
        "allow_outside_root": config.allow_outside_root,
        "allow_private_web": config.allow_private_web,
        "shell": config.shell or "(platform default)",
        "max_read_bytes": config.max_read_bytes,
        "stateless": config.stateless,
        "json_response": config.json_response,
        "allowed_hosts": config.allowed_hosts,
        "allowed_origins": config.allowed_origins,
        "public_url": config.public_url,
        "log_level": config.log_level,
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    config = ServerConfig(
        root=Path(args.root).expanduser(),
        transport=args.transport,
        host=args.host,
        port=args.port,
        path=args.path if args.path.startswith("/") else "/" + args.path,
        token=args.token,
        no_auth=args.no_auth,
        allow_outside_root=args.allow_outside_root,
        allow_private_web=args.allow_private_web,
        shell=args.shell,
        max_read_bytes=args.max_read_bytes,
        stateless=args.stateless,
        json_response=args.json_response,
        allowed_hosts=args.allowed_host,
        allowed_origins=args.allowed_origin,
        public_url=args.public_url,
        log_level=args.log_level,
    )

    if args.print_config:
        print(json.dumps(_redacted_config(config), indent=2))
        return 0

    if not config.root.exists() or not config.root.is_dir():
        parser.error(f"--root is not a directory: {config.root}")

    if config.token and len(config.token) < 16:
        parser.error("--token must be at least 16 characters")

    if config.transport == "http" and not config.token and not config.no_auth:
        if config.host not in LOOPBACK_HOSTS:
            parser.error(
                "Refusing to bind a non-loopback address without authentication. "
                "Set AGENT_MCP_TOKEN/--token, or pass --no-auth explicitly."
            )
        logging.getLogger("agent-mcp").warning(
            "HTTP server has no authentication. It is bound to %s; use --token "
            "before exposing it to a network.",
            config.host,
        )

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        mcp = create_server(config)
    except (ToolError, ValueError) as exc:
        parser.error(str(exc))
        return 2  # pragma: no cover

    if config.transport == "stdio":
        mcp.run(transport="stdio", show_banner=False)
        return 0

    mcp.run(
        transport="http",
        host=config.host,
        port=config.port,
        path=config.path,
        stateless_http=config.stateless,
        json_response=config.json_response,
        allowed_hosts=config.allowed_hosts or None,
        allowed_origins=config.allowed_origins or None,
        log_level=config.log_level.lower(),
        show_banner=False,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
