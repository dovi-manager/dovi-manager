from pathlib import Path

import pytest

from app.models import CandidateCategory
from app.scanner import ScanParseError, parse_scan_output, parse_scan_results


def row(name: str, status: str, action: str) -> str:
    return f"{name:<50} {status:<36} {action}"


def output_for(directory: Path, rows: list[str]) -> str:
    return "\n".join(
        [
            "Running Scanning recursively (5 levels deep)...",
            "Filename                                           Format                               Action",
            "-" * 96,
            f"\x1b[1mDIRECTORY: {directory}\x1b[0m",
            *rows,
        ]
    )


def test_parses_all_candidate_categories(tmp_path: Path) -> None:
    root = tmp_path / "media"
    folder = root / "Movies"
    folder.mkdir(parents=True)
    files = []
    for name in ("MEL.mkv", "Simple.mkv", "Complex.mkv", "Broken.mkv"):
        path = folder / name
        path.write_bytes(name.encode())
        files.append(path)

    output = output_for(
        folder,
        [
            row("MEL.mkv", "DV Profile 7 MEL (Safe)", "CONVERT"),
            row("Simple.mkv", "DV Profile 7 FEL (Simple)", "CONVERT*"),
            row("Complex.mkv", "DV Profile 7 FEL (Complex)", "SKIP"),
            row("Broken.mkv", "DV Profile 7 (Scan Failed)", "SKIP (error)"),
            row("Ignored.mkv", "HDR10", "IGNORE"),
        ],
    )

    candidates = parse_scan_output(output, root, files)

    assert [item.category for item in candidates] == [
        CandidateCategory.MEL,
        CandidateCategory.SIMPLE_FEL,
        CandidateCategory.COMPLEX_FEL,
        CandidateCategory.SCAN_ERROR,
    ]


def test_resolves_unique_truncated_filename(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    name = "A" * 60 + ".mkv"
    movie = root / name
    movie.write_bytes(b"x")
    shown = name[:47] + "..."

    candidates = parse_scan_output(
        output_for(root, [row(shown, "DV Profile 7 MEL (Safe)", "CONVERT")]),
        root,
        [movie],
    )

    assert candidates[0].relative_path == name


def test_rejects_ambiguous_truncated_filename(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    prefix = "A" * 47
    files = [root / f"{prefix}{suffix}.mkv" for suffix in ("one", "two")]
    for path in files:
        path.write_bytes(b"x")

    with pytest.raises(ScanParseError, match="uniquely"):
        parse_scan_output(
            output_for(
                root,
                [row(prefix + "...", "DV Profile 7 MEL (Safe)", "CONVERT")],
            ),
            root,
            files,
        )


def test_empty_media_scan_is_valid(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    assert parse_scan_output("No MKV files found.", root, []) == []


def test_missing_table_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ScanParseError, match="results table"):
        parse_scan_output("unexpected output", tmp_path, [])


def test_single_directory_output_without_directory_header(tmp_path: Path) -> None:
    root = tmp_path / "media"
    folder = root / "Only"
    folder.mkdir(parents=True)
    movie = folder / "Movie.mkv"
    movie.write_bytes(b"x")
    output = "\n".join(
        [
            "Filename                                           Format                               Action",
            "-" * 96,
            row("Movie.mkv", "DV Profile 7 MEL (Safe)", "CONVERT"),
        ]
    )

    candidates = parse_scan_output(output, root, [movie])

    assert candidates[0].relative_path == "Only/Movie.mkv"


def test_parses_upstream_single_file_scan_output(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    movie = root / "Movie.mkv"
    movie.write_bytes(b"x")

    results = parse_scan_results(
        "\n".join(
            [
                "---------------------------------------------------",
                "File:   Movie.mkv",
                "Status: DV Profile 7 FEL (Simple)",
                "Action: CONVERT*",
                "---------------------------------------------------",
            ]
        ),
        root,
        [movie],
    )

    assert len(results) == 1
    assert results[0].relative_path == "Movie.mkv"
    assert results[0].category is CandidateCategory.SIMPLE_FEL


def test_scan_results_include_non_candidates_for_inventory(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    movie = root / "Movie.mkv"
    movie.write_bytes(b"x")
    output = "\n".join(
        [
            "Filename                                           Format                               Action",
            "-" * 96,
            row("Movie.mkv", "HDR10", "IGNORE"),
        ]
    )

    results = parse_scan_results(output, root, [movie])

    assert len(results) == 1
    assert results[0].category is None
    assert parse_scan_output(output, root, [movie]) == []
