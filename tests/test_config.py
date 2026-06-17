from pathlib import Path

import pytest

from app.config import load_settings


def test_load_settings_uses_container_defaults() -> None:
    settings = load_settings({})

    assert settings.media_root == Path("/media2/movies")
    assert settings.temp_dir == Path("/cache")
    assert settings.config_dir == Path("/config")
    assert settings.db_path == Path("/config/dovi-manager.db")
    assert [(root.id, root.label, root.path) for root in settings.media_roots] == [
        ("default", "Movies", Path("/media2/movies")),
        ("shows", "Shows", Path("/media2/shows")),
    ]


def test_load_settings_uses_environment_overrides() -> None:
    settings = load_settings(
        {
            "MEDIA_ROOT": "/library",
            "SHOWS_ROOT": "/shows",
            "SHOWS_ROOT_LABEL": "Series",
            "TEMP_DIR": "/work",
            "CONFIG_DIR": "/settings",
            "DB_PATH": "/database/custom.db",
        }
    )

    assert settings.media_root == Path("/library")
    assert settings.shows_root == Path("/shows")
    assert settings.shows_root_label == "Series"
    assert settings.temp_dir == Path("/work")
    assert settings.config_dir == Path("/settings")
    assert settings.db_path == Path("/database/custom.db")


def test_db_path_defaults_to_config_dir() -> None:
    settings = load_settings({"CONFIG_DIR": "/custom-config"})

    assert settings.db_path == Path("/custom-config/dovi-manager.db")


def test_load_settings_reads_runtime_controls() -> None:
    settings = load_settings(
        {
            "SCAN_DEPTH": "8",
            "STABILITY_SECONDS": "45",
            "RETENTION_DAYS": "60",
            "DOVI_CONVERT_PATH": "/usr/local/bin/dovi_convert",
            "JOB_LOG_LIMIT": "2048",
            "SCAN_OUTPUT_LIMIT_BYTES": "12345",
            "DISK_RESERVE_GIB": "3",
            "SHUTDOWN_GRACE_SECONDS": "15",
            "AUTH_USERNAME": "admin",
            "AUTH_PASSWORD": "secret",
        }
    )

    assert settings.scan_depth == 8
    assert settings.stability_seconds == 45
    assert settings.retention_days == 60
    assert settings.dovi_convert_path == "/usr/local/bin/dovi_convert"
    assert settings.job_log_limit == 2048
    assert settings.scan_output_limit_bytes == 12345
    assert settings.disk_reserve_gib == 3
    assert settings.shutdown_grace_seconds == 15
    assert settings.auth_username == "admin"


def test_authentication_settings_must_be_paired() -> None:
    with pytest.raises(ValueError, match="both be set"):
        load_settings({"AUTH_USERNAME": "admin"})


def test_load_settings_parses_additional_media_roots() -> None:
    settings = load_settings(
        {
            "MEDIA_ROOT": "/movies",
            "MEDIA_ROOT_LABEL": "Films",
            "ADDITIONAL_MEDIA_ROOTS_JSON": (
                '[{"id":"tv","label":"TV Shows","path":"/media/tv"}]'
            ),
        }
    )

    assert [(root.id, root.label, root.path) for root in settings.media_roots] == [
        ("default", "Films", Path("/movies")),
        ("shows", "Shows", Path("/media2/shows")),
        ("tv", "TV Shows", Path("/media/tv")),
    ]


def test_load_settings_reads_extra_roots_from_config_file(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "media-roots.json").write_text(
        (
            '[{"id":"anime","label":"Anime","path":"/media2/anime"},'
            '{"id":"docs","label":"Documentaries","path":"/media2/docs"}]'
        ),
        encoding="utf-8",
    )

    settings = load_settings({"CONFIG_DIR": str(config_dir)})

    assert [(root.id, root.label, root.path) for root in settings.media_roots] == [
        ("default", "Movies", Path("/media2/movies")),
        ("shows", "Shows", Path("/media2/shows")),
        ("anime", "Anime", Path("/media2/anime")),
        ("docs", "Documentaries", Path("/media2/docs")),
    ]


def test_config_file_takes_precedence_over_legacy_env_json(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "media-roots.json").write_text(
        '[{"id":"anime","label":"Anime","path":"/media2/anime"}]',
        encoding="utf-8",
    )

    with pytest.warns(UserWarning, match="ADDITIONAL_MEDIA_ROOTS_JSON is ignored"):
        settings = load_settings(
            {
                "CONFIG_DIR": str(config_dir),
                "ADDITIONAL_MEDIA_ROOTS_JSON": (
                    '[{"id":"docs","label":"Documentaries","path":"/media2/docs"}]'
                ),
            }
        )

    assert [root.id for root in settings.media_roots] == [
        "default",
        "shows",
        "anime",
    ]


@pytest.mark.parametrize(
    "value",
    [
        "not-json",
        "{}",
        '[{"id":"default","label":"TV","path":"/tv"}]',
        '[{"id":"shows","label":"Shows","path":"/shows"}]',
        '[{"id":"tv","label":"TV","path":"relative"}]',
        '[{"id":"tv","label":"TV","path":"/movies/tv"}]',
    ],
)
def test_additional_media_roots_are_validated(value: str) -> None:
    with pytest.raises(ValueError):
        load_settings(
            {
                "MEDIA_ROOT": "/movies",
                "ADDITIONAL_MEDIA_ROOTS_JSON": value,
            }
        )


@pytest.mark.parametrize(
    "content",
    [
        "not-json",
        "{}",
        '[{"id":"default","label":"Movies","path":"/movies2"}]',
        '[{"id":"shows","label":"Shows","path":"/shows2"}]',
        '[{"id":"anime","label":"Anime","path":"relative"}]',
        (
            '[{"id":"anime","label":"Anime","path":"/media2/anime"},'
            '{"id":"anime","label":"Anime 2","path":"/media2/anime2"}]'
        ),
    ],
)
def test_media_roots_config_file_is_validated(
    tmp_path: Path,
    content: str,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "media-roots.json").write_text(content, encoding="utf-8")

    with pytest.raises(ValueError):
        load_settings({"CONFIG_DIR": str(config_dir)})


@pytest.mark.parametrize("name", ["SCAN_DEPTH", "RETENTION_DAYS", "JOB_LOG_LIMIT"])
def test_positive_integer_settings_are_validated(name: str) -> None:
    with pytest.raises(ValueError, match=name):
        load_settings({name: "0"})
