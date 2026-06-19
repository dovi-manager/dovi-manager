import asyncio
from pathlib import Path

import pytest

from app.dovi import (
    SubprocessRunner,
    convert_command,
    inspect_command,
    normalize_terminal_output,
    recovery_backup_command,
    recovery_restore_command,
    scan_command,
)


def test_command_builders_use_supported_non_destructive_flags() -> None:
    scan = scan_command(
        "dovi_convert",
        Path("/media"),
        recursive=True,
        depth=5,
        debug=False,
    )
    mel = convert_command(
        "dovi_convert",
        Path("/media/movie.mkv"),
        Path("/cache"),
        include_simple=False,
    )
    simple = convert_command(
        "dovi_convert",
        Path("/media/simple.mkv"),
        Path("/cache"),
        include_simple=True,
        create_recovery_archive=True,
    )

    assert scan == [
        "dovi_convert",
        "scan",
        str(Path("/media")),
        "--recursive",
        "5",
    ]
    assert mel == [
        "dovi_convert",
        "convert",
        str(Path("/media/movie.mkv")),
        "--temp",
        str(Path("/cache")),
        "--yes",
    ]
    assert simple[-2:] == ["--include-simple", "--backup"]
    assert inspect_command("dovi_convert", Path("/media/movie.mkv")) == [
        "dovi_convert",
        "inspect",
        str(Path("/media/movie.mkv")),
    ]
    for command in (scan, mel, simple):
        assert "--force" not in command
        assert "--delete" not in command
    assert recovery_backup_command(
        "dovi_convert", Path("/media/movie.mkv"), Path("/cache")
    ) == [
        "dovi_convert",
        "backup",
        str(Path("/media/movie.mkv")),
        "--temp",
        str(Path("/cache")),
    ]
    assert recovery_restore_command(
        "dovi_convert", Path("/media/movie.mkv"), Path("/cache")
    ) == [
        "dovi_convert",
        "restore",
        str(Path("/media/movie.mkv")),
        "--temp",
        str(Path("/cache")),
    ]


def test_command_builders_snapshot_safe_runtime_options() -> None:
    scan = scan_command(
        "dovi_convert",
        [Path("/media/a.mkv"), Path("/media/b.mkv")],
        recursive=False,
        depth=1,
        debug=True,
    )
    convert = convert_command(
        "dovi_convert",
        Path("/media/movie.mkv"),
        Path("/cache"),
        include_simple=False,
        safe_mode=True,
        verbose=True,
    )

    assert scan == [
        "dovi_convert",
        "scan",
        str(Path("/media/a.mkv")),
        str(Path("/media/b.mkv")),
        "--debug",
    ]
    assert convert[-2:] == ["--safe", "--verbose"]


@pytest.mark.parametrize(
    "flag",
    ["--force", "--delete", "--output", "--hdr10", "--candidates", "--source"],
)
def test_runner_refuses_forbidden_flags(flag: str) -> None:
    runner = SubprocessRunner()

    async def discard(_: str) -> None:
        pass

    with pytest.raises(ValueError, match="forbidden"):
        asyncio.run(runner.run(["dovi_convert", "convert", flag], discard))


def test_terminal_output_is_normalized() -> None:
    output = "\x1b[31mfirst\x1b[0m\rprogress\rcomplete\nnext"
    assert normalize_terminal_output(output) == "complete\nnext"
