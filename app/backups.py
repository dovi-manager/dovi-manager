from datetime import UTC, datetime
from pathlib import Path
import tarfile

from app.config import MediaRoot
from app.models import BackupFile, BackupSet, RecoveryArchive
from app.safety import (
    BACKUP_SUFFIX,
    PathSafetyError,
    relative_media_path,
    resolve_under_root,
)


RECOVERY_ARCHIVE_SUFFIX = ".dovi"
RECOVERY_ARCHIVE_MEMBER = "el.hevc"


def validate_recovery_archive(path: Path) -> tuple[bool, str]:
    if not path.is_file() or path.is_symlink():
        return False, "Recovery archive is missing or not a regular file"
    try:
        with tarfile.open(path, "r") as archive:
            member = archive.getmember(RECOVERY_ARCHIVE_MEMBER)
            if not member.isfile() or member.size <= 0:
                return False, "Recovery archive contains an empty enhancement layer"
    except (KeyError, OSError, tarfile.TarError):
        return False, "Recovery archive is invalid"
    return True, "Ready to restore"


def discover_backups(
    media_root: Path,
    retention_days: int,
    *,
    now: datetime | None = None,
    root_id: str = "default",
    root_label: str = "Movies",
) -> list[BackupFile]:
    reference_time = now or datetime.now(UTC)
    backups: list[BackupFile] = []
    if not media_root.exists():
        return backups

    for path in sorted(media_root.rglob(f"*{BACKUP_SUFFIX}")):
        if not path.is_file() or path.is_symlink():
            continue
        stat = path.stat()
        modified = datetime.fromtimestamp(stat.st_mtime, UTC)
        age_days = max(0, (reference_time - modified).days)
        counterpart = Path(str(path)[: -len(BACKUP_SUFFIX)])
        counterpart_exists = False
        if counterpart.is_file() and not counterpart.is_symlink():
            try:
                resolve_under_root(media_root, counterpart)
                counterpart_exists = True
            except PathSafetyError:
                pass
        recovery_archive_path = counterpart.with_suffix(RECOVERY_ARCHIVE_SUFFIX)
        recovery_archive_valid, recovery_archive_reason = validate_recovery_archive(
            recovery_archive_path
        )
        eligible = counterpart_exists and age_days >= retention_days
        if not counterpart_exists:
            reason = "Protected: converted counterpart is missing"
        elif age_days < retention_days:
            reason = f"Retained for {retention_days - age_days} more day(s)"
        else:
            reason = "Eligible for confirmed deletion"

        try:
            relative_path = relative_media_path(media_root, path)
        except PathSafetyError:
            continue

        backups.append(
            BackupFile(
                relative_path=relative_path,
                path=path.resolve(),
                counterpart_path=counterpart.resolve(),
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                age_days=age_days,
                counterpart_exists=counterpart_exists,
                eligible=eligible,
                reason=reason,
                root_id=root_id,
                root_label=root_label,
                recovery_archive_path=(
                    recovery_archive_path.resolve()
                    if recovery_archive_path.exists()
                    else None
                ),
                recovery_archive_valid=recovery_archive_valid,
                recovery_archive_reason=recovery_archive_reason,
            )
        )
    return backups


def discover_all_backups(
    roots: tuple[MediaRoot, ...],
    retention_days: int,
    *,
    now: datetime | None = None,
) -> list[BackupFile]:
    backups: list[BackupFile] = []
    for root in roots:
        backups.extend(
            discover_backups(
                root.path,
                retention_days,
                now=now,
                root_id=root.id,
                root_label=root.label,
            )
        )
    return sorted(backups, key=lambda item: (item.root_label, item.relative_path))


