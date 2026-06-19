import asyncio
import os
import re
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


OutputCallback = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    output: str
    output_limit_exceeded: bool = False


ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def normalize_terminal_output(text: str) -> str:
    text = ANSI_ESCAPE_RE.sub("", text)
    lines: list[str] = []
    for line in text.split("\n"):
        if "\r" in line:
            line = line.rsplit("\r", 1)[-1]
        lines.append(line)
    return "\n".join(lines)


def scan_command(
    executable: str,
    targets: Path | Sequence[Path],
    *,
    recursive: bool,
    depth: int,
    debug: bool,
) -> list[str]:
    target_list = [targets] if isinstance(targets, Path) else list(targets)
    command = [executable, "scan", *(str(path) for path in target_list)]
    if recursive:
        command.extend(("--recursive", str(depth)))
    if debug:
        command.append("--debug")
    return command


def convert_command(
    executable: str,
    file_path: Path,
    temp_dir: Path,
    *,
    include_simple: bool,
    safe_mode: bool = False,
    verbose: bool = False,
    create_recovery_archive: bool = False,
) -> list[str]:
    command = [
        executable,
        "convert",
        str(file_path),
        "--temp",
        str(temp_dir),
        "--yes",
    ]
    if include_simple:
        command.append("--include-simple")
    if safe_mode:
        command.append("--safe")
    if verbose:
        command.append("--verbose")
    if create_recovery_archive:
        command.append("--backup")
    return command


def inspect_command(executable: str, file_path: Path) -> list[str]:
    return [executable, "inspect", str(file_path)]


def recovery_backup_command(
    executable: str, file_path: Path, temp_dir: Path
) -> list[str]:
    return [executable, "backup", str(file_path), "--temp", str(temp_dir)]


def recovery_restore_command(
    executable: str, file_path: Path, temp_dir: Path
) -> list[str]:
    return [executable, "restore", str(file_path), "--temp", str(temp_dir)]


class SubprocessRunner:
    def __init__(self) -> None:
        self.active_process: asyncio.subprocess.Process | None = None

    async def run(
        self,
        command: list[str],
        on_output: OutputCallback,
        *,
        capture_limit_bytes: int | None = None,
    ) -> CommandResult:
        if self.active_process is not None:
            raise RuntimeError("a dovi_convert process is already running")
        forbidden_flags = {
            "--force",
            "--delete",
            "--output",
            "--hdr10",
            "--candidates",
            "--source",
        }
        if forbidden_flags.intersection(command):
            raise ValueError("unsafe dovi_convert flags are forbidden")

        environment = os.environ.copy()
        environment["NO_COLOR"] = "1"
        environment["PYTHONUNBUFFERED"] = "1"
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=environment,
        )
        self.active_process = process
        output_parts: list[bytes] = []
        captured_bytes = 0
        output_limit_exceeded = False
        try:
            assert process.stdout is not None
            while chunk := await process.stdout.read(8192):
                text = chunk.decode("utf-8", errors="replace")
                await on_output(text)
                if capture_limit_bytes is None:
                    output_parts.append(chunk)
                    continue
                remaining = capture_limit_bytes - captured_bytes
                if remaining > 0:
                    output_parts.append(chunk[:remaining])
                    captured_bytes += min(len(chunk), remaining)
                if len(chunk) > remaining:
                    output_limit_exceeded = True
            exit_code = await process.wait()
            output = b"".join(output_parts).decode("utf-8", errors="replace")
            return CommandResult(
                exit_code=exit_code,
                output=normalize_terminal_output(output),
                output_limit_exceeded=output_limit_exceeded,
            )
        finally:
            self.active_process = None

    async def stop(self, grace_seconds: int) -> None:
        process = self.active_process
        if process is None or process.returncode is not None:
            return

        try:
            process.send_signal(signal.SIGINT)
            await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        except (ProcessLookupError, asyncio.TimeoutError):
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
