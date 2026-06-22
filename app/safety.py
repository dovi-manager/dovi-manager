import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.models import FileFingerprint


BACKUP_SUFFIX = ".bak.dovi_convert"


class PathSafetyError(ValueError):
    pass


@dataclass(frozen=True)
class StorageRequirement:
    media_required: int
    temp_required: int
    combined: bool


def conversion_storage_requirement(
    source: Path,
    temp_dir: Path,
    source_size: int,
    reserve_bytes: int,
) -> StorageRequirement:
    conversion_bytes = (source_size * 11 + 9) // 10
    combined = source.parent.stat().st_dev == temp_dir.stat().st_dev
    if combined:
        return StorageRequirement(
            media_required=(conversion_bytes * 2) + reserve_bytes,
            temp_required=0,
            combined=True,
        )
    return StorageRequirement(
        media_required=conversion_bytes + reserve_bytes,
        temp_required=conversion_bytes + reserve_bytes,
        combined=False,
    )


def conversion_with_recovery_storage_requirement(
    source: Path,
    temp_dir: Path,
    source_size: int,
    reserve_bytes: int,
) -> StorageRequirement:
    """Peak free space for conversion plus a compact recovery archive."""
    combined = source.parent.stat().st_dev == temp_dir.stat().st_dev
    if combined:
        return StorageRequirement(
            media_required=(source_size * 3) + reserve_bytes,
            temp_required=0,
            combined=True,
        )
    return StorageRequirement(
        media_required=(source_size * 2) + reserve_bytes,
        temp_required=source_size + reserve_bytes,
        combined=False,
    )


def require_conversion_storage(
    source: Path,
    temp_dir: Path,
    source_size: int,
    reserve_bytes: int,
) -> StorageRequirement:
    requirement = conversion_storage_requirement(
        source,
        temp_dir,
        source_size,
        reserve_bytes,
    )
    media_free = shutil.disk_usage(source.parent).free
    if media_free < requirement.media_required:
        raise PathSafetyError(
            "insufficient free space on the media filesystem "
            f"(need {requirement.media_required} bytes, have {media_free})"
        )
    if not requirement.combined:
        temp_free = shutil.disk_usage(temp_dir).free
        if temp_free < requirement.temp_required:
            raise PathSafetyError(
                "insufficient free space on the temporary filesystem "
                f"(need {requirement.temp_required} bytes, have {temp_free})"
            )
    return requirement


def recovery_backup_storage_requirement(
    source: Path,
    temp_dir: Path,
    source_size: int,
    reserve_bytes: int,
) -> StorageRequirement:
    combined = source.parent.stat().st_dev == temp_dir.stat().st_dev
    if combined:
        return StorageRequirement(
            media_required=(source_size * 2) + reserve_bytes,
            temp_required=0,
            combined=True,
        )
    return StorageRequirement(
        media_required=source_size + reserve_bytes,
        temp_required=source_size + reserve_bytes,
        combined=False,
    )


def recovery_restore_storage_requirement(
    source: Path,
    temp_dir: Path,
    restored_estimate: int,
    reserve_bytes: int,
) -> StorageRequirement:
    combined = source.parent.stat().st_dev == temp_dir.stat().st_dev
    if combined:
        return StorageRequirement(
            media_required=(restored_estimate * 3) + reserve_bytes,
            temp_required=0,
            combined=True,
        )
    return StorageRequirement(
        media_required=restored_estimate + reserve_bytes,
        temp_required=(restored_estimate * 2) + reserve_bytes,
        combined=False,
    )


def require_storage(
    requirement: StorageRequirement, media_dir: Path, temp_dir: Path
) -> None:
    media_free = shutil.disk_usage(media_dir).free
    if media_free < requirement.media_required:
        raise PathSafetyError(
            "insufficient free space on the media filesystem "
            f"(need {requirement.media_required} bytes, have {media_free})"
        )
    if not requirement.combined:
        temp_free = shutil.disk_usage(temp_dir).free
        if temp_free < requirement.temp_required:
            raise PathSafetyError(
                "insufficient free space on the temporary filesystem "
                f"(need {requirement.temp_required} bytes, have {temp_free})"
            )


def require_directory_writable(directory: Path) -> None:
    try:
        descriptor, probe = tempfile.mkstemp(
            prefix=".dovi-manager-write-",
            dir=directory,
        )
        os.close(descriptor)
        Path(probe).unlink()
    except OSError as exc:
        raise PathSafetyError(f"directory is not writable: {directory}") from exc


def resolve_under_root(
    root: Path,
    candidate: Path,
    *,
    require_exists: bool = True,
) -> Path:
    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve(strict=require_exists)

    if (
        resolved_candidate != resolved_root
        and resolved_root not in resolved_candidate.parents
    ):
        raise PathSafetyError(f"path is outside MEDIA_ROOT: {candidate}")
    return resolved_candidate


def validate_media_file(media_root: Path, candidate: Path) -> Path:
    if candidate.is_symlink():
        raise PathSafetyError(f"symbolic links cannot be processed: {candidate}")
    resolved = resolve_under_root(media_root, candidate)
    if not resolved.is_file():
        raise PathSafetyError(f"path is not a regular file: {candidate}")
    if resolved.suffix.lower() != ".mkv":
        raise PathSafetyError(f"path is not an MKV file: {candidate}")
    if resolved.name.endswith(BACKUP_SUFFIX):
        raise PathSafetyError(f"backup files cannot be processed: {candidate}")
    return resolved


def validate_media_directory(media_root: Path, candidate: Path) -> Path:
    if candidate.is_symlink():
        raise PathSafetyError(f"symbolic links cannot be processed: {candidate}")
    resolved = resolve_under_root(media_root, candidate)
    if not resolved.is_dir():
        raise PathSafetyError(f"path is not a directory: {candidate}")
    return resolved


def fingerprint(path: Path) -> FileFingerprint:
    stat = path.stat()
    return FileFingerprint(size=stat.st_size, mtime_ns=stat.st_mtime_ns)


def require_fingerprint(path: Path, expected: FileFingerprint) -> None:
    actual = fingerprint(path)
    if actual != expected:
        raise PathSafetyError("file changed since it was scanned")


def relative_media_path(media_root: Path, candidate: Path) -> str:
    return (
        resolve_under_root(media_root, candidate)
        .relative_to(media_root.resolve())
        .as_posix()
    )


def path_from_relative(
    media_root: Path,
    relative_path: str,
    *,
    require_exists: bool = True,
) -> Path:
    path = Path(relative_path)
    if path.is_absolute():
        raise PathSafetyError("absolute paths are not accepted")
    return resolve_under_root(
        media_root,
        media_root / path,
        require_exists=require_exists,
    )
