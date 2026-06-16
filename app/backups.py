from datetime import UTC, datetime
from pathlib import Path

from app.config import MediaRoot
from app.models import BackupFile
from app.safety import (
    BACKUP_SUFFIX,
    PathSafetyError,
    relative_media_path,
    resolve_under_root,
)


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
