from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

TOKEN = "test-token-with-enough-characters"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, process: subprocess.Popen[str], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr else ""
            raise RuntimeError(f"server exited early:\n{stderr}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("server did not start in time")


@pytest.fixture()
def server(tmp_path: Path):
    (tmp_path / "hello.txt").write_text("hello from http\n", encoding="utf-8")
    port = _free_port()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agent_mcp",
            "--root",
            str(tmp_path),
            "--port",
            str(port),
            "--token",
            TOKEN,
            "--stateless",
            "--json-response",
            "--log-level",
            "ERROR",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_port(port, process)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()


def _post(url: str, payload: dict, token: str | None, session_id: str | None = None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if session_id:
        headers["mcp-session-id"] = session_id
    with httpx.Client(timeout=10) as client:
        return client.post(url, json=payload, headers=headers)


def _initialize(url: str, token: str | None):
    return _post(
        url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1.0"},
            },
        },
        token,
    )


def test_http_requires_bearer(server: str) -> None:
    response = _initialize(server, None)
    assert response.status_code == 401
    assert "www-authenticate" in {key.lower() for key in response.headers}

    response = _initialize(server, "wrong-token-but-long-enough")
    assert response.status_code == 401


def test_http_initialize_and_list_tools(server: str) -> None:
    response = _initialize(server, TOKEN)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["result"]["serverInfo"]["name"] == "agent-mcp"

    session_id = response.headers.get("mcp-session-id")
    response = _post(
        server,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        TOKEN,
        session_id,
    )
    assert response.status_code == 200, response.text
    names = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert {
        "read",
        "glob",
        "grep",
        "edit",
        "write",
        "patch",
        "shell",
        "webfetch",
        "websearch",
    } <= names


def test_http_call_read(server: str) -> None:
    response = _initialize(server, TOKEN)
    session_id = response.headers.get("mcp-session-id")
    response = _post(
        server,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "read", "arguments": {"path": "hello.txt"}},
        },
        TOKEN,
        session_id,
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    text = "".join(
        item.get("text", "") for item in result.get("content", []) if item.get("type") == "text"
    )
    assert "hello from http" in text
