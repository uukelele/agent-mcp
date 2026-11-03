"""Apply Codex/OpenCode-style ``*** Begin Patch`` envelopes.

The format is intentionally small:

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

Paths are resolved by :class:`~agent_mcp.context.Workspace`, so they
are relative to the sandbox root unless they are absolute and outside access is
enabled.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .context import (
    ToolError,
    Workspace,
    atomic_write,
    decode_text,
    encode_text,
    normalize_newlines,
    restore_newlines,
)

_BEGIN = "*** Begin Patch"
_END = "*** End Patch"
_ADD = "*** Add File: "
_UPDATE = "*** Update File: "
_DELETE = "*** Delete File: "
_MOVE = "*** Move to: "


@dataclass
class PatchOp:
    kind: Literal["add", "update", "delete"]
    raw_path: str
    content: str | None = None
    hunks: list[list[str]] = field(default_factory=list)
    move_to: str | None = None
    path: Path | None = None
    move_path: Path | None = None


def _is_header(line: str) -> bool:
    return line.startswith((_ADD, _UPDATE, _DELETE, _MOVE)) or line in (_BEGIN, _END)


def parse_patch(patch_text: str) -> list[PatchOp]:
    text = normalize_newlines(patch_text)
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    if not lines or lines[0].strip() != _BEGIN:
        raise ToolError("patchText must start with '*** Begin Patch'")
    if lines[-1].strip() != _END:
        raise ToolError("patchText must end with '*** End Patch'")

    ops: list[PatchOp] = []
    i = 1
    while i < len(lines) - 1:
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        if line.startswith(_ADD):
            raw = line[len(_ADD) :].strip()
            i += 1
            body: list[str] = []
            while i < len(lines) - 1 and not _is_header(lines[i]):
                item = lines[i]
                if item.startswith("+"):
                    body.append(item[1:])
                elif item == "":
                    body.append("")
                else:
                    raise ToolError(
                        f"Add File body lines must start with '+': {item!r}"
                    )
                i += 1
            content = "\n".join(body)
            if content and not content.endswith("\n"):
                content += "\n"
            ops.append(PatchOp(kind="add", raw_path=raw, content=content))
            continue

        if line.startswith(_UPDATE):
            op = PatchOp(kind="update", raw_path=line[len(_UPDATE) :].strip())
            i += 1
            current: list[str] | None = None
            while i < len(lines) - 1:
                item = lines[i]
                if item.startswith(_MOVE):
                    op.move_to = item[len(_MOVE) :].strip()
                    i += 1
                    continue
                if _is_header(item):
                    break
                if item.startswith("@@"):
                    current = []
                    op.hunks.append(current)
                    i += 1
                    continue
                if current is None:
                    # Be forgiving: a single hunk may omit the leading @@.
                    current = []
                    op.hunks.append(current)
                if item.startswith("\\"):
                    # "\ No newline at end of file" from unified diffs.
                    i += 1
                    continue
                if item == "":
                    current.append(" ")
                elif item[0] in " +-":
                    current.append(item)
                else:
                    raise ToolError(f"Unexpected line in Update File hunk: {item!r}")
                i += 1
            ops.append(op)
            continue

        if line.startswith(_DELETE):
            ops.append(PatchOp(kind="delete", raw_path=line[len(_DELETE) :].strip()))
            i += 1
            continue

        raise ToolError(f"Unexpected patch line: {line!r}")

    if not ops:
        raise ToolError("patchText contains no file operations")
    return ops


def _hunk_blocks(hunk: list[str]) -> tuple[list[str], list[str]]:
    old: list[str] = []
    new: list[str] = []
    for line in hunk:
        prefix = line[:1]
        body = line[1:] if prefix in " +-" else line
        if prefix == " ":
            old.append(body)
            new.append(body)
        elif prefix == "-":
            old.append(body)
        elif prefix == "+":
            new.append(body)
        else:  # pragma: no cover - parser already rejects this
            raise ToolError(f"Invalid hunk line: {line!r}")
    return old, new


def _find_block(lines: list[str], block: list[str], start: int) -> int | None:
    if not block:
        return None
    last = len(lines) - len(block)
    for index in range(start, last + 1):
        if lines[index : index + len(block)] == block:
            return index
    return None


def _apply_hunks(path: Path, lines: list[str], hunks: list[list[str]]) -> list[str]:
    cursor = 0
    for number, hunk in enumerate(hunks, start=1):
        old_block, new_block = _hunk_blocks(hunk)
        if not old_block:
            if not new_block:
                continue
            # Pure addition with no context: append it.
            lines.extend(new_block)
            cursor = len(lines)
            continue

        index = _find_block(lines, old_block, cursor)
        if index is None:
            index = _find_block(lines, old_block, 0)
        if index is None:
            preview = "\n".join(old_block[:6])
            raise ToolError(
                f"Hunk {number} for {path} did not match the file.\n"
                f"Looking for:\n{preview}"
            )
        lines[index : index + len(old_block)] = new_block
        cursor = index + len(new_block)
    return lines


def _diff(before: str, after: str, path: str) -> str:
    diff = list(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )
    if not diff:
        return ""
    return "".join(diff[:400])


def apply_patch(patch_text: str, workspace: Workspace) -> str:
    ops = parse_patch(patch_text)

    # Resolve every path first so a typo cannot partially apply a patch.
    for op in ops:
        op.path = workspace.resolve(op.raw_path)
        if op.move_to:
            op.move_path = workspace.resolve(op.move_to)

    # Stage update/add content in memory.
    staged: dict[Path, tuple[str, str]] = {}  # path -> (new text, newline)
    originals: dict[Path, str] = {}
    deletes: list[Path] = []
    moves: list[tuple[Path, Path]] = []

    def load(path: Path) -> tuple[str, str]:
        if path in staged:
            return staged[path]
        if not path.exists():
            raise ToolError(f"Update target does not exist: {path}")
        if not path.is_file():
            raise ToolError(f"Update target is not a file: {path}")
        decoded = decode_text(path.read_bytes())
        if "\x00" in decoded.text[:8192]:
            raise ToolError(f"Refusing to patch binary file: {path}")
        normalized = normalize_newlines(decoded.text)
        normalized = normalized.removeprefix("\ufeff")
        staged[path] = (normalized, decoded.newline)
        originals[path] = normalized
        return staged[path]

    for op in ops:
        assert op.path is not None
        if op.kind == "add":
            if op.path.exists():
                raise ToolError(f"Add File target already exists: {op.path}")
            content = op.content or ""
            staged[op.path] = (content, "\n")
            originals[op.path] = ""
        elif op.kind == "delete":
            if not op.path.exists():
                raise ToolError(f"Delete target does not exist: {op.path}")
            if not op.path.is_file():
                raise ToolError(f"Delete target is not a file: {op.path}")
            deletes.append(op.path)
            staged.pop(op.path, None)
        elif op.kind == "update":
            text, newline = load(op.path)
            if not op.hunks:
                if not op.move_path:
                    raise ToolError(f"Update File has no hunks: {op.path}")
            else:
                trailing = text.endswith("\n")
                lines = text.splitlines()
                lines = _apply_hunks(op.path, lines, op.hunks)
                updated = "\n".join(lines)
                if trailing and not updated.endswith("\n"):
                    updated += "\n"
                staged[op.path] = (updated, newline)
            if op.move_path:
                if op.move_path.exists() and op.move_path != op.path:
                    raise ToolError(f"Move target already exists: {op.move_path}")
                moves.append((op.path, op.move_path))

    # Commit. Every hunk was validated above; a disk error can still leave a
    # partial result, so keep the order deterministic and report what changed.
    changed: list[str] = []
    for path, (text, newline) in staged.items():
        decoded = decode_text(path.read_bytes()) if path.exists() else None
        if decoded is not None:
            encoded = encode_text(restore_newlines(text, newline), decoded)
        else:
            encoded = restore_newlines(text, newline).encode("utf-8")
        before = originals.get(path, "")
        if path.exists() and decoded is not None:
            before_text = normalize_newlines(decoded.text)
        else:
            before_text = ""
        if before_text != text:
            try:
                atomic_write(path, encoded, mode_from=path if path.exists() else None)
            except OSError as exc:
                raise ToolError(f"Cannot write {path}: {exc}") from exc
            changed.append(workspace.display(path))

    for path in deletes:
        try:
            path.unlink()
            changed.append(f"deleted {workspace.display(path)}")
        except OSError as exc:
            raise ToolError(f"Cannot delete {path}: {exc}") from exc

    for source, target in moves:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            source.replace(target)
            changed.append(f"moved {workspace.display(source)} -> {workspace.display(target)}")
        except OSError as exc:
            raise ToolError(f"Cannot move {source} to {target}: {exc}") from exc

    if not changed:
        return "Patch applied: no changes were necessary."

    summary = [f"Applied patch to {len(changed)} path(s):"]
    summary.extend(f"  {item}" for item in changed)

    # A compact diff helps the model verify what happened.
    diffs: list[str] = []
    for path, (text, _newline) in staged.items():
        before = originals.get(path, "")
        if before == text:
            continue
        piece = _diff(before, text, workspace.display(path))
        if piece:
            diffs.append(piece)
    if diffs:
        summary.append("")
        summary.append("\n".join(diffs)[:12000])
    return "\n".join(summary)
