from pathlib import Path

from app.config import Settings
from app.models import CandidateCategory, FileFingerprint, JobKind, ScanMode
from app.repository import Repository
from app.runtime_settings import RuntimeSettings
from app.safety import (
    PathSafetyError,
    path_from_relative,
    require_fingerprint,
    relative_media_path,
    validate_media_directory,
    validate_media_file,
)


class JobService:
    def __init__(self, settings: Settings, repository: Repository):
        self.settings = settings
        self.repository = repository

    def queue_scan(
        self,
        *,
        mode: ScanMode = ScanMode.FULL,
        trigger: str = "manual",
        target: str = "",
        root_id: str | None = None,
        recursive: bool = True,
        depth: int | None = None,
        debug: bool | None = None,
    ) -> int:
        runtime = RuntimeSettings.load(self.settings, self.repository)
        effective_depth = runtime.scan_depth if depth is None else depth
        if not 1 <= effective_depth <= 20:
            raise ValueError("scan depth must be between 1 and 20")

        normalized_target = ""
        selected_root = (
            self.settings.media_root_by_id(root_id) if root_id is not None else None
        )
        if mode is ScanMode.CUSTOM:
            selected_root = selected_root or self.settings.media_root_by_id("default")
            path = validate_media_directory(
                selected_root.path,
                selected_root.path / target,
            )
            normalized_target = (
                ""
                if path == selected_root.path.resolve()
                else relative_media_path(selected_root.path, path)
            )
        elif mode is ScanMode.FILE:
            selected_root = selected_root or self.settings.media_root_by_id("default")
            path = validate_media_file(
                selected_root.path,
                selected_root.path / target,
            )
            normalized_target = relative_media_path(selected_root.path, path)
            recursive = False
            effective_depth = 1
        elif target:
            raise ValueError("only custom and file scans accept a target")

        payload = runtime.scan_payload(
            mode=mode.value,
            trigger=trigger,
            target=normalized_target,
            recursive=recursive,
            depth=effective_depth,
            debug=debug,
        )
        payload["root_id"] = selected_root.id if selected_root else ""
        payload["root_ids"] = (
            [selected_root.id]
            if selected_root
            else [root.id for root in self.settings.media_roots]
        )
        return self.repository.create_job(JobKind.SCAN, payload=payload)

    def queue_inspect(self, candidate_id: int) -> int:
        candidate = self._active_candidate(candidate_id)
        self._validate_candidate_file(candidate)
        return self.repository.create_job(
            JobKind.INSPECT,
            candidate_id=candidate_id,
            payload={
                **self._candidate_payload(candidate),
                "trigger": "manual",
                "candidate_category": candidate["category"],
            },
        )

    def queue_conversion(
        self,
        candidate_id: int,
        *,
        approved: bool,
        approved_by: str,
    ) -> int:
        if not approved:
            raise ValueError("conversion confirmation is required")
        candidate = self._active_candidate(candidate_id)
        category = CandidateCategory(candidate["category"])
        if category not in (CandidateCategory.MEL, CandidateCategory.SIMPLE_FEL):
            raise ValueError("this candidate cannot be converted")
        self._validate_candidate_file(candidate)
        return self.repository.create_job(
            JobKind.CONVERT,
            candidate_id=candidate_id,
            payload={
                **self._candidate_payload(candidate),
                "include_simple": category is CandidateCategory.SIMPLE_FEL,
                "candidate_category": category.value,
                "queue_origin": (
                    "automatic" if approved_by == "local:auto" else "manual"
                ),
                **RuntimeSettings.load(
                    self.settings,
                    self.repository,
                ).conversion_options(),
            },
            approved_by=approved_by,
        )

    def queue_bulk_mel(
        self,
        *,
        approved: bool,
        approved_by: str,
    ) -> tuple[list[int], int]:
        if not approved:
            raise ValueError("bulk conversion confirmation is required")
        job_ids: list[int] = []
        skipped = 0
        for candidate in self.repository.list_candidates(CandidateCategory.MEL.value):
            try:
                job_ids.append(
                    self.queue_conversion(
                        candidate["id"],
                        approved=True,
                        approved_by=approved_by,
                    )
                )
            except (ValueError, PathSafetyError):
                skipped += 1
        return job_ids, skipped

    def queue_backup_deletion(
        self,
        backups: list[dict[str, int | str]],
        *,
        approved_by: str,
    ) -> int:
        if not backups:
            raise ValueError("at least one backup must be selected")
        return self.repository.create_job(
            JobKind.BACKUP_DELETE,
            payload={"backups": backups},
            approved_by=approved_by,
        )

    def _active_candidate(self, candidate_id: int):
        candidate = self.repository.get_candidate(candidate_id)
        if candidate is None or not candidate["active"]:
            raise ValueError("candidate is not active")
        return candidate

    def _validate_candidate_file(self, candidate) -> Path:
        root = self.settings.media_root_by_id(candidate["root_id"])
        path = path_from_relative(root.path, candidate["relative_path"])
        path = validate_media_file(root.path, path)
        require_fingerprint(
            path,
            FileFingerprint(
                size=candidate["file_size"],
                mtime_ns=candidate["file_mtime_ns"],
            ),
        )
        return path

    @staticmethod
    def _candidate_payload(candidate) -> dict[str, int | str]:
        return {
            "root_id": candidate["root_id"],
            "relative_path": candidate["relative_path"],
            "file_size": candidate["file_size"],
            "file_mtime_ns": candidate["file_mtime_ns"],
        }
