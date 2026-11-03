from __future__ import annotations

import codecs
from pathlib import Path

import pytest

from agent_mcp.context import ToolError, Workspace
from agent_mcp.tools import files


@pytest.fixture()
def ws(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path)


def test_read_numbers_lines_and_pages(ws: Workspace, tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    assert files.read(ws, "sample.txt") == "1: alpha\n2: beta\n3: gamma"
    assert files.read(ws, "sample.txt", offset=2, limit=1) == "2: beta"
    assert "offset 99 is past the end" in files.read(ws, "sample.txt", offset=99)


def test_read_empty_file_and_directory(ws: Workspace, tmp_path: Path) -> None:
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")
    assert files.read(ws, "empty.txt") == "(empty file)"

    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "child.txt").write_text("x", encoding="utf-8")
    listing = files.read(ws, "sub")
    assert "(directory)" in listing
    assert "[F] child.txt" in listing


def test_read_rejects_binary(ws: Workspace, tmp_path: Path) -> None:
    (tmp_path / "bin.dat").write_bytes(b"hello\x00world")
    with pytest.raises(ToolError, match="binary file"):
        files.read(ws, "bin.dat")


def test_read_returns_image(ws: Workspace, tmp_path: Path) -> None:
    # Minimal PNG signature is enough for the media branch; FastMCP reads it
    # lazily when the tool result is serialized.
    png = codecs.decode(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c63600000020001e221bc330000000049454e44ae426082",
        "hex",
    )
    (tmp_path / "pixel.png").write_bytes(png)
    result = files.read(ws, "pixel.png")
    assert result.__class__.__name__ == "Image"


def test_read_truncates_long_lines(ws: Workspace, tmp_path: Path) -> None:
    (tmp_path / "long.txt").write_text("x" * 2500, encoding="utf-8")
    result = files.read(ws, "long.txt")
    assert "truncated 500 chars" in result


