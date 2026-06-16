from pathlib import Path
import os
import stat

import pytest

from app.security import (
    CSRF_SECRET_BYTES,
    CSRF_SECRET_FILENAME,
    csrf_token,
    load_or_create_csrf_secret,
    verify_csrf_token,
)


def test_csrf_secret_is_persisted_and_reused(tmp_path: Path) -> None:
    first = load_or_create_csrf_secret(tmp_path)
    second = load_or_create_csrf_secret(tmp_path)

    assert len(first) == CSRF_SECRET_BYTES
    assert second == first
    assert (tmp_path / CSRF_SECRET_FILENAME).read_bytes() == first


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions are unavailable")
def test_csrf_secret_permissions_are_restrictive(tmp_path: Path) -> None:
    load_or_create_csrf_secret(tmp_path)
    mode = stat.S_IMODE((tmp_path / CSRF_SECRET_FILENAME).stat().st_mode)

    assert mode == 0o600


def test_csrf_token_is_bound_to_actor() -> None:
    secret = b"x" * CSRF_SECRET_BYTES
    token = csrf_token(secret, "alice")

    assert verify_csrf_token(secret, "alice", token)
    assert not verify_csrf_token(secret, "bob", token)
    assert not verify_csrf_token(secret, "alice", "invalid")


def test_malformed_csrf_secret_fails_startup(tmp_path: Path) -> None:
    (tmp_path / CSRF_SECRET_FILENAME).write_bytes(b"too-short")

    with pytest.raises(RuntimeError, match="exactly"):
        load_or_create_csrf_secret(tmp_path)


def test_unreadable_csrf_secret_fails_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / CSRF_SECRET_FILENAME
    path.write_bytes(b"x" * CSRF_SECRET_BYTES)

    def fail(_: Path) -> bytes:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_bytes", fail)
    with pytest.raises(RuntimeError, match="cannot read"):
        load_or_create_csrf_secret(tmp_path)
