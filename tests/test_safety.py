import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models import FileFingerprint
from app.safety import (
    PathSafetyError,
    conversion_storage_requirement,
    conversion_with_recovery_storage_requirement,
    fingerprint,
    path_from_relative,
    require_directory_writable,
    require_fingerprint,
    validate_media_file,
)


def test_accepts_regular_mkv_under_media_root(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    movie = root / "Movie.mkv"
    movie.write_bytes(b"movie")

    assert validate_media_file(root, movie) == movie.resolve()
    assert path_from_relative(root, "Movie.mkv") == movie.resolve()


@pytest.mark.parametrize(
    "name",
    ["Movie.txt", "Movie.mkv.bak.dovi_convert"],
)
def test_rejects_invalid_media_files(tmp_path: Path, name: str) -> None:
    root = tmp_path / "media"
    root.mkdir()
    path = root / name
    path.write_bytes(b"x")

    with pytest.raises(PathSafetyError):
        validate_media_file(root, path)


def test_rejects_traversal_and_sibling_prefix(tmp_path: Path) -> None:
    root = tmp_path / "media"
    sibling = tmp_path / "media-other"
    root.mkdir()
    sibling.mkdir()
    outside = sibling / "Movie.mkv"
    outside.write_bytes(b"x")

    with pytest.raises(PathSafetyError):
        path_from_relative(root, "../media-other/Movie.mkv")


def test_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "media"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    target = outside / "Movie.mkv"
    target.write_bytes(b"x")
    link = root / "Movie.mkv"
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(PathSafetyError):
        validate_media_file(root, link)


def test_detects_changed_fingerprint(tmp_path: Path) -> None:
    path = tmp_path / "Movie.mkv"
    path.write_bytes(b"first")
    before = fingerprint(path)
    path.write_bytes(b"changed")

    with pytest.raises(PathSafetyError, match="changed"):
        require_fingerprint(path, FileFingerprint(before.size, before.mtime_ns))


def test_combines_storage_requirement_on_shared_filesystem(tmp_path: Path) -> None:
    source = tmp_path / "Movie.mkv"
    source.write_bytes(b"x")
    temp_dir = tmp_path / "cache"
    temp_dir.mkdir()

    requirement = conversion_storage_requirement(source, temp_dir, 1000, 200)

    assert requirement.combined
    assert requirement.media_required == 2400
    assert requirement.temp_required == 0


def test_compact_conversion_combines_peak_storage_on_shared_filesystem(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Movie.mkv"
    source.write_bytes(b"x")
    temp_dir = tmp_path / "cache"
    temp_dir.mkdir()

    requirement = conversion_with_recovery_storage_requirement(
        source, temp_dir, 1000, 200
    )

    assert requirement.combined
    assert requirement.media_required == 3200
    assert requirement.temp_required == 0


def test_compact_conversion_splits_media_and_temp_peak_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "media" / "Movie.mkv"
    source.parent.mkdir()
    source.write_bytes(b"x")
    temp_dir = tmp_path / "cache"
    temp_dir.mkdir()

    monkeypatch.setattr(
        Path,
        "stat",
        lambda path: SimpleNamespace(st_dev=1 if path == source.parent else 2),
    )

    requirement = conversion_with_recovery_storage_requirement(
        source, temp_dir, 1000, 200
    )

    assert not requirement.combined
    assert requirement.media_required == 2200
    assert requirement.temp_required == 1200


def test_write_probe_rejects_unwritable_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr("app.safety.tempfile.mkstemp", fail)
    with pytest.raises(PathSafetyError, match="not writable"):
        require_directory_writable(tmp_path)
