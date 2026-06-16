import os
import re
from collections import defaultdict
from pathlib import Path

from app.models import CandidateCategory, ScannedFile, ScanCandidate
from app.safety import fingerprint, relative_media_path


ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
DIRECTORY_PREFIX = "DIRECTORY:"


class ScanParseError(ValueError):
    pass


def inventory_media(
    media_root: Path,
    target: Path | None = None,
    *,
    recursive: bool = True,
    depth: int | None = None,
) -> list[Path]:
    search_root = target or media_root
    if not search_root.exists() or search_root.is_symlink():
        return []
    if search_root.is_file():
        return (
            [search_root]
            if search_root.suffix.lower() == ".mkv"
            and not search_root.name.endswith(".bak.dovi_convert")
            else []
        )

    max_subdirectory_depth: int | None
    if not recursive:
        max_subdirectory_depth = 0
    elif depth is None:
        max_subdirectory_depth = None
    else:
        max_subdirectory_depth = 0 if depth == 1 else depth

    files: list[Path] = []
    for current, directories, names in os.walk(search_root, followlinks=False):
        current_path = Path(current)
        current_depth = len(current_path.relative_to(search_root).parts)
        directories[:] = [
            name for name in directories if not (current_path / name).is_symlink()
        ]
        if (
            max_subdirectory_depth is not None
            and current_depth >= max_subdirectory_depth
        ):
            directories.clear()
        for name in names:
            path = current_path / name
            if (
                not path.is_symlink()
                and path.suffix.lower() == ".mkv"
                and not path.name.endswith(".bak.dovi_convert")
            ):
                files.append(path)
    return sorted(files)


def _classify(status: str) -> CandidateCategory | None:
    if "DV Profile 7 MEL (Safe)" in status:
        return CandidateCategory.MEL
    if "DV Profile 7 FEL (Simple)" in status:
        return CandidateCategory.SIMPLE_FEL
    if "DV Profile 7 FEL (Complex)" in status:
        return CandidateCategory.COMPLEX_FEL
    if "DV Profile 7" in status and (
        "Failed" in status or "Error" in status or "Check" in status
    ):
        return CandidateCategory.SCAN_ERROR
    return None


def _resolve_row_file(
    filename: str,
    current_directory: Path | None,
    files_by_directory: dict[Path, list[Path]],
    all_files: list[Path],
) -> Path:
    pool = (
        files_by_directory.get(current_directory.resolve(), [])
        if current_directory is not None
        else all_files
    )

    if filename.endswith("..."):
        prefix = filename[:-3]
        matches = [path for path in pool if path.name.startswith(prefix)]
    else:
        matches = [path for path in pool if path.name == filename]

    if len(matches) != 1:
        location = str(current_directory) if current_directory else "media inventory"
        raise ScanParseError(
            f"could not uniquely reconcile scan row {filename!r} in {location}"
        )
    return matches[0]


def parse_scan_results(
    output: str,
    media_root: Path,
    inventory: list[Path] | None = None,
    *,
    root_id: str = "default",
) -> list[ScannedFile]:
    files = inventory if inventory is not None else inventory_media(media_root)
    files_by_directory: dict[Path, list[Path]] = defaultdict(list)
    for path in files:
        files_by_directory[path.parent.resolve()].append(path)

    current_directory: Path | None = None
    in_table = False
    results: list[ScannedFile] = []
    seen_paths: set[str] = set()
    single_file: str | None = None
    single_status: str | None = None
    single_action: str | None = None

    for raw_line in output.splitlines():
        line = ANSI_ESCAPE_RE.sub("", raw_line).rstrip()
        if line.startswith("File:"):
            single_file = line.split(":", 1)[1].strip()
            continue
        if line.startswith("Status:"):
            single_status = line.split(":", 1)[1].strip()
            continue
        if line.startswith("Action:"):
            single_action = line.split(":", 1)[1].strip()
            continue
        if line.startswith(DIRECTORY_PREFIX):
            directory_text = line[len(DIRECTORY_PREFIX) :].strip()
            directory = Path(directory_text)
            if not directory.is_absolute():
                directory = media_root / directory
            current_directory = directory
            continue
        if line.startswith("Filename") and "Format" in line and "Action" in line:
            in_table = True
            continue
        if not in_table or not line or set(line) == {"-"}:
            continue
        if line.startswith(("Showing ", "No conversion", "===")):
            break
        if len(line) < 52:
            continue

        filename = line[:50].rstrip()
        status = line[51:87].strip()
        action = line[88:].strip() if len(line) > 88 else ""
        category = _classify(status)
        try:
            path = _resolve_row_file(
                filename,
                current_directory,
                files_by_directory,
                files,
            )
        except ScanParseError:
            if category is None:
                continue
            raise
        relative_path = relative_media_path(media_root, path)
        if relative_path in seen_paths:
            raise ScanParseError(f"duplicate scan result for {relative_path}")
        seen_paths.add(relative_path)
        results.append(
            ScannedFile(
                relative_path=relative_path,
                category=category,
                status_text=status,
                action_text=action,
                fingerprint=fingerprint(path),
                root_id=root_id,
            )
        )

    if "No MKV files found." in output:
        return []
    if single_file and single_status is not None and single_action is not None:
        path = _resolve_row_file(single_file, None, files_by_directory, files)
        return [
            ScannedFile(
                relative_path=relative_media_path(media_root, path),
                category=_classify(single_status),
                status_text=single_status,
                action_text=single_action,
                fingerprint=fingerprint(path),
                root_id=root_id,
            )
        ]
    if not in_table:
        raise ScanParseError("dovi_convert scan output did not contain a results table")
    return results


def parse_scan_output(
    output: str,
    media_root: Path,
    inventory: list[Path] | None = None,
    *,
    root_id: str = "default",
) -> list[ScanCandidate]:
    return [
        ScanCandidate(
            relative_path=result.relative_path,
            category=result.category,
            status_text=result.status_text,
            action_text=result.action_text,
            fingerprint=result.fingerprint,
            root_id=result.root_id,
        )
        for result in parse_scan_results(
            output,
            media_root,
            inventory,
            root_id=root_id,
        )
        if result.category is not None
    ]
