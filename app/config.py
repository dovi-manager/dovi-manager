import json
import warnings
from dataclasses import dataclass
from os import environ
from pathlib import Path
from pathlib import PurePosixPath
from typing import Mapping


@dataclass(frozen=True)
class MediaRoot:
    id: str
    label: str
    path: Path


@dataclass(frozen=True)
class Settings:
    media_root: Path
    temp_dir: Path
    config_dir: Path
    db_path: Path
    scan_depth: int = 5
    stability_seconds: int = 30
    retention_days: int = 30
    dovi_convert_path: str = "dovi_convert"
    job_log_limit: int = 1_000_000
    scan_output_limit_bytes: int = 20 * 1024 * 1024
    disk_reserve_gib: int = 2
    auth_username: str | None = None
    auth_password: str | None = None
    shutdown_grace_seconds: int = 20
    media_root_label: str = "Movies"
    shows_root: Path = Path("/media2/shows")
    shows_root_label: str = "Shows"
    additional_media_roots: tuple[MediaRoot, ...] = ()

    @property
    def media_roots(self) -> tuple[MediaRoot, ...]:
        return (
            MediaRoot("default", self.media_root_label, self.media_root),
            MediaRoot("shows", self.shows_root_label, self.shows_root),
            *self.additional_media_roots,
        )

    def media_root_by_id(self, root_id: str) -> MediaRoot:
        for root in self.media_roots:
            if root.id == root_id:
                return root
        raise ValueError(f"unknown media root: {root_id}")


def _positive_int(values: Mapping[str, str], name: str, default: int) -> int:
    raw_value = values.get(name)
    if raw_value is None:
        return default

    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc

    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _parse_media_roots_json(raw_value: str, *, source: str) -> tuple[MediaRoot, ...]:
    if not raw_value:
        return ()
    try:
        items = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source} must be valid JSON") from exc
    if not isinstance(items, list):
        raise ValueError(f"{source} must be a JSON array")

    roots: list[MediaRoot] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("each additional media root must be an object")
        root_id = str(item.get("id", "")).strip()
        label = str(item.get("label", "")).strip()
        path = Path(str(item.get("path", "")).strip())
        if (
            not root_id
            or root_id in {"default", "shows"}
            or not root_id.replace("-", "").replace("_", "").isalnum()
        ):
            raise ValueError(
                "media root ids must be unique URL-safe identifiers and not reserved"
            )
        if not label:
            raise ValueError(f"media root {root_id!r} requires a label")
        if not (path.is_absolute() or path.as_posix().startswith("/")):
            raise ValueError(f"media root {root_id!r} path must be absolute")
        roots.append(MediaRoot(root_id, label, path))
    return tuple(roots)


def _additional_media_roots(
    values: Mapping[str, str],
    config_dir: Path,
) -> tuple[MediaRoot, ...]:
    config_file = config_dir / "media-roots.json"
    env_value = values.get("ADDITIONAL_MEDIA_ROOTS_JSON", "").strip()
    if config_file.is_file():
        if env_value:
            warnings.warn(
                (
                    "ADDITIONAL_MEDIA_ROOTS_JSON is ignored because "
                    f"{config_file} exists"
                ),
                stacklevel=2,
            )
        return _parse_media_roots_json(
            config_file.read_text(encoding="utf-8"),
            source=str(config_file),
        )
    return _parse_media_roots_json(env_value, source="ADDITIONAL_MEDIA_ROOTS_JSON")


def validate_media_roots(settings: Settings, *, require_available: bool = True) -> None:
    seen_ids: set[str] = set()
    resolved: list[tuple[str, Path]] = []
    for root in settings.media_roots:
        if root.id in seen_ids:
            raise ValueError(f"duplicate media root id: {root.id}")
        seen_ids.add(root.id)
        is_container_absolute = (
            root.path.is_absolute() or root.path.as_posix().startswith("/")
        )
        if not is_container_absolute:
            raise ValueError(f"media root {root.id!r} path must be absolute")
        if require_available and (not root.path.is_dir() or root.path.is_symlink()):
            raise ValueError(f"media root {root.id!r} is unavailable: {root.path}")
        root_path = (
            root.path.resolve(strict=require_available)
            if root.path.is_absolute()
            else PurePosixPath(root.path.as_posix())
        )
        for other_id, other_path in resolved:
            if (
                root_path == other_path
                or root_path in other_path.parents
                or other_path in root_path.parents
            ):
                raise ValueError(f"media roots {other_id!r} and {root.id!r} overlap")
        resolved.append((root.id, root_path))


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    values = environ if env is None else env
    config_dir = Path(values.get("CONFIG_DIR", "/config"))
    auth_username = values.get("AUTH_USERNAME") or None
    auth_password = values.get("AUTH_PASSWORD") or None
    if bool(auth_username) != bool(auth_password):
        raise ValueError(
            "AUTH_USERNAME and AUTH_PASSWORD must either both be set or both be unset"
        )

    settings = Settings(
        media_root=Path(values.get("MEDIA_ROOT", "/media2/movies")),
        temp_dir=Path(values.get("TEMP_DIR", "/cache")),
        config_dir=config_dir,
        db_path=Path(values.get("DB_PATH", str(config_dir / "dovi-manager.db"))),
        scan_depth=_positive_int(values, "SCAN_DEPTH", 5),
        stability_seconds=_positive_int(values, "STABILITY_SECONDS", 30),
        retention_days=_positive_int(values, "RETENTION_DAYS", 30),
        dovi_convert_path=values.get("DOVI_CONVERT_PATH", "dovi_convert"),
        job_log_limit=_positive_int(values, "JOB_LOG_LIMIT", 1_000_000),
        scan_output_limit_bytes=_positive_int(
            values,
            "SCAN_OUTPUT_LIMIT_BYTES",
            20 * 1024 * 1024,
        ),
        disk_reserve_gib=_positive_int(values, "DISK_RESERVE_GIB", 2),
        auth_username=auth_username,
        auth_password=auth_password,
        shutdown_grace_seconds=_positive_int(values, "SHUTDOWN_GRACE_SECONDS", 20),
        media_root_label=values.get("MEDIA_ROOT_LABEL", "Movies").strip() or "Movies",
        shows_root=Path(values.get("SHOWS_ROOT", "/media2/shows")),
        shows_root_label=values.get("SHOWS_ROOT_LABEL", "Shows").strip() or "Shows",
        additional_media_roots=_additional_media_roots(values, config_dir),
    )
    validate_media_roots(settings, require_available=False)
    return settings
