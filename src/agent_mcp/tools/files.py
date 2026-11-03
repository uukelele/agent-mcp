"""File tools with the same names and input fields as OpenCode's built-ins."""

from __future__ import annotations

import difflib
import mimetypes
from pathlib import Path

from fastmcp.utilities.types import File, Image

from ..context import (
    ToolError,
    Workspace,
    atomic_write,
    decode_text,
    encode_text,
    looks_binary,
    normalize_newlines,
    read_bytes,
    restore_newlines,
)
from ..patch import apply_patch as apply_patch_text

# OpenCode caps a read at 2,000 lines and about 50 KiB per page.
MAX_READ_LINES = 2000
MAX_READ_BYTES = 50 * 1024
MAX_LINE_CHARS = 2000

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".tif", ".tiff"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"}


def _format_size(size: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024  # type: ignore[assignment]
    return f"{size} B"  # pragma: no cover


def _directory_listing(path: Path) -> str:
    try:
        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as exc:
        raise ToolError(f"Cannot list {path}: {exc}") from exc

    lines = [f"{path} (directory)"]
    for entry in entries[:1000]:
        try:
            if entry.is_dir():
                lines.append(f"  [D] {entry.name}/")
            else:
                lines.append(f"  [F] {entry.name}  {_format_size(entry.stat().st_size)}")
        except OSError:
            lines.append(f"  [?] {entry.name}")
    if not entries:
        lines.append("  (empty directory)")
    elif len(entries) > 1000:
        lines.append(f"  ... ({len(entries) - 1000} more entries)")
    return "\n".join(lines)


def read(
    workspace: Workspace,
    path: str,
    offset: float | int | None = None,
    limit: float | int | None = None,
    *,
    max_bytes: int = 10 * 1024 * 1024,
) -> str | Image | File:
    """Read a file with OpenCode's ``path``/``offset``/``limit`` arguments.

    Directories return a non-recursive listing.  Images, audio, and PDFs are
    returned as MCP media content; other binary files are rejected.
    """

    target = workspace.resolve(path, must_exist=True)

    if target.is_dir():
        return _directory_listing(target)

    if not target.is_file():
        raise ToolError(f"Not a regular file: {target}")

    suffix = target.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return Image(path=target)
    if suffix in AUDIO_SUFFIXES:
        from fastmcp.utilities.types import Audio

        return Audio(path=target)
    if suffix == ".pdf":
        return File(path=target, name=target.name)

    data = read_bytes(target, max_bytes=max_bytes)
    if looks_binary(data):
        mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        raise ToolError(
            f"Cannot read binary file {target} ({mime}) as text. "
            "Use the shell tool or a media-aware client for this file."
        )

    decoded = decode_text(data)
    text = normalize_newlines(decoded.text).removeprefix("\ufeff")

    lines = text.splitlines()
    total = len(lines)

    start = int(offset) if offset is not None else 1
    start = max(start, 1)
    max_lines = int(limit) if limit is not None else MAX_READ_LINES
    max_lines = max(max_lines, 1)
    max_lines = min(max_lines, MAX_READ_LINES)

    if total == 0:
        return "(empty file)"
    if start > total:
        return f"(offset {start} is past the end of {workspace.display(target)}, which has {total} lines)"

    out: list[str] = []
    used = 0
    index = start - 1
    end = min(total, start - 1 + max_lines)
    while index < end:
        line = lines[index]
        shown = line
        if len(shown) > MAX_LINE_CHARS:
            shown = shown[:MAX_LINE_CHARS] + f"… [truncated {len(line) - MAX_LINE_CHARS} chars]"
        rendered = f"{index + 1}: {shown}"
        encoded_len = len(rendered.encode("utf-8")) + 1
        if out and used + encoded_len > MAX_READ_BYTES:
            out.append(
                f"... [page truncated at {MAX_READ_BYTES} bytes; continue with offset={index + 1}]"
            )
            break
        out.append(rendered)
        used += encoded_len
        index += 1

    if index >= total and len(out) < max_lines:
        return "\n".join(out)

    if limit is not None:
        # The caller asked for a specific page; pagination is their job.
        return "\n".join(out)

    if index < total and not (out and out[-1].startswith("... [page truncated")):
        out.append(f"... [{total - index} more lines; continue with offset={index + 1}]")

    return "\n".join(out)


def write(workspace: Workspace, path: str, content: str) -> str:
    """Create or completely replace a text file (OpenCode's ``write``)."""

    target = workspace.resolve(path)
    if target.exists() and target.is_dir():
        raise ToolError(f"Refusing to write to a directory: {target}")

    if target.exists() and target.is_file():
        existing = target.read_bytes()
        if looks_binary(existing):
            raise ToolError(
                f"Refusing to overwrite binary file {target}; use a binary-aware tool instead"
            )
        decoded = decode_text(existing)
        encoding = decoded.encoding
        bom = decoded.bom
    else:
        encoding = "utf-8"
        bom = b""

    try:
        data = bom + content.encode(encoding)
    except UnicodeEncodeError as exc:
        raise ToolError(
            f"Content cannot be encoded as {encoding} for existing file {target}: {exc}"
        ) from exc

    try:
        atomic_write(target, data, mode_from=target if target.exists() else None)
    except OSError as exc:
        raise ToolError(f"Cannot write {target}: {exc}") from exc
    return f"Wrote {len(data)} bytes to {workspace.display(target)}"


def _make_diff(before: str, after: str, name: str) -> str:
    diff = list(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{name}",
            tofile=f"b/{name}",
            n=3,
        )
    )
    if not diff:
        return ""
    return "".join(diff[:400])


