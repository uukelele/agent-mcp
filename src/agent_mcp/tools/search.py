"""Glob and grep with OpenCode-compatible argument names."""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path

from ..context import ToolError, Workspace, decode_text, looks_binary

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
TIMEOUT_SECONDS = 30.0
MAX_GREP_FILE_BYTES = 10 * 1024 * 1024

_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "dist",
    "build",
    "target",
    ".next",
    ".nuxt",
}


def _clamp_limit(limit: int | float | None, default: int = DEFAULT_LIMIT) -> int:
    value = int(limit) if limit is not None else default
    value = max(value, 1)
    return min(value, MAX_LIMIT)


def _expand_braces(pattern: str) -> list[str]:
    match = re.search(r"\{([^{}]+)\}", pattern)
    if not match:
        return [pattern]
    head, tail = pattern[: match.start()], pattern[match.end() :]
    out: list[str] = []
    for part in match.group(1).split(","):
        out.extend(_expand_braces(head + part.strip() + tail))
    return out[:64]


def _matches_include(relative: str, name: str, include: str | None) -> bool:
    if not include:
        return True
    for pattern in _expand_braces(include):
        pattern = pattern.replace("\\", "/")
        if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(name, pattern):
            return True
    return False


def _is_hidden(relative: str) -> bool:
    return any(part.startswith(".") for part in relative.replace("\\", "/").split("/") if part)


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a glob with proper ``**`` and path-separator semantics."""

    pattern = pattern.replace("\\", "/")
    out: list[str] = ["^"]
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern[index : index + 2] == "**":
                index += 2
                if pattern[index : index + 1] == "/":
                    index += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            end = pattern.find("]", index + 1)
            if end == -1:
                out.append(r"\[")
            else:
                body = pattern[index + 1 : end]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body + "]")
                index = end
        else:
            out.append(re.escape(char))
        index += 1
    out.append("$")
    return re.compile("".join(out))


def glob_paths(
    workspace: Workspace,
    pattern: str,
    path: str | None = None,
    hidden: bool = False,
    limit: int | float | None = None,
) -> str:
    """Find files by glob pattern, newest first (OpenCode's ``glob``)."""

    base = workspace.resolve(path, must_exist=True) if path else workspace.root
    if not base.is_dir():
        raise ToolError(f"glob path is not a directory: {base}")

    if Path(pattern).is_absolute():
        # Resolve only the fixed prefix before the first wildcard.  Calling
        # Path.resolve() on the whole pattern can fail on Windows because '*'
        # and '?' are not valid path characters.
        absolute = Path(pattern)
        parts = absolute.parts
        fixed: list[str] = []
        for part in parts[1:]:
            if any(char in part for char in "*?["):
                break
            fixed.append(part)
        fixed_path = Path(absolute.anchor, *fixed)
        base = workspace.resolve(fixed_path)
        relative_pattern = "/".join(parts[1 + len(fixed) :])
        if base.is_file():
            relative_pattern = base.name
            base = base.parent
        expanded = _expand_braces(relative_pattern or "**")
    else:
        relative_pattern = pattern.replace("\\", "/")
        while relative_pattern.startswith("./"):
            relative_pattern = relative_pattern[2:]
        expanded = _expand_braces(relative_pattern)

    regexes = [_glob_to_regex(item) for item in expanded]
    cap = _clamp_limit(limit)
    deadline = time.monotonic() + TIMEOUT_SECONDS
    found: list[tuple[float, str]] = []
    seen = 0

    for root, dirs, names in os.walk(base, followlinks=False):
        if time.monotonic() > deadline:
            raise ToolError(
                f"glob timed out after {TIMEOUT_SECONDS:.0f}s; narrow the pattern or path"
            )
        root_path = Path(root)
        try:
            relative_root = root_path.relative_to(base).as_posix()
        except ValueError:
            relative_root = ""
        if not hidden:
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        else:
            dirs.sort()
        if relative_root in {"", "."}:
            relative_root = ""
        if not hidden and relative_root and _is_hidden(relative_root):
            dirs[:] = []
            continue

        for name in sorted(names):
            if not hidden and name.startswith("."):
                continue
            relative = f"{relative_root}/{name}" if relative_root else name
            if not any(regex.match(relative) for regex in regexes):
                continue
            candidate = root_path / name
            try:
                if not candidate.is_file():
                    continue
                mtime = candidate.stat().st_mtime
            except OSError:
                continue
            seen += 1
            found.append((mtime, relative))
            if len(found) >= cap * 4:
                found.sort(key=lambda item: item[0], reverse=True)
                found = found[:cap]

    found.sort(key=lambda item: (-item[0], item[1]))
    paths = [item[1] for item in found[:cap]]

    if not paths:
        return f"No files found matching {pattern!r} under {workspace.display(base)}"

    lines = [f"Found {len(paths)} file(s) matching {pattern!r}:"]
    lines.extend(paths)
    if seen > cap:
        lines.append(f"... [limited to {cap}; {seen}+ matches]")
    return "\n".join(lines)


def _walk_files(base: Path) -> Iterable[Path]:
    if base.is_file():
        yield base
        return

    for root, dirs, files in os.walk(base, followlinks=False):
        dirs[:] = sorted(
            d for d in dirs if d not in _IGNORED_DIRS and not d.startswith(".")
        )
        for name in sorted(files):
            if name.startswith("."):
                continue
            yield Path(root) / name


def _grep_python(
    workspace: Workspace,
    pattern: str,
    base: Path,
    include: str | None,
    regex: re.Pattern[str],
    cap: int,
    deadline: float,
) -> list[str]:
    results: list[str] = []
    for file_path in _walk_files(base):
        if time.monotonic() > deadline:
            raise ToolError(
                f"grep timed out after {TIMEOUT_SECONDS:.0f}s; narrow the path or pattern"
            )
        try:
            relative = (
                file_path.relative_to(workspace.root).as_posix()
                if base.is_file()
                else file_path.relative_to(base).as_posix()
            )
        except ValueError:
            relative = str(file_path)
        if not _matches_include(relative, file_path.name, include):
            continue
        try:
            size = file_path.stat().st_size
            if size > MAX_GREP_FILE_BYTES:
                continue
            data = file_path.read_bytes()
        except OSError:
            continue
        if looks_binary(data):
            continue
        text = decode_text(data).text
        for number, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                shown = line if len(line) <= 500 else line[:500] + "…"
                results.append(f"{relative}:{number}: {shown}")
                if len(results) >= cap:
                    return results
    return results


def _grep_ripgrep(
    pattern: str,
    base: Path,
    include: str | None,
    case_sensitive: bool,
    literal: bool,
    cap: int,
) -> list[str] | None:
    rg = shutil.which("rg")
    if not rg:
        return None

    command = [
        rg,
        "--line-number",
        "--no-heading",
        "--color",
        "never",
        "--with-filename",
        "--max-filesize",
        "10M",
    ]
    if not case_sensitive:
        command.append("--ignore-case")
    if literal:
        command.append("--fixed-strings")
    if include:
        for expanded in _expand_braces(include):
            command.extend(["--glob", expanded])
    for ignored in sorted(_IGNORED_DIRS):
        command.extend(["--glob", f"!**/{ignored}/**"])
    command.append("--")
    command.append(pattern)
    command.append(str(base))

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=False,
        )
    except OSError:
        return None

    results: list[str] = []
    try:
        assert process.stdout is not None
        deadline = time.monotonic() + TIMEOUT_SECONDS
        for raw in process.stdout:
            if time.monotonic() > deadline:
                _terminate(process)
                raise ToolError(
                    f"grep timed out after {TIMEOUT_SECONDS:.0f}s; narrow the path or pattern"
                )
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line:
                results.append(line)
            if len(results) >= cap:
                _terminate(process)
                break
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _terminate(process)
    finally:
        if process.poll() is None:
            _terminate(process)
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()

    if process.returncode not in (0, 1, -15, -9, 143, 137):
        return None
    return results


