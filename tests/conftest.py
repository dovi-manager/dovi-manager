from pathlib import Path

import pytest

from app.config import Settings
from app.db import initialize_database
from app.repository import Repository


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    media_root = tmp_path / "media"
    temp_dir = tmp_path / "cache"
    config_dir = tmp_path / "config"
    media_root.mkdir()
    temp_dir.mkdir()
    config_dir.mkdir()
    return Settings(
        media_root=media_root,
        temp_dir=temp_dir,
        config_dir=config_dir,
        db_path=config_dir / "dovi-manager.db",
        stability_seconds=1,
    )


@pytest.fixture
def repository(settings: Settings) -> Repository:
    initialize_database(settings.db_path)
    return Repository(settings.db_path)