def edit(
    workspace: Workspace,
    path: str,
    oldString: str,
    newString: str,
    replaceAll: bool = False,
) -> str:
    """Replace exact text in a file (OpenCode's ``edit``).

    ``oldString`` must occur exactly once unless ``replaceAll`` is true.  The
    file's encoding and newline style are preserved.
    """

    target = workspace.resolve(path, must_exist=True)
    if not target.is_file():
        raise ToolError(f"Not a regular file: {target}")
    if oldString == "":
        raise ToolError("oldString must not be empty")
    if oldString == newString:
        return f"No changes: oldString and newString are identical in {workspace.display(target)}"

    data = target.read_bytes()
    if looks_binary(data):
        raise ToolError(f"Refusing to edit binary file: {target}")
    decoded = decode_text(data)
    original = normalize_newlines(decoded.text)
    old = normalize_newlines(oldString)
    new = normalize_newlines(newString)

    count = original.count(old)
    if count == 0:
        raise ToolError(
            f"oldString was not found in {workspace.display(target)}. "
            "Read the file again and match the exact text, including whitespace."
        )
    if count > 1 and not replaceAll:
        raise ToolError(
            f"oldString occurs {count} times in {workspace.display(target)}. "
            "Add more surrounding context or pass replaceAll=true."
        )

    if replaceAll:
        updated = original.replace(old, new)
    else:
        updated = original.replace(old, new, 1)

    if updated == original:
        return f"No changes to {workspace.display(target)}"

    body = restore_newlines(updated, decoded.newline)
    try:
        atomic_write(target, encode_text(body, decoded), mode_from=target)
    except OSError as exc:
        raise ToolError(f"Cannot write {target}: {exc}") from exc

    diff = _make_diff(original, updated, workspace.display(target))
    replacements = count if replaceAll else 1
    header = f"Edited {workspace.display(target)} ({replacements} replacement(s))"
    return f"{header}\n\n{diff[:12000]}" if diff else header


def patch(workspace: Workspace, patchText: str) -> str:
    """Apply an OpenCode/Codex ``*** Begin Patch`` envelope."""

    if not patchText.strip():
        raise ToolError("patchText must not be empty")
    return apply_patch_text(patchText, workspace)
