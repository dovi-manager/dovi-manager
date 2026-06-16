from __future__ import annotations

import os
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path

from app.config import Settings
from app.db import database_connection


REQUIRED_TOOLS = ("dovi_tool", "ffmpeg", "mkvmerge", "mkvextract", "mediainfo")


def check_readiness(
    settings: Settings,
    *,
    worker_running: bool,
    which: Callable[[str], str | None] = shutil.which,
) -> dict[str, bool]:
    checks: dict[str, bool] = {}
    try:
        with database_connection(settings.db_path) as connection:
            connection.execute("SELECT 1").fetchone()
        checks["database"] = True
    except (OSError, sqlite3.Error):
        checks["database"] = False

    checks["worker"] = worker_running
    executable = settings.dovi_convert_path
    checks["dovi_convert"] = bool(
        Path(executable).is_file()
        if Path(executable).is_absolute()
        else which(executable)
    )
    for tool in REQUIRED_TOOLS:
        checks[tool] = which(tool) is not None
    for root in settings.media_roots:
        checks[f"media_{root.id}_writable"] = (
            root.path.is_dir()
            and not root.path.is_symlink()
            and os.access(root.path, os.R_OK | os.W_OK)
        )
    for name, path in (
        ("temp_writable", settings.temp_dir),
        ("config_writable", settings.config_dir),
    ):
        checks[name] = path.is_dir() and os.access(path, os.R_OK | os.W_OK)
    return checks
