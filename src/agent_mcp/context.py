"""Shared filesystem helpers for the OpenCode-compatible MCP server.

Everything in this module is deliberately standard-library only and works on
Linux, macOS, and Windows.  The two important jobs are:

* keep every path inside the configured workspace root, and
* read/write text without destroying the file's encoding, BOM, or newline style.
"""

from __future__ import annotations

import codecs
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ToolError(Exception):
    """A user-facing tool error.

    FastMCP turns raised exceptions into MCP tool errors.  Keeping our own type
    makes it obvious which failures are intentional and safe to show.
    """


# ---------------------------------------------------------------------------
# Path sandbox
# ---------------------------------------------------------------------------


def _normcase(value: str) -> str:
    return os.path.normcase(os.path.abspath(value))


def is_within(path: Path, root: Path) -> bool:
    """Return True when *path* is inside *root* (or is *root* itself)."""

    try:
        return os.path.commonpath([_normcase(str(path)), _normcase(str(root))]) == _normcase(
            str(root)
        )
    except ValueError:
        # Different Windows drives, for example.
        return False


class Workspace:
    """Resolve user-supplied paths against a sandbox root."""

    def __init__(self, root: str | Path, allow_outside: bool = False) -> None:
        self.root = Path(root).expanduser().resolve()
        self.allow_outside = allow_outside
        if not self.root.exists():
            raise ToolError(f"Workspace root does not exist: {self.root}")
        if not self.root.is_dir():
            raise ToolError(f"Workspace root is not a directory: {self.root}")

    def resolve(self, raw: str | Path, *, must_exist: bool = False) -> Path:
        """Turn *raw* into an absolute path and enforce the sandbox.

        Relative paths are resolved against the workspace root, which is what
        OpenCode does for its built-in tools.
        """

        candidate = Path(os.path.expanduser(str(raw)))
        if not candidate.is_absolute():
            candidate = self.root / candidate

        try:
            resolved = candidate.resolve(strict=must_exist)
        except FileNotFoundError:
            resolved = candidate.resolve(strict=False)
        except OSError as exc:  # pragma: no cover - platform specific
            raise ToolError(f"Cannot resolve path {raw!r}: {exc}") from exc

        if not self.allow_outside and not is_within(resolved, self.root):
            raise ToolError(
                f"Path {str(resolved)!r} is outside the workspace root {str(self.root)!r}. "
                "Start the server with --allow-outside-root to permit this."
            )
        return resolved

    def display(self, path: Path) -> str:
        """Return a workspace-relative path when possible, else an absolute one."""

        try:
            if is_within(path, self.root):
                return path.relative_to(self.root).as_posix()
        except ValueError:
            pass
        return str(path)


# ---------------------------------------------------------------------------
# Text decoding / encoding
# ---------------------------------------------------------------------------

_BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)


def _fallback_encodings() -> list[str]:
    """Encodings to try after UTF-8, in order.

    ``locale.getpreferredencoding`` covers Windows ANSI code pages.  cp1252
    covers the common case on Linux builds where the locale is UTF-8 but the
    file came from a Windows editor.  latin-1 always succeeds and round-trips
    every byte, so it is the final lossless fallback.
    """

    values: list[str] = []
    try:
        import locale

        locale_name = locale.getpreferredencoding(False)
        if locale_name:
            values.append(locale_name)
    except Exception:
        pass
    values.extend(["cp1252", "latin-1"])
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = value.lower().replace("_", "-")
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out



_BOM_PREFIXES: tuple[bytes, ...] = tuple(bom for bom, _encoding in _BOMS)


