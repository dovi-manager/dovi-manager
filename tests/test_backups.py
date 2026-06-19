from datetime import UTC, datetime, timedelta
import io
from pathlib import Path
import tarfile

from app.backups import (
    discover_backups,
    discover_recovery_archives,
    validate_recovery_archive,
)


def write_recovery_archive(path: Path, payload: bytes = b"enhancement-layer") -> None:
    with tarfile.open(path, "w") as archive:
        info = tarfile.TarInfo("el.hevc")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))


def set_age(path: Path, days: int, now: datetime) -> None:
    timestamp = (now - timedelta(days=days)).timestamp()
    path.touch()
    import os

    os.utime(path, (timestamp, timestamp))


def test_backup_detection_and_eligibility(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    now = datetime(2026, 6, 7, tzinfo=UTC)

    converted = root / "Movie.mkv"
    converted.write_bytes(b"converted")
    old_backup = root / "Movie.mkv.bak.dovi_convert"
    old_backup.write_bytes(b"original")
    set_age(old_backup, 40, now)

    recent_backup = root / "Recent.mkv.bak.dovi_convert"
    recent_backup.write_bytes(b"recent")
    (root / "Recent.mkv").write_bytes(b"converted")
    set_age(recent_backup, 5, now)

    orphan = root / "Orphan.mkv.bak.dovi_convert"
    orphan.write_bytes(b"orphan")
    set_age(orphan, 40, now)

    backups = {item.relative_path: item for item in discover_backups(root, 30, now=now)}

    assert backups[old_backup.name].eligible
    assert not backups[recent_backup.name].eligible
    assert not backups[orphan.name].eligible
    assert "missing" in backups[orphan.name].reason


def test_recovery_archive_validation_and_discovery(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    converted = root / "Movie.mkv"
    converted.write_bytes(b"converted")
    archive = root / "Movie.dovi"
    write_recovery_archive(archive)

    valid, reason = validate_recovery_archive(archive)
    discovered = discover_recovery_archives(root)

    assert valid
    assert reason == "Ready to restore"
    assert len(discovered) == 1
    assert discovered[0].relative_path == "Movie.dovi"
    assert discovered[0].counterpart_path == converted.resolve()
    assert discovered[0].restored_path == (root / "Movie.restored.mkv").resolve()
    assert discovered[0].counterpart_exists
    assert not discovered[0].restored_exists
    assert discovered[0].valid


def test_invalid_and_empty_recovery_archives_are_rejected(tmp_path: Path) -> None:
    invalid = tmp_path / "Invalid.dovi"
    invalid.write_bytes(b"not a tar archive")
    empty = tmp_path / "Empty.dovi"
    write_recovery_archive(empty, b"")

    assert validate_recovery_archive(invalid) == (
        False,
        "Recovery archive is invalid",
    )
    assert validate_recovery_archive(empty) == (
        False,
        "Recovery archive contains an empty enhancement layer",
    )
