from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class CandidateCategory(StrEnum):
    MEL = "mel"
    SIMPLE_FEL = "simple_fel"
    COMPLEX_FEL = "complex_fel"
    SCAN_ERROR = "scan_error"


class JobKind(StrEnum):
    SCAN = "scan"
    CONVERT = "convert"
    INSPECT = "inspect"
    BACKUP_DELETE = "backup_delete"
    RECOVERY_BACKUP = "recovery_backup"
    RECOVERY_RESTORE = "recovery_restore"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_JOB_STATES = (JobState.QUEUED.value, JobState.RUNNING.value)


class ScanMode(StrEnum):
    FULL = "full"
    CUSTOM = "custom"
    SMART = "smart"
    FILE = "file"


@dataclass(frozen=True)
class FileFingerprint:
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class ScanCandidate:
    relative_path: str
    category: CandidateCategory
    status_text: str
    action_text: str
    fingerprint: FileFingerprint
    root_id: str = "default"


@dataclass(frozen=True)
class ScannedFile:
    relative_path: str
    category: CandidateCategory | None
    status_text: str
    action_text: str
    fingerprint: FileFingerprint
    root_id: str = "default"


@dataclass(frozen=True)
class BackupFile:
    relative_path: str
    path: Path
    counterpart_path: Path
    size: int
    mtime_ns: int
    age_days: int
    counterpart_exists: bool
    eligible: bool
    reason: str
    root_id: str = "default"
    root_label: str = "Movies"
    recovery_archive_path: Path | None = None
    recovery_archive_valid: bool = False
    recovery_archive_reason: str = "Recovery archive is missing"

    @property
    def selection_key(self) -> str:
        return f"{self.root_id}:{self.relative_path}"


@dataclass(frozen=True)
class RecoveryArchive:
    relative_path: str
    path: Path
    counterpart_path: Path
    restored_path: Path
    size: int
    mtime_ns: int
    counterpart_exists: bool
    restored_exists: bool
    valid: bool
    reason: str
    root_id: str = "default"
    root_label: str = "Movies"

    @property
    def selection_key(self) -> str:
        return f"{self.root_id}:{self.relative_path}"
