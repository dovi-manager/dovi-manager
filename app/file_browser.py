import os
from dataclasses import dataclass
from pathlib import Path

from app.safety import BACKUP_SUFFIX, relative_media_path, validate_media_directory


@dataclass(frozen=True)
class BrowserEntry:
    name: str
    relative_path: str
    is_directory: bool
    size: int | None = None


def browse_media_root(
    root: Path,
    relative_directory: str = "",
    *,
    search: str = "",
    limit: int = 500,
) -> list[BrowserEntry]:
    directory = validate_media_directory(root, root / relative_directory)
    query = search.strip().casefold()
    entries: list[BrowserEntry] = []

    if query:
        for current, directories, names in os.walk(directory, followlinks=False):
            current_path = Path(current)
            directories[:] = [
                name for name in directories if not (current_path / name).is_symlink()
            ]
            for name in names:
                path = current_path / name
                if (
                    query not in name.casefold()
                    or path.is_symlink()
                    or path.suffix.lower() != ".mkv"
                    or path.name.endswith(BACKUP_SUFFIX)
                ):
                    continue
                entries.append(
                    BrowserEntry(
                        name=name,
                        relative_path=relative_media_path(root, path),
                        is_directory=False,
                        size=path.stat().st_size,
                    )
                )
                if len(entries) >= limit:
                    return entries
        return sorted(entries, key=lambda item: item.relative_path.casefold())

    for path in directory.iterdir():
        if path.is_symlink():
            continue
        if path.is_dir():
            entries.append(
                BrowserEntry(
                    name=path.name,
                    relative_path=relative_media_path(root, path),
                    is_directory=True,
                )
            )
        elif (
            path.is_file()
            and path.suffix.lower() == ".mkv"
            and not path.name.endswith(BACKUP_SUFFIX)
        ):
            entries.append(
                BrowserEntry(
                    name=path.name,
                    relative_path=relative_media_path(root, path),
                    is_directory=False,
                    size=path.stat().st_size,
                )
            )
    return sorted(
        entries,
        key=lambda item: (not item.is_directory, item.name.casefold()),
    )[:limit]