def discover_recovery_archives(
    media_root: Path,
    retention_days: int = 30,
    *,
    now: datetime | None = None,
    root_id: str = "default",
    root_label: str = "Movies",
) -> list[RecoveryArchive]:
    archives: list[RecoveryArchive] = []
    reference_time = now or datetime.now(UTC)
    if not media_root.exists():
        return archives

    for path in sorted(media_root.rglob(f"*{RECOVERY_ARCHIVE_SUFFIX}")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            relative_path = relative_media_path(media_root, path)
        except PathSafetyError:
            continue
        counterpart = path.with_suffix(".mkv")
        restored = path.with_name(f"{path.stem}.restored.mkv")
        counterpart_exists = counterpart.is_file() and not counterpart.is_symlink()
        if counterpart_exists:
            try:
                resolve_under_root(media_root, counterpart)
            except PathSafetyError:
                counterpart_exists = False
        valid, reason = validate_recovery_archive(path)
        if valid and not counterpart_exists:
            reason = "Converted MKV is missing"
        elif valid and restored.exists():
            reason = "Restored output already exists"
        stat = path.stat()
        modified = datetime.fromtimestamp(stat.st_mtime, UTC)
        age_days = max(0, (reference_time - modified).days)
        eligible = counterpart_exists and age_days >= retention_days
        if not counterpart_exists:
            deletion_reason = "Protected: converted counterpart is missing"
        elif age_days < retention_days:
            deletion_reason = f"Retained for {retention_days - age_days} more day(s)"
        else:
            deletion_reason = "Eligible for confirmed deletion"
        archives.append(
            RecoveryArchive(
                relative_path=relative_path,
                path=path.resolve(),
                counterpart_path=counterpart.resolve(),
                restored_path=restored.resolve(),
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                counterpart_exists=counterpart_exists,
                restored_exists=restored.is_file() and not restored.is_symlink(),
                valid=valid,
                reason=reason,
                age_days=age_days,
                eligible=eligible,
                deletion_reason=deletion_reason,
                root_id=root_id,
                root_label=root_label,
            )
        )
    return archives


def discover_all_recovery_archives(
    roots: tuple[MediaRoot, ...],
    retention_days: int = 30,
    *,
    now: datetime | None = None,
) -> list[RecoveryArchive]:
    archives: list[RecoveryArchive] = []
    for root in roots:
        archives.extend(
            discover_recovery_archives(
                root.path,
                retention_days,
                now=now,
                root_id=root.id,
                root_label=root.label,
            )
        )
    return sorted(archives, key=lambda item: (item.root_label, item.relative_path))


def discover_backup_sets(
    roots: tuple[MediaRoot, ...],
    retention_days: int,
    *,
    now: datetime | None = None,
) -> list[BackupSet]:
    full_backups = discover_all_backups(roots, retention_days, now=now)
    compact_archives = discover_all_recovery_archives(
        roots, retention_days, now=now
    )
    grouped: dict[tuple[str, str], dict[str, BackupFile | RecoveryArchive]] = {}
    labels = {root.id: root.label for root in roots}

    for backup in full_backups:
        target = backup.counterpart_path.relative_to(
            next(root.path.resolve() for root in roots if root.id == backup.root_id)
        ).as_posix()
        grouped.setdefault((backup.root_id, target), {})["full"] = backup
    for archive in compact_archives:
        target = archive.counterpart_path.relative_to(
            next(root.path.resolve() for root in roots if root.id == archive.root_id)
        ).as_posix()
        grouped.setdefault((archive.root_id, target), {})["compact"] = archive

    result: list[BackupSet] = []
    for (root_id, target), artifacts in grouped.items():
        root = next(root for root in roots if root.id == root_id)
        counterpart = (root.path / target).resolve()
        result.append(
            BackupSet(
                relative_path=target,
                counterpart_path=counterpart,
                counterpart_exists=counterpart.is_file()
                and not counterpart.is_symlink(),
                root_id=root_id,
                root_label=labels[root_id],
                full=artifacts.get("full"),  # type: ignore[arg-type]
                compact=artifacts.get("compact"),  # type: ignore[arg-type]
            )
        )
    return sorted(result, key=lambda item: (item.root_label, item.relative_path))
