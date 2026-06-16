import hashlib
import hmac
import os
import secrets
from pathlib import Path


CSRF_SECRET_BYTES = 32
CSRF_SECRET_FILENAME = "csrf-secret"


def load_or_create_csrf_secret(config_dir: Path) -> bytes:
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / CSRF_SECRET_FILENAME

    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        pass
    else:
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(secrets.token_bytes(CSRF_SECRET_BYTES))
        except Exception:
            path.unlink(missing_ok=True)
            raise

    try:
        secret = path.read_bytes()
        os.chmod(path, 0o600)
    except OSError as exc:
        raise RuntimeError(f"cannot read CSRF secret: {path}") from exc

    if len(secret) != CSRF_SECRET_BYTES:
        raise RuntimeError(
            f"CSRF secret must contain exactly {CSRF_SECRET_BYTES} bytes: {path}"
        )
    return secret


def csrf_token(secret: bytes, actor: str) -> str:
    return hmac.new(
        secret,
        actor.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_csrf_token(secret: bytes, actor: str, token: str) -> bool:
    return secrets.compare_digest(csrf_token(secret, actor), token)
