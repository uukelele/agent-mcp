"""Cross-platform shell execution for the ``shell`` tool."""

from __future__ import annotations

import codecs
import locale
import os
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path

from ..context import ToolError, Workspace

DEFAULT_TIMEOUT_MS = 120_000
MAX_OUTPUT_BYTES = 1_000_000


def _default_shell() -> list[str]:
    if os.name == "nt":
        for candidate in ("pwsh", "powershell"):
            found = shutil.which(candidate)
            if found:
                return [found, "-NoProfile", "-NonInteractive", "-Command"]
        comspec = os.environ.get("COMSPEC") or shutil.which("cmd") or "cmd.exe"
        return [comspec, "/d", "/s", "/c"]

    shell = os.environ.get("SHELL")
    if shell and Path(shell).exists():
        base = Path(shell).name.lower()
        if base in {"bash", "zsh", "ksh"}:
            return [shell, "-lc"]
        return [shell, "-c"]
    bash = shutil.which("bash")
    if bash:
        return [bash, "-lc"]
    return ["/bin/sh", "-c"]


def _shell_command(shell: str | None) -> list[str]:
    if not shell:
        return _default_shell()

    parts = shlex.split(shell, posix=(os.name != "nt"))
    if os.name == "nt":
        # shlex with posix=False keeps surrounding quotes; Windows paths with
        # spaces are commonly supplied as a single quoted executable.
        parts = [
            part[1:-1]
            if len(part) >= 2 and part[0] == part[-1] == '"'
            else part
            for part in parts
        ]
    if not parts:
        return _default_shell()
    executable = parts[0]
    base = Path(executable).name.lower()
    if base in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        return [*parts, "-NoProfile", "-NonInteractive", "-Command"]
    if base in {"cmd", "cmd.exe"}:
        return [*parts, "/d", "/s", "/c"]
    if base in {"bash", "zsh", "ksh", "fish"}:
        return [*parts, "-lc"]
    return [*parts, "-c"]


def _kill_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
        return

    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except Exception:
            pass


def _decode_output(data: bytes) -> str:
    # Windows PowerShell 5.1 emits UTF-16LE when stdout is redirected.
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    if data.startswith(codecs.BOM_UTF8):
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError:
            pass
    if b"\x00" in data[:200]:
        for encoding in ("utf-16-le", "utf-16-be"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue

    for encoding in ("utf-8", locale.getpreferredencoding(False) or "utf-8"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _truncate(data: bytes, limit: int = MAX_OUTPUT_BYTES) -> tuple[bytes, bool]:
    if len(data) <= limit:
        return data, False
    half = limit // 2
    return (
        data[:half]
        + f"\n... [output truncated, {len(data) - limit} bytes omitted] ...\n".encode()
        + data[-half:],
        True,
    )


class ShellRunner:
    def __init__(
        self,
        workspace: Workspace,
        shell: str | None = None,
        max_output_bytes: int = MAX_OUTPUT_BYTES,
    ) -> None:
        self.workspace = workspace
        self.argv_prefix = _shell_command(shell)
        self.max_output_bytes = max(4096, max_output_bytes)

    def _cwd(self, workdir: str | None) -> Path:
        if not workdir:
            return self.workspace.root
        path = self.workspace.resolve(workdir, must_exist=True)
        if not path.is_dir():
            raise ToolError(f"workdir is not a directory: {path}")
        return path

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PAGER", "cat")
        env.setdefault("GIT_PAGER", "cat")
        return env

    def run(
        self,
        command: str,
        workdir: str | None = None,
        timeout: int | float | None = DEFAULT_TIMEOUT_MS,
        background: bool = False,
    ) -> str:
        if not command.strip():
            raise ToolError("command must not be empty")

        cwd = self._cwd(workdir)
        timeout_ms = int(timeout) if timeout is not None else DEFAULT_TIMEOUT_MS
        timeout_ms = max(timeout_ms, 0)

        argv = [*self.argv_prefix, command]
        popen_kwargs: dict[str, object] = {
            "cwd": str(cwd),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "env": self._env(),
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        if background:
            return self._run_background(argv, cwd, popen_kwargs)

        try:
            process = subprocess.Popen(argv, **popen_kwargs)  # type: ignore[arg-type]
        except FileNotFoundError as exc:
            raise ToolError(f"Shell not found: {argv[0]}") from exc
        except OSError as exc:
            raise ToolError(f"Cannot start shell: {exc}") from exc

        timed_out = False
        try:
            stdout, _ = process.communicate(timeout=timeout_ms / 1000 if timeout_ms else None)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(process)
            try:
                stdout, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stdout = b""

        output = _decode_output(stdout or b"")
        output = output.replace("\r\n", "\n")
        if len(output.encode("utf-8", errors="replace")) > self.max_output_bytes:
            raw, _ = _truncate(
                output.encode("utf-8", errors="replace"), self.max_output_bytes
            )
            output = raw.decode("utf-8", errors="replace")

        notes: list[str] = []
        if timed_out:
            notes.append(f"[timed out after {timeout_ms / 1000:g}s]")
        if process.returncode not in (0, None):
            notes.append(f"[exit code: {process.returncode}]")

        if not output.strip():
            output = "(no output)"
        if notes:
            output = f"{output.rstrip()}\n" + "\n".join(notes)
        return output

    def _run_background(
        self,
        argv: list[str],
        cwd: Path,
        popen_kwargs: dict[str, object],
    ) -> str:
        log_dir = self.workspace.root / ".agent-mcp" / "shell"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ToolError(
                f"Cannot create background log directory {log_dir}: {exc}"
            ) from exc
        stamp = time.strftime("%Y%m%d-%H%M%S")
        suffix = time.time_ns() % 1_000_000
        log_path = log_dir / f"shell-{stamp}-{os.getpid()}-{suffix:06d}.log"

        try:
            log = log_path.open("wb")
        except OSError as exc:
            raise ToolError(f"Cannot create background log {log_path}: {exc}") from exc

        kwargs = dict(popen_kwargs)
        kwargs["stdout"] = log
        kwargs["stderr"] = subprocess.STDOUT
        try:
            process = subprocess.Popen(argv, **kwargs)  # type: ignore[arg-type]
        except OSError as exc:
            log.close()
            raise ToolError(f"Cannot start background shell: {exc}") from exc
        finally:
            log.close()

        header = (
            f"Started background command (PID {process.pid}).\n"
            f"Log: {log_path}\n"
            f"Workdir: {cwd}\n"
            f"Command: {argv[-1]}\n"
            "Use the read tool on the log path to check output."
        )
        return header
