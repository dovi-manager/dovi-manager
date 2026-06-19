from app.runtime_settings import RuntimeSettings


def test_runtime_settings_use_environment_defaults(settings, repository) -> None:
    runtime = RuntimeSettings.load(settings, repository)

    assert runtime.scan_depth == settings.scan_depth
    assert runtime.retention_days == settings.retention_days
    assert runtime.schedule_interval_minutes == 30
    assert runtime.schedule_start_time == "03:00"
    assert not runtime.scan_debug
    assert not runtime.convert_safe_mode
    assert not runtime.allow_backup_retention_override
    assert runtime.create_recovery_archive_on_convert


def test_runtime_settings_use_valid_database_overrides(settings, repository) -> None:
    repository.set_settings(
        {
            "scan_depth": "12",
            "scan_debug": "true",
            "convert_safe_mode": "true",
            "convert_verbose": "true",
            "schedule_enabled": "true",
            "schedule_interval_minutes": "60",
            "schedule_start_time": "22:45",
            "webhooks_enabled": "true",
            "radarr_root_prefix": "/movies",
            "allow_backup_retention_override": "true",
            "create_recovery_archive_on_convert": "false",
        }
    )

    runtime = RuntimeSettings.load(settings, repository)

    assert runtime.scan_depth == 12
    assert runtime.scan_debug
    assert runtime.convert_safe_mode
    assert runtime.convert_verbose
    assert runtime.schedule_enabled
    assert runtime.schedule_interval_minutes == 60
    assert runtime.schedule_start_time == "22:45"
    assert runtime.webhooks_enabled
    assert runtime.radarr_root_prefix == "/movies"
    assert runtime.allow_backup_retention_override
    assert not runtime.create_recovery_archive_on_convert


def test_invalid_stored_numbers_fall_back_to_environment(settings, repository) -> None:
    repository.set_settings(
        {
            "scan_depth": "99",
            "schedule_interval_minutes": "invalid",
            "schedule_start_time": "99:99",
        }
    )

    runtime = RuntimeSettings.load(settings, repository)

    assert runtime.scan_depth == settings.scan_depth
    assert runtime.schedule_interval_minutes == 30
    assert runtime.schedule_start_time == "03:00"


def test_schedule_value_and_unit_are_normalized(settings, repository) -> None:
    repository.set_settings(
        {
            "schedule_interval_value": "2",
            "schedule_interval_unit": "weeks",
        }
    )

    runtime = RuntimeSettings.load(settings, repository)

    assert runtime.schedule_interval_value == 2
    assert runtime.schedule_interval_unit == "weeks"
    assert runtime.schedule_interval_minutes == 20160
    assert runtime.schedule_interval_label == "2 weeks"
