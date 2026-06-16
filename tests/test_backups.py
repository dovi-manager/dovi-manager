from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.backups import discover_backups


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
