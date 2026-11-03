from __future__ import annotations

from pathlib import Path

import pytest

from agent_mcp.context import ToolError, Workspace
from agent_mcp.tools import search


@pytest.fixture()
def ws(tmp_path: Path) -> Workspace:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def needle():\n    return 'a.b'\n", encoding="utf-8")
    (tmp_path / "src" / "b.ts").write_text("const needle = 1;\n", encoding="utf-8")
    (tmp_path / ".hidden.py").write_text("needle hidden\n", encoding="utf-8")
    (tmp_path / "src" / ".secret.py").write_text("needle secret\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("Needle upper\nneedle lower\n", encoding="utf-8")
    return Workspace(tmp_path)


def test_glob_finds_files(ws: Workspace) -> None:
    result = search.glob_paths(ws, "**/*.py")
    assert "src/a.py" in result
    assert ".hidden.py" not in result
    assert ".secret.py" not in result


def test_glob_hidden(ws: Workspace) -> None:
    result = search.glob_paths(ws, "**/*.py", hidden=True)
    assert ".hidden.py" in result
    assert "src/.secret.py" in result


def test_glob_limit(ws: Workspace) -> None:
    result = search.glob_paths(ws, "**/*", limit=1)
    assert "limited to 1" in result or result.count("\n") <= 2


def test_glob_no_matches(ws: Workspace) -> None:
    assert "No files found" in search.glob_paths(ws, "**/*.rs")


def test_glob_absolute_pattern_cannot_escape_root(ws: Workspace, tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="outside the workspace root"):
        search.glob_paths(ws, str(tmp_path.parent / "**" / "*.py"))


def test_glob_absolute_pattern_inside_root(ws: Workspace, tmp_path: Path) -> None:
    result = search.glob_paths(ws, str(tmp_path / "**" / "*.py"))
    assert "src/a.py" in result


def test_grep_regex(ws: Workspace) -> None:
    result = search.grep_paths(ws, r"def\s+needle")
    assert "src/a.py:1:" in result


def test_grep_literal(ws: Workspace) -> None:
    result = search.grep_paths(ws, "a.b", literal=True)
    assert "src/a.py:2:" in result
    # A regex interpretation would also match "aXb", but the fixture has none;
    # verify the literal path does not treat the dot as a wildcard.
    assert "notes.txt" not in result


def test_grep_include(ws: Workspace) -> None:
    result = search.grep_paths(ws, "needle", include="*.ts")
    assert "src/b.ts" in result
    assert "src/a.py" not in result


def test_grep_case_sensitive(ws: Workspace) -> None:
    sensitive = search.grep_paths(ws, "Needle")
    assert "notes.txt:1:" in sensitive
    assert "notes.txt:2:" not in sensitive

    insensitive = search.grep_paths(ws, "Needle", caseSensitive=False)
    assert "notes.txt:1:" in insensitive
    assert "notes.txt:2:" in insensitive


def test_grep_skips_hidden_and_ignored(ws: Workspace, tmp_path: Path) -> None:
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("needle\n", encoding="utf-8")
    result = search.grep_paths(ws, "needle", include="*.js")
    assert "No matches" in result


def test_grep_invalid_regex(ws: Workspace) -> None:
    with pytest.raises(ToolError, match="Invalid regular expression"):
        search.grep_paths(ws, "(")


def test_grep_single_file(ws: Workspace) -> None:
    result = search.grep_paths(ws, "needle", path="src/b.ts")
    assert "src/b.ts:1:" in result


def test_grep_no_matches(ws: Workspace) -> None:
    assert "No matches found" in search.grep_paths(ws, "zzz-not-there")


def test_grep_python_fallback_reads_utf16(ws: Workspace, tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "utf16.txt").write_bytes("needle in utf16\n".encode("utf-16-le"))
    monkeypatch.setattr(search.shutil, "which", lambda name: None)
    result = search.grep_paths(ws, "needle")
    assert "utf16.txt:1:" in result
