from dataclasses import dataclass
import re

from app.config import Settings
from app.repository import Repository

SCHEDULE_UNIT_MINUTES = {
    "minutes": 1,
    "hours": 60,
    "days": 1440,
    "weeks": 10080,
}
MAX_SCHEDULE_MINUTES = 52 * 7 * 24 * 60
SCHEDULE_START_RE = re.compile(r"^\d{2}:\d{2}$")


def _stored_bool(repository: Repository, key: str, default: bool) -> bool:
    raw_value = repository.get_setting(key, "true" if default else "false")
    return raw_value == "true"


def _stored_int(
    repository: Repository,
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw_value = repository.get_setting(key, str(default))
    try:
        value = int(raw_value)
    except ValueError:
        return default
    return value if minimum <= value <= maximum else default


@dataclass(frozen=True)
class RuntimeSettings:
    scan_depth: int
    scan_debug: bool
    convert_safe_mode: bool
    convert_verbose: bool
    create_recovery_archive_on_convert: bool
    schedule_enabled: bool
    schedule_interval_value: int
    schedule_interval_unit: str
    schedule_interval_minutes: int
    schedule_start_time: str
    webhooks_enabled: bool
    radarr_root_prefix: str
    retention_days: int
    allow_backup_retention_override: bool
    auto_queue_mel: bool
    auto_convert_mel_after_inspect: bool
    auto_inspect_mel: bool
    auto_inspect_simple_fel: bool
    auto_inspect_complex_fel: bool

    @classmethod
    def load(
        cls,
        settings: Settings,
        repository: Repository,
    ) -> "RuntimeSettings":
        legacy_minutes = _stored_int(
            repository,
            "schedule_interval_minutes",
            30,
            minimum=5,
            maximum=MAX_SCHEDULE_MINUTES,
        )
        schedule_unit = repository.get_setting(
            "schedule_interval_unit",
            "minutes",
        )
        if schedule_unit not in SCHEDULE_UNIT_MINUTES:
            schedule_unit = "minutes"
        schedule_value = _stored_int(
            repository,
            "schedule_interval_value",
            legacy_minutes,
            minimum=1,
            maximum=MAX_SCHEDULE_MINUTES,
        )
        schedule_minutes = schedule_value * SCHEDULE_UNIT_MINUTES[schedule_unit]
        if not 5 <= schedule_minutes <= MAX_SCHEDULE_MINUTES:
            schedule_unit = "minutes"
            schedule_value = legacy_minutes
            schedule_minutes = legacy_minutes
        schedule_start_time = repository.get_setting("schedule_start_time", "03:00")
        if not SCHEDULE_START_RE.match(schedule_start_time):
            schedule_start_time = "03:00"
        else:
            hour, minute = (int(part) for part in schedule_start_time.split(":"))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                schedule_start_time = "03:00"
        return cls(
            scan_depth=_stored_int(
                repository,
                "scan_depth",
                settings.scan_depth,
                minimum=1,
                maximum=20,
            ),
            scan_debug=_stored_bool(repository, "scan_debug", False),
            convert_safe_mode=_stored_bool(
                repository,
                "convert_safe_mode",
                False,
            ),
            convert_verbose=_stored_bool(
                repository,
                "convert_verbose",
                False,
            ),
            create_recovery_archive_on_convert=_stored_bool(
                repository,
                "create_recovery_archive_on_convert",
                True,
            ),
            schedule_enabled=_stored_bool(
                repository,
                "schedule_enabled",
                False,
            ),
            schedule_interval_value=schedule_value,
            schedule_interval_unit=schedule_unit,
            schedule_interval_minutes=schedule_minutes,
            schedule_start_time=schedule_start_time,
            webhooks_enabled=_stored_bool(
                repository,
                "webhooks_enabled",
                False,
            ),
            radarr_root_prefix=repository.get_setting(
                "radarr_root_prefix",
                "",
            ).strip(),
            retention_days=_stored_int(
                repository,
                "retention_days",
                settings.retention_days,
                minimum=1,
                maximum=3650,
            ),
            allow_backup_retention_override=_stored_bool(
                repository,
                "allow_backup_retention_override",
                False,
            ),
            auto_queue_mel=_stored_bool(
                repository,
                "auto_queue_mel",
                False,
            ),
            auto_convert_mel_after_inspect=_stored_bool(
                repository,
                "auto_convert_mel_after_inspect",
                False,
            ),
            auto_inspect_mel=_stored_bool(
                repository,
                "auto_inspect_mel",
                False,
            ),
            auto_inspect_simple_fel=_stored_bool(
                repository,
                "auto_inspect_simple_fel",
                False,
            ),
            auto_inspect_complex_fel=_stored_bool(
                repository,
                "auto_inspect_complex_fel",
                False,
            ),
        )

    @property
    def schedule_interval_label(self) -> str:
        return f"{self.schedule_interval_value} {self.schedule_interval_unit}"

    def auto_inspect_enabled(self, category: str) -> bool:
        if (
            category == "mel"
            and self.auto_queue_mel
            and self.auto_convert_mel_after_inspect
        ):
            return True
        return {
            "mel": self.auto_inspect_mel,
            "simple_fel": self.auto_inspect_simple_fel,
            "complex_fel": self.auto_inspect_complex_fel,
        }.get(category, False)

    def scan_payload(
        self,
        *,
        mode: str,
        trigger: str,
        target: str = "",
        recursive: bool = True,
        depth: int | None = None,
        debug: bool | None = None,
    ) -> dict[str, object]:
        return {
            "scan_mode": mode,
            "trigger": trigger,
            "target": target,
            "recursive": recursive,
            "depth": self.scan_depth if depth is None else depth,
            "debug": self.scan_debug if debug is None else debug,
        }

    def conversion_options(self) -> dict[str, bool]:
        return {
            "safe_mode": self.convert_safe_mode,
            "verbose": self.convert_verbose,
            "create_recovery_archive": self.create_recovery_archive_on_convert,
        }