def _detect_bomless_utf16(data: bytes) -> str | None:
    """Detect BOM-less UTF-16 from the distribution of NUL bytes.

    ASCII text in UTF-16 has a NUL in every other byte.  This is only a
    heuristic, but it prevents UTF-16 files from being misread as binary or as
    the ANSI code page.
    """

    sample = data[:8192]
    if len(sample) < 4 or b"\x00" not in sample:
        return None
    even_nuls = sample[0::2].count(0)
    odd_nuls = sample[1::2].count(0)
    threshold = max(1, len(sample) // 8)
    if odd_nuls > threshold and even_nuls < threshold:
        return "utf-16-le"
    if even_nuls > threshold and odd_nuls < threshold:
        return "utf-16-be"
    return None


def looks_binary(data: bytes) -> bool:
    """A cheap, cross-platform binary sniff.

    BOM-marked UTF-16/32 and BOM-less UTF-16 are text, not binary.
    """

    if data.startswith(_BOM_PREFIXES):
        return False
    if _detect_bomless_utf16(data):
        return False
    return b"\x00" in data[:8192]


@dataclass
class DecodedText:
    text: str
    encoding: str
    newline: str
    had_final_newline: bool
    bom: bytes = b""


def decode_text(data: bytes) -> DecodedText:
    """Decode bytes while preserving enough information for a round trip."""

    encoding: str | None = None
    bom = b""
    payload = data
    for candidate_bom, candidate_encoding in _BOMS:
        if data.startswith(candidate_bom):
            bom = candidate_bom
            payload = data[len(candidate_bom) :]
            encoding = candidate_encoding
            break

    if encoding is None:
        bomless = _detect_bomless_utf16(data)
        if bomless:
            encoding = bomless
            try:
                text = data.decode(bomless)
            except UnicodeDecodeError:
                text = data.decode(bomless, errors="replace")
        else:
            try:
                text = data.decode("utf-8")
                encoding = "utf-8"
            except UnicodeDecodeError:
                text = ""
                encoding = "utf-8"
                for candidate in _fallback_encodings():
                    try:
                        text = data.decode(candidate)
                        encoding = candidate
                        break
                    except (UnicodeDecodeError, LookupError):
                        continue
                else:
                    # Last resort: round-trippable utf-8 with surrogate escapes.
                    text = data.decode("utf-8", errors="surrogateescape")
                    encoding = "utf-8"
    else:
        try:
            text = payload.decode(encoding)
        except UnicodeDecodeError:
            text = payload.decode(encoding, errors="replace")

    newline = "\n"
    if "\r\n" in text:
        newline = "\r\n"
    elif "\r" in text and "\n" not in text:
        newline = "\r"

    return DecodedText(
        text=text,
        encoding=encoding,
        newline=newline,
        had_final_newline=text.endswith(("\n", "\r")),
        bom=bom,
    )


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def restore_newlines(text: str, newline: str) -> str:
    if newline == "\n":
        return text
    return text.replace("\n", newline)


def encode_text(text: str, decoded: DecodedText) -> bytes:
    try:
        return decoded.bom + text.encode(decoded.encoding)
    except UnicodeEncodeError as exc:
        raise ToolError(
            f"Cannot encode the new content as {decoded.encoding}: {exc}. "
            "The file was left unchanged."
        ) from exc


def read_bytes(path: Path, max_bytes: int) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ToolError(f"Cannot stat {path}: {exc}") from exc
    if size > max_bytes:
        raise ToolError(
            f"{path} is {size} bytes, larger than the {max_bytes} byte limit. "
            "Read it in pieces with the shell tool or raise --max-read-bytes."
        )
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ToolError(f"Cannot read {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def atomic_write(path: Path, data: bytes, *, mode_from: Path | None = None) -> None:
    """Write *data* to *path* atomically on POSIX and Windows."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        source = mode_from or (path if path.exists() else None)
        if source is not None:
            try:
                shutil.copymode(source, tmp)
            except OSError:
                pass
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def ensure_no_symlink_escape(path: Path, root: Path) -> None:
    """Refuse to follow a symlink that points outside the sandbox.

    ``Workspace.resolve`` already resolves the final path, so this helper is a
    second check for callers that need to reason about the link itself.
    """

    if path.is_symlink():
        target = path.resolve(strict=False)
        if not is_within(target, root):
            raise ToolError(f"Symlink {path} points outside the workspace root")


def file_mode(path: Path) -> int | None:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None