def _terminate(process: subprocess.Popen[bytes]) -> None:
    try:
        process.terminate()
        process.wait(timeout=2)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def _relative_grep_line(line: str, workspace: Workspace) -> str:
    """Turn an absolute ripgrep path into a workspace-relative one."""

    match = re.match(r"^(.*?):(\d+):(.*)$", line)
    if not match:
        return line
    raw_path, number, preview = match.groups()
    try:
        path = Path(raw_path)
        if not path.is_absolute():
            path = workspace.root / path
        relative = path.resolve(strict=False).relative_to(workspace.root).as_posix()
    except (OSError, ValueError):
        relative = raw_path
    return f"{relative}:{number}:{preview}"


def grep_paths(
    workspace: Workspace,
    pattern: str,
    path: str | None = None,
    include: str | None = None,
    caseSensitive: bool = True,
    literal: bool = False,
    limit: int | float | None = None,
) -> str:
    """Search file contents with a regex (OpenCode's ``grep``).

    Uses ripgrep when it is on PATH, with a pure-Python fallback that behaves
    the same for ordinary patterns.
    """

    base = workspace.resolve(path, must_exist=True) if path else workspace.root
    cap = _clamp_limit(limit)

    if not literal:
        try:
            compiled = re.compile(pattern, 0 if caseSensitive else re.IGNORECASE)
        except re.error as exc:
            raise ToolError(f"Invalid regular expression {pattern!r}: {exc}") from exc
    else:
        compiled = re.compile(re.escape(pattern), 0 if caseSensitive else re.IGNORECASE)

    results = _grep_ripgrep(pattern, base, include, caseSensitive, literal, cap)
    if results is None:
        deadline = time.monotonic() + TIMEOUT_SECONDS
        results = _grep_python(
            workspace, pattern, base, include, compiled, cap, deadline
        )
    else:
        results = [_relative_grep_line(line, workspace) for line in results]

    if not results:
        return f"No matches found for {pattern!r} under {workspace.display(base)}"

    header = f"Found {len(results)} match(es) for {pattern!r}:"
    lines = [header, *results]
    if len(results) >= cap:
        lines.append(f"... [limited to {cap} matches]")
    return "\n".join(lines)
