from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from app.config import Settings
from app.models import FileFingerprint
from app.safety import (
    path_from_relative,
    require_fingerprint,
    validate_media_file,
)


def cleanup_stale_job_dirs(temp_dir: Path) -> None:
    temp_root = temp_dir / "dovi-manager"
    if not temp_root.is_dir():
        return
    for path in temp_root.iterdir():
        if (
            path.is_dir()
            and not path.is_symlink()
            and path.name.startswith("job-")
            and path.name[4:].isdigit()
        ):
            shutil.rmtree(path)


def path_in_scan_scope(
    relative_path: str,
    target: str,
    recursive: bool,
    depth: int,
) -> bool:
    path_parts = Path(relative_path).parts
    target_parts = Path(target).parts if target else ()
    if path_parts[: len(target_parts)] != target_parts:
        return False
    child_depth = len(path_parts) - len(target_parts) - 1
    if child_depth < 0:
        return False
    if not recursive:
        return child_depth == 0
    max_depth = 0 if depth == 1 else depth
    return child_depth <= max_depth


def job_media_path(settings: Settings, job: Any) -> tuple[Path, dict[str, Any]]:
    payload = json.loads(job["payload_json"])
    root = settings.media_root_by_id(str(payload.get("root_id") or "default"))
    path = path_from_relative(
        root.path,
        payload["relative_path"],
    )
    path = validate_media_file(root.path, path)
    require_fingerprint(
        path,
        FileFingerprint(
            size=int(payload["file_size"]),
            mtime_ns=int(payload["file_mtime_ns"]),
        ),
    )
    return path, payload