def test_read_respects_page_byte_limit(ws: Workspace, tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("\n".join("y" * 100 for _ in range(2000)), encoding="utf-8")
    result = files.read(ws, "big.txt")
    assert "page truncated at" in result


def test_read_outside_root_is_rejected(ws: Workspace, tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("nope", encoding="utf-8")
    with pytest.raises(ToolError, match="outside the workspace root"):
        files.read(ws, str(outside))


def test_write_creates_parents(ws: Workspace, tmp_path: Path) -> None:
    result = files.write(ws, "a/b/c.txt", "hello")
    assert "Wrote 5 bytes" in result
    assert (tmp_path / "a" / "b" / "c.txt").read_text(encoding="utf-8") == "hello"

    files.write(ws, "a/b/c.txt", "replaced")
    assert (tmp_path / "a" / "b" / "c.txt").read_text(encoding="utf-8") == "replaced"


def test_write_rejects_directory(ws: Workspace, tmp_path: Path) -> None:
    (tmp_path / "dir").mkdir()
    with pytest.raises(ToolError, match="directory"):
        files.write(ws, "dir", "nope")


def test_edit_unique_replacement(ws: Workspace, tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("one two three\n", encoding="utf-8")
    result = files.edit(ws, "f.txt", "two", "TWO")
    assert "1 replacement" in result
    assert target.read_text(encoding="utf-8") == "one TWO three\n"
    assert "--- a/f.txt" in result


def test_edit_ambiguous_requires_replace_all(ws: Workspace, tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("x x\n", encoding="utf-8")
    with pytest.raises(ToolError, match="occurs 2 times"):
        files.edit(ws, "f.txt", "x", "y")
    files.edit(ws, "f.txt", "x", "y", replaceAll=True)
    assert target.read_text(encoding="utf-8") == "y y\n"


def test_edit_missing_text(ws: Workspace, tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("hello\n", encoding="utf-8")
    with pytest.raises(ToolError, match="was not found"):
        files.edit(ws, "f.txt", "goodbye", "hi")


def test_edit_rejects_binary(ws: Workspace, tmp_path: Path) -> None:
    (tmp_path / "bin.dat").write_bytes(b"abc\x00def")
    with pytest.raises(ToolError, match="binary file"):
        files.edit(ws, "bin.dat", "abc", "xyz")


def test_write_rejects_binary(ws: Workspace, tmp_path: Path) -> None:
    (tmp_path / "bin.dat").write_bytes(b"abc\x00def")
    with pytest.raises(ToolError, match="binary file"):
        files.write(ws, "bin.dat", "text")


def test_edit_preserves_crlf(ws: Workspace, tmp_path: Path) -> None:
    target = tmp_path / "crlf.txt"
    target.write_bytes(b"one\r\ntwo\r\n")
    files.edit(ws, "crlf.txt", "two", "three")
    assert target.read_bytes() == b"one\r\nthree\r\n"


def test_edit_preserves_utf8_bom(ws: Workspace, tmp_path: Path) -> None:
    target = tmp_path / "bom.txt"
    target.write_bytes(codecs.BOM_UTF8 + "café\n".encode())
    files.edit(ws, "bom.txt", "café", "tea")
    assert target.read_bytes() == codecs.BOM_UTF8 + b"tea\n"


def test_edit_preserves_cp1252(ws: Workspace, tmp_path: Path) -> None:
    target = tmp_path / "ansi.txt"
    target.write_bytes("café\n".encode("cp1252"))
    files.edit(ws, "ansi.txt", "café", "cafe")
    assert target.read_bytes() == b"cafe\n"


def test_read_and_edit_bomless_utf16(ws: Workspace, tmp_path: Path) -> None:
    target = tmp_path / "utf16.txt"
    target.write_bytes("hello\nworld\n".encode("utf-16-le"))

    assert files.read(ws, "utf16.txt") == "1: hello\n2: world"
    files.edit(ws, "utf16.txt", "world", "there")
    assert target.read_bytes() == "hello\nthere\n".encode("utf-16-le")


def test_read_and_edit_utf16_with_bom(ws: Workspace, tmp_path: Path) -> None:
    target = tmp_path / "utf16bom.txt"
    target.write_bytes("hello\n".encode("utf-16"))

    assert files.read(ws, "utf16bom.txt") == "1: hello"
    files.edit(ws, "utf16bom.txt", "hello", "bye")
    assert target.read_bytes() == "bye\n".encode("utf-16")


def test_edit_preserves_utf16_be_bom(ws: Workspace, tmp_path: Path) -> None:
    target = tmp_path / "utf16be.txt"
    target.write_bytes(codecs.BOM_UTF16_BE + "hello\n".encode("utf-16-be"))

    files.edit(ws, "utf16be.txt", "hello", "bye")
    assert target.read_bytes() == codecs.BOM_UTF16_BE + "bye\n".encode("utf-16-be")


def test_write_preserves_existing_utf8_bom(ws: Workspace, tmp_path: Path) -> None:
    target = tmp_path / "bom.txt"
    target.write_bytes(codecs.BOM_UTF8 + b"old\n")
    files.write(ws, "bom.txt", "new\n")
    assert target.read_bytes() == codecs.BOM_UTF8 + b"new\n"


def test_patch_add_update_move_delete(ws: Workspace, tmp_path: Path) -> None:
    (tmp_path / "old.txt").write_text("a\nb\n", encoding="utf-8")
    (tmp_path / "gone.txt").write_text("bye\n", encoding="utf-8")

    patch_text = """*** Begin Patch
*** Add File: added.txt
+hello
*** Update File: old.txt
@@
 a
-b
+B
*** Update File: old.txt
*** Move to: moved.txt
@@
 a
+A
*** Delete File: gone.txt
*** End Patch
"""

    result = files.patch(ws, patch_text)
    assert "Applied patch" in result
    assert (tmp_path / "added.txt").read_text(encoding="utf-8") == "hello\n"
    assert (tmp_path / "moved.txt").read_text(encoding="utf-8") == "a\nA\nB\n"
    assert not (tmp_path / "old.txt").exists()
    assert not (tmp_path / "gone.txt").exists()


def test_patch_is_all_or_nothing(ws: Workspace, tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta\n", encoding="utf-8")

    patch_text = """*** Begin Patch
*** Update File: a.txt
@@
-alpha
+ALPHA
*** Update File: b.txt
@@
-does-not-match
+whatever
*** End Patch
"""
    with pytest.raises(ToolError, match="did not match"):
        files.patch(ws, patch_text)
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "alpha\n"
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "beta\n"


def test_patch_add_existing_file_fails(ws: Workspace, tmp_path: Path) -> None:
    (tmp_path / "exists.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(ToolError, match="already exists"):
        files.patch(ws, "*** Begin Patch\n*** Add File: exists.txt\n+y\n*** End Patch\n")


def test_patch_delete_missing_file_fails(ws: Workspace) -> None:
    with pytest.raises(ToolError, match="does not exist"):
        files.patch(ws, "*** Begin Patch\n*** Delete File: nope.txt\n*** End Patch\n")


def test_patch_move_without_content_change(ws: Workspace, tmp_path: Path) -> None:
    (tmp_path / "before.txt").write_text("same\n", encoding="utf-8")
    result = files.patch(
        ws,
        "*** Begin Patch\n*** Update File: before.txt\n*** Move to: after.txt\n*** End Patch\n",
    )
    assert "moved" in result
    assert not (tmp_path / "before.txt").exists()
    assert (tmp_path / "after.txt").read_text(encoding="utf-8") == "same\n"
