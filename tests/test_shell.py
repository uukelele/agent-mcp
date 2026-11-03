from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from agent_mcp.context import ToolError, Workspace
from agent_mcp.tools.shell import ShellRunner, _decode_output


@pytest.fixture()
def ws(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path)


@pytest.fixture()
def runner(ws: Workspace) -> ShellRunner:
    return ShellRunner(ws)


def _py(code: str) -> str:
    if os.name == "nt":
        # PowerShell needs the call operator before a quoted executable path.
        return f'& "{sys.executable}" -c "{code}"'
    return f'"{sys.executable}" -c "{code}"'


def test_shell_echo(runner: ShellRunner) -> None:
    result = runner.run(_py("print('shell-ok')"))
    assert "shell-ok" in result


def test_shell_exit_code(runner: ShellRunner) -> None:
    result = runner.run(_py("raise SystemExit(3)"))
    assert "[exit code: 3]" in result


def test_shell_workdir(runner: ShellRunner, tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    result = runner.run(_py("import os; print(os.getcwd())"), workdir="sub")
    assert str(tmp_path / "sub") in result


def test_shell_workdir_outside_root(runner: ShellRunner) -> None:
    with pytest.raises(ToolError, match="outside the workspace root"):
        runner.run(_py("print(1)"), workdir=str(Path.home()))


def test_shell_timeout_kills_process(runner: ShellRunner) -> None:
    started = time.monotonic()
    result = runner.run(_py("import time; time.sleep(5)"), timeout=300)
    elapsed = time.monotonic() - started
    assert "timed out" in result
    assert elapsed < 4


def test_shell_background_writes_log(runner: ShellRunner, tmp_path: Path) -> None:
    result = runner.run(
        _py("import time; time.sleep(0.2); print('background-ok')"),
        background=True,
        timeout=5000,
    )
    assert "Started background command" in result
    log_line = next(line for line in result.splitlines() if line.startswith("Log: "))
    log_path = Path(log_line.removeprefix("Log: "))
    assert log_path.is_relative_to(tmp_path)
    deadline = time.monotonic() + 5
    content = ""
    while time.monotonic() < deadline:
        if log_path.exists():
            content = log_path.read_text(encoding="utf-8", errors="replace")
            if "background-ok" in content:
                break
        time.sleep(0.1)
    assert "background-ok" in content


def test_decode_utf16_output() -> None:
    assert _decode_output("hello".encode("utf-16")) == "hello"
    assert _decode_output("hello".encode("utf-16-le")) == "hello"
