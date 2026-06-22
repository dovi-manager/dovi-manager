import asyncio
import json
import shutil
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.backups import (
    RECOVERY_ARCHIVE_SUFFIX,
    discover_backup_sets,
    validate_recovery_archive,
)
from app.config import Settings
from app.dovi import (
    SubprocessRunner,
    convert_command,
    inspect_command,
    recovery_backup_command,
    recovery_restore_command,
    scan_command,
)
from app.models import (
    BackupMode,
    CandidateCategory,
    FileFingerprint,
    JobKind,
    JobState,
    ScannedFile,
    ScanMode,
)
from app.repository import Repository
from app.runtime_settings import RuntimeSettings
from app.safety import (
    BACKUP_SUFFIX,
    PathSafetyError,
    path_from_relative,
    recovery_backup_storage_requirement,
    recovery_restore_storage_requirement,
    relative_media_path,
    require_conversion_storage,
    require_directory_writable,
    require_fingerprint,
    require_storage,
    validate_media_directory,
    validate_media_file,
)
from app.scanner import ScanParseError, inventory_media, parse_scan_results
from app.worker_support import (
    cleanup_stale_job_dirs,
    job_media_path,
    path_in_scan_scope,
)


Sleep = Callable[[float], Awaitable[None]]
SMART_SCAN_BATCH_SIZE = 50


class JobInterrupted(RuntimeError):
    pass


class JobWorker:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        *,
        runner: SubprocessRunner | None = None,
        sleep: Sleep = asyncio.sleep,
    ):
        self.settings = settings
        self.repository = repository
        self.runner = runner or SubprocessRunner()
        self.sleep = sleep
        self._task: asyncio.Task[None] | None = None
        self._wake_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._stopping = False
        self.current_job_id: int | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self.repository.recover_running_jobs()
        await asyncio.to_thread(cleanup_stale_job_dirs, self.settings.temp_dir)
        self._stopping = False
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="dovi-job-worker")

    async def stop(self) -> None:
        self._stopping = True
        self._stop_event.set()
        self._wake_event.set()
        await self.runner.stop(self.settings.shutdown_grace_seconds)
        if self._task is not None:
            try:
                await asyncio.wait_for(
                    self._task,
                    timeout=self.settings.shutdown_grace_seconds,
                )
            except asyncio.TimeoutError:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def notify(self) -> None:
        self._wake_event.set()

    async def run_once(self) -> bool:
        job = self.repository.claim_next_job()
        if job is None:
            return False
        self.current_job_id = job["id"]
        try:
            await self._process(job)
        except JobInterrupted as exc:
            self.repository.append_job_log(
                job["id"],
                f"\nJob interrupted: {exc}\n",
                self.settings.job_log_limit,
            )
            self.repository.finish_job(
                job["id"],
                JobState.FAILED,
                error=str(exc),
            )
        except Exception as exc:
            self.repository.append_job_log(
                job["id"],
                f"\nWorker error: {exc}\n",
                self.settings.job_log_limit,
            )
            self.repository.finish_job(
                job["id"],
                JobState.FAILED,
                error=str(exc),
            )
        finally:
            self.current_job_id = None
        return True

    async def _run_loop(self) -> None:
        while not self._stopping:
            if await self.run_once():
                continue
            self._wake_event.clear()
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass

    async def _process(self, job: Any) -> None:
        kind = JobKind(job["kind"])
        if kind is JobKind.SCAN:
            await self._run_scan(job)
        elif kind is JobKind.CONVERT:
            await self._run_convert(job)
        elif kind is JobKind.INSPECT:
            await self._run_inspect(job)
        elif kind is JobKind.BACKUP_DELETE:
            await self._run_backup_delete(job)
        elif kind is JobKind.RECOVERY_BACKUP:
            await self._run_recovery_backup(job)
        elif kind is JobKind.RECOVERY_RESTORE:
            await self._run_recovery_restore(job)

    async def _run_command(
        self,
        job_id: int,
        command: list[str],
        *,
        capture_limit_bytes: int | None = None,
    ):
        self.repository.set_job_command(job_id, command)
        pending: list[str] = []
        pending_size = 0
        last_flush = time.monotonic()

        async def log_output(text: str) -> None:
            nonlocal pending_size, last_flush
            pending.append(text)
            pending_size += len(text)
            now = time.monotonic()
            if pending_size >= 64 * 1024 or now - last_flush >= 0.25:
                self.repository.append_job_log(
                    job_id,
                    "".join(pending),
                    self.settings.job_log_limit,
                )
                pending.clear()
                pending_size = 0
                last_flush = now

        try:
            result = await self.runner.run(
                command,
                log_output,
                capture_limit_bytes=capture_limit_bytes,
            )
        finally:
            if pending:
                self.repository.append_job_log(
                    job_id,
                    "".join(pending),
                    self.settings.job_log_limit,
                )
        if self._stopping:
            raise JobInterrupted("application shutdown interrupted the active command")
        return result

    async def _run_scan(self, job: Any) -> None:
        payload = json.loads(job["payload_json"])
        mode = ScanMode(payload.get("scan_mode", ScanMode.FULL.value))
        recursive = bool(payload.get("recursive", True))
        depth = int(payload.get("depth", self.settings.scan_depth))
        debug = bool(payload.get("debug", False))
        target_relative = str(payload.get("target", ""))
        root_ids = payload.get("root_ids")
        if not isinstance(root_ids, list) or not root_ids:
            root_id = str(payload.get("root_id") or "default")
            root_ids = [root_id]

        reconciliations: list[tuple[str, list[ScannedFile], bool, set[str]]] = []
        for root_id in root_ids:
            root = self.settings.media_root_by_id(str(root_id))
            results, replace_all, removed_paths = await self._scan_root(
                job,
                root.id,
                root.path,
                mode,
                target_relative,
                recursive,
                depth,
                debug,
            )
            if results is None:
                return
            reconciliations.append((root.id, results, replace_all, removed_paths))

        all_results: list[ScannedFile] = []
        for root_id, results, replace_all, removed_paths in reconciliations:
            self.repository.reconcile_scan(
                results,
                job["id"],
                root_id=root_id,
                replace_all=replace_all,
                removed_paths=removed_paths,
            )
            all_results.extend(results)

        runtime = RuntimeSettings.load(self.settings, self.repository)
        for result in all_results:
            if result.category is None:
                continue
            candidate = self.repository.get_candidate_by_path(
                result.relative_path,
                result.root_id,
            )
            if candidate is None or not candidate["active"]:
                continue
            if runtime.auto_inspect_enabled(
                result.category.value
            ) and not self.repository.inspection_exists_for_fingerprint(
                candidate["id"],
                candidate["file_size"],
                candidate["file_mtime_ns"],
            ):
                try:
                    self.repository.create_job(
                        JobKind.INSPECT,
                        candidate_id=candidate["id"],
                        payload={
                            "root_id": candidate["root_id"],
                            "relative_path": candidate["relative_path"],
                            "file_size": candidate["file_size"],
                            "file_mtime_ns": candidate["file_mtime_ns"],
                            "candidate_category": candidate["category"],
                            "trigger": "automatic",
                            "source_scan_job_id": job["id"],
                            **(
                                {
                                    "follow_up_conversion": {
                                        "include_simple": False,
                                        "queue_origin": "automatic",
                                        **runtime.conversion_options(),
                                    }
                                }
                                if runtime.auto_queue_mel
                                and runtime.auto_convert_mel_after_inspect
                                and result.category is CandidateCategory.MEL
                                else {}
                            ),
                        },
                    )
                except ValueError:
                    pass
            if (
                runtime.auto_queue_mel
                and result.category is CandidateCategory.MEL
                and not runtime.auto_convert_mel_after_inspect
            ):
                if self.repository.conversion_failed_for_fingerprint(
                    candidate["id"],
                    candidate["file_size"],
                    candidate["file_mtime_ns"],
                ):
                    continue
                try:
                    self.repository.create_job(
                        JobKind.CONVERT,
                        candidate_id=candidate["id"],
                        payload={
                            "root_id": candidate["root_id"],
                            "relative_path": candidate["relative_path"],
                            "file_size": candidate["file_size"],
                            "file_mtime_ns": candidate["file_mtime_ns"],
                            "include_simple": False,
                            "candidate_category": candidate["category"],
                            "queue_origin": "automatic",
                            **runtime.conversion_options(),
                        },
                        approved_by="local:auto",
                    )
                except ValueError:
                    pass
        self.repository.finish_job(
            job["id"],
            JobState.SUCCEEDED,
            exit_code=0,
        )
        self.notify()

    async def _scan_root(
        self,
        job: Any,
        root_id: str,
        media_root: Path,
        mode: ScanMode,
        target_relative: str,
        recursive: bool,
        depth: int,
        debug: bool,
    ) -> tuple[list[ScannedFile] | None, bool, set[str]]:
        self.repository.append_job_log(
            job["id"],
            f"\nScanning library root {root_id}\n",
            self.settings.job_log_limit,
        )
        target = media_root
        if mode is ScanMode.CUSTOM:
            target = validate_media_directory(
                media_root,
                media_root / target_relative,
            )
        elif mode is ScanMode.FILE:
            target = validate_media_file(
                media_root,
                media_root / target_relative,
            )

        current_inventory = await asyncio.to_thread(
            inventory_media,
            media_root,
            target,
            recursive=recursive,
            depth=depth,
        )
        removed_paths: set[str] = set()
        scan_targets: list[Path]
        replace_all = mode is ScanMode.FULL

        if mode is ScanMode.SMART:
            stored = self.repository.inventory(root_id)
            current_by_path = {
                relative_media_path(media_root, path): path
                for path in current_inventory
            }
            scan_targets = []
            for relative_path, path in current_by_path.items():
                stat = path.stat()
                previous = stored.get(relative_path)
                if (
                    previous is None
                    or previous["file_size"] != stat.st_size
                    or previous["file_mtime_ns"] != stat.st_mtime_ns
                ):
                    scan_targets.append(path)
            removed_paths = set(stored) - set(current_by_path)
        else:
            scan_targets = [target]
            if mode is ScanMode.CUSTOM:
                current_paths = {
                    path.relative_to(media_root).as_posix()
                    for path in current_inventory
                }
                removed_paths = {
                    relative_path
                    for relative_path in self.repository.inventory(root_id)
                    if path_in_scan_scope(
                        relative_path,
                        target_relative,
                        recursive,
                        depth,
                    )
                    and relative_path not in current_paths
                }

        results: list[ScannedFile] = []
        if mode is ScanMode.SMART and not scan_targets:
            self.repository.append_job_log(
                job["id"],
                "Smart scan found no new or changed MKV files.\n",
                self.settings.job_log_limit,
            )
        else:
            batches = (
                [
                    scan_targets[index : index + SMART_SCAN_BATCH_SIZE]
                    for index in range(0, len(scan_targets), SMART_SCAN_BATCH_SIZE)
                ]
                if mode is ScanMode.SMART
                else [scan_targets]
            )
            for index, batch in enumerate(batches, start=1):
                if mode is ScanMode.SMART and len(batches) > 1:
                    self.repository.append_job_log(
                        job["id"],
                        f"\nSmart scan batch {index}/{len(batches)}\n",
                        self.settings.job_log_limit,
                    )
                command = scan_command(
                    self.settings.dovi_convert_path,
                    batch,
                    recursive=recursive if mode is not ScanMode.SMART else False,
                    depth=depth,
                    debug=debug,
                )
                result = await self._run_command(
                    job["id"],
                    command,
                    capture_limit_bytes=self.settings.scan_output_limit_bytes,
                )
                if result.output_limit_exceeded:
                    self.repository.finish_job(
                        job["id"],
                        JobState.FAILED,
                        exit_code=result.exit_code,
                        error=(
                            "dovi_convert scan output exceeded "
                            f"{self.settings.scan_output_limit_bytes} bytes"
                        ),
                    )
                    return None, replace_all, removed_paths
                if result.exit_code != 0:
                    self.repository.finish_job(
                        job["id"],
                        JobState.FAILED,
                        exit_code=result.exit_code,
                        error="dovi_convert scan failed",
                    )
                    return None, replace_all, removed_paths
                batch_inventory = batch if mode is ScanMode.SMART else current_inventory
                try:
                    batch_results = await asyncio.to_thread(
                        parse_scan_results,
                        result.output,
                        media_root,
                        batch_inventory,
                        root_id=root_id,
                    )
                except ScanParseError as exc:
                    self.repository.finish_job(
                        job["id"],
                        JobState.FAILED,
                        exit_code=result.exit_code,
                        error=str(exc),
                    )
                    return None, replace_all, removed_paths
                results.extend(batch_results)
        return results, replace_all, removed_paths

    def _job_media_path(self, job: Any) -> tuple[Path, dict[str, Any]]:
        return job_media_path(self.settings, job)

    async def _run_convert(self, job: Any) -> None:
        path, payload = self._job_media_path(job)
        backup_path = path.with_suffix(f".mkv{BACKUP_SUFFIX}")
        archive_path = path.with_suffix(RECOVERY_ARCHIVE_SUFFIX)
        create_recovery_archive = bool(payload.get("create_recovery_archive"))
        try:
            backup_mode = BackupMode(
                payload.get(
                    "backup_mode",
                    "both" if create_recovery_archive else "full_only",
                )
            )
        except ValueError as exc:
            raise PathSafetyError("invalid queued backup mode") from exc
        if backup_path.exists():
            raise PathSafetyError("conversion backup already exists")
        if create_recovery_archive and archive_path.exists():
            archive_valid, archive_reason = validate_recovery_archive(archive_path)
            if not archive_valid:
                raise PathSafetyError(archive_reason)

        reserve_bytes = self.settings.disk_reserve_gib * 1024**3
        require_directory_writable(path.parent)
        require_directory_writable(self.settings.temp_dir)
        if create_recovery_archive:
            require_storage(
                recovery_restore_storage_requirement(
                    path,
                    self.settings.temp_dir,
                    int(payload["file_size"]),
                    reserve_bytes,
                ),
                path.parent,
                self.settings.temp_dir,
            )
        else:
            require_conversion_storage(
                path,
                self.settings.temp_dir,
                int(payload["file_size"]),
                reserve_bytes,
            )

        before = path.stat()
        sleep_task = asyncio.create_task(self.sleep(self.settings.stability_seconds))
        stop_task = asyncio.create_task(self._stop_event.wait())
        done, pending = await asyncio.wait(
            (sleep_task, stop_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if stop_task in done and stop_task.result():
            raise JobInterrupted("application shutdown during file stability check")
        await sleep_task
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (
            after.st_size,
            after.st_mtime_ns,
        ):
            raise PathSafetyError(
                f"file changed during the {self.settings.stability_seconds}-second "
                "stability window"
            )

        job_temp_dir = self.settings.temp_dir / "dovi-manager" / f"job-{job['id']}"
        job_temp_dir.mkdir(parents=True, exist_ok=False)
        try:
            command = convert_command(
                self.settings.dovi_convert_path,
                path,
                job_temp_dir,
                include_simple=bool(payload.get("include_simple")),
                safe_mode=bool(payload.get("safe_mode")),
                verbose=bool(payload.get("verbose")),
                create_recovery_archive=create_recovery_archive,
            )
            result = await self._run_command(job["id"], command)
        finally:
            temp_root = self.settings.temp_dir / "dovi-manager"
            if job_temp_dir.parent.resolve() == temp_root.resolve():
                shutil.rmtree(job_temp_dir, ignore_errors=True)
        if result.exit_code == 0:
            if not path.is_file() or path.stat().st_size <= 0:
                raise PathSafetyError(
                    "conversion reported success but the converted MKV is missing or empty"
                )
            if not backup_path.is_file():
                raise PathSafetyError(
                    "conversion reported success but the original backup is missing"
                )
            if backup_path.stat().st_size != int(payload["file_size"]):
                raise PathSafetyError(
                    "conversion reported success but the backup size does not match "
                    "the scanned original"
                )
            if create_recovery_archive:
                archive_valid, archive_reason = validate_recovery_archive(archive_path)
                if not archive_valid:
                    raise PathSafetyError(archive_reason)
            if backup_mode is BackupMode.COMPACT_ONLY:
                try:
                    backup_path.unlink()
                    self.repository.append_job_log(
                        job["id"],
                        "Compact-only policy: removed validated full original backup.\n",
                        self.settings.job_log_limit,
                    )
                except OSError as exc:
                    if job["candidate_id"] is not None:
                        self.repository.deactivate_candidate(job["candidate_id"])
                    raise PathSafetyError(
                        "conversion succeeded but compact-only cleanup failed; "
                        "the full original was retained"
                    ) from exc
            if job["candidate_id"] is not None:
                self.repository.deactivate_candidate(job["candidate_id"])
            self.repository.finish_job(
                job["id"],
                JobState.SUCCEEDED,
                exit_code=0,
            )
        else:
            self.repository.finish_job(
                job["id"],
                JobState.FAILED,
                exit_code=result.exit_code,
                error="dovi_convert conversion failed",
            )

    async def _run_recovery_backup(self, job: Any) -> None:
        path, payload = self._job_media_path(job)
        archive_path = path.with_suffix(RECOVERY_ARCHIVE_SUFFIX)
        if archive_path.exists():
            raise PathSafetyError("recovery archive already exists")
        require_directory_writable(path.parent)
        require_directory_writable(self.settings.temp_dir)
        reserve_bytes = self.settings.disk_reserve_gib * 1024**3
        require_storage(
            recovery_backup_storage_requirement(
                path,
                self.settings.temp_dir,
                int(payload["file_size"]),
                reserve_bytes,
            ),
            path.parent,
            self.settings.temp_dir,
        )
        job_temp_dir = self.settings.temp_dir / "dovi-manager" / f"job-{job['id']}"
        job_temp_dir.mkdir(parents=True, exist_ok=False)
        try:
            result = await self._run_command(
                job["id"],
                recovery_backup_command(
                    self.settings.dovi_convert_path,
                    path,
                    job_temp_dir,
                ),
            )
        finally:
            shutil.rmtree(job_temp_dir, ignore_errors=True)
        if result.exit_code != 0:
            self.repository.finish_job(
                job["id"],
                JobState.FAILED,
                exit_code=result.exit_code,
                error="dovi_convert recovery backup failed",
            )
            return
        valid, reason = validate_recovery_archive(archive_path)
        if not valid:
            raise PathSafetyError(reason)
        self.repository.finish_job(job["id"], JobState.SUCCEEDED, exit_code=0)

    async def _run_recovery_restore(self, job: Any) -> None:
        payload = json.loads(job["payload_json"])
        root = self.settings.media_root_by_id(str(payload.get("root_id") or "default"))
        path = path_from_relative(
            root.path, payload["relative_path"], require_exists=False
        )
        recovery_path = path_from_relative(root.path, payload["recovery_relative_path"])
        recovery_kind = str(payload.get("recovery_kind") or "compact")
        if recovery_kind not in {"full", "compact"}:
            raise PathSafetyError("invalid recovery type")
        if recovery_path.is_symlink() or not recovery_path.is_file():
            raise PathSafetyError("recovery source is missing or not a regular file")
        if recovery_kind == "full" and not recovery_path.name.endswith(BACKUP_SUFFIX):
            raise PathSafetyError("invalid full original backup path")
        if (
            recovery_kind == "compact"
            and recovery_path.suffix.lower() != RECOVERY_ARCHIVE_SUFFIX
        ):
            raise PathSafetyError("invalid compact recovery path")
        require_fingerprint(
            recovery_path,
            FileFingerprint(
                size=int(payload["recovery_size"]),
                mtime_ns=int(payload["recovery_mtime_ns"]),
            ),
        )
        expected_current = payload.get("file_size") is not None
        if expected_current:
            path = validate_media_file(root.path, path)
            require_fingerprint(
                path,
                FileFingerprint(
                    size=int(payload["file_size"]),
                    mtime_ns=int(payload["file_mtime_ns"]),
                ),
            )
        elif path.exists():
            raise PathSafetyError("target appeared after recovery approval")

        compact_path = None
        if payload.get("compact_relative_path"):
            compact_path = path_from_relative(
                root.path, payload["compact_relative_path"]
            )
            if compact_path.is_symlink() or not compact_path.is_file():
                raise PathSafetyError("compact archive changed before recovery")
            require_fingerprint(
                compact_path,
                FileFingerprint(
                    size=int(payload["compact_size"]),
                    mtime_ns=int(payload["compact_mtime_ns"]),
                ),
            )
        if recovery_kind == "compact":
            valid, reason = validate_recovery_archive(recovery_path)
            if not valid:
                raise PathSafetyError(reason)
            if not expected_current:
                raise PathSafetyError("compact recovery requires the converted MKV")

        await self.sleep(self.settings.stability_seconds)
        require_fingerprint(
            recovery_path,
            FileFingerprint(
                size=int(payload["recovery_size"]),
                mtime_ns=int(payload["recovery_mtime_ns"]),
            ),
        )
        if expected_current:
            require_fingerprint(
                path,
                FileFingerprint(
                    size=int(payload["file_size"]),
                    mtime_ns=int(payload["file_mtime_ns"]),
                ),
            )

        restored_path = path.with_name(f"{path.stem}.restored.mkv")
        if recovery_kind == "compact" and restored_path.exists():
            raise PathSafetyError("restored output already exists")
        require_directory_writable(path.parent)
        reserve_bytes = self.settings.disk_reserve_gib * 1024**3
        if (
            shutil.disk_usage(path.parent).free
            < int(payload["recovery_size"]) + reserve_bytes
        ):
            raise PathSafetyError("insufficient free space for recovery")

        if recovery_kind == "compact":
            require_directory_writable(self.settings.temp_dir)
            restored_estimate = int(payload["file_size"]) + int(
                payload["recovery_size"]
            )
            require_storage(
                recovery_restore_storage_requirement(
                    path,
                    self.settings.temp_dir,
                    restored_estimate,
                    reserve_bytes,
                ),
                path.parent,
                self.settings.temp_dir,
            )

            job_temp_dir = self.settings.temp_dir / "dovi-manager" / f"job-{job['id']}"
            job_temp_dir.mkdir(parents=True, exist_ok=False)
            try:
                result = await self._run_command(
                    job["id"],
                    recovery_restore_command(
                        self.settings.dovi_convert_path,
                        path,
                        job_temp_dir,
                    ),
                )
            finally:
                shutil.rmtree(job_temp_dir, ignore_errors=True)
            if result.exit_code != 0:
                self.repository.finish_job(
                    job["id"],
                    JobState.FAILED,
                    exit_code=result.exit_code,
                    error="dovi_convert recovery restore failed",
                )
                return
            if not restored_path.is_file() or restored_path.is_symlink():
                raise PathSafetyError("restore reported success but output is missing")
            if restored_path.stat().st_size <= 0:
                raise PathSafetyError("restore reported success but output is empty")
            staged_path = restored_path
        else:
            staged_path = path.with_name(f".{path.name}.restore-{job['id']}.tmp")
            if staged_path.exists():
                raise PathSafetyError("recovery staging file already exists")
            await asyncio.to_thread(shutil.copy2, recovery_path, staged_path)
            if staged_path.stat().st_size != int(payload["recovery_size"]):
                staged_path.unlink(missing_ok=True)
                raise PathSafetyError("staged full original size mismatch")

        tombstone = None
        replaced = False
        try:
            if compact_path is not None:
                tombstone = compact_path.with_name(
                    f".{compact_path.name}.restore-{job['id']}.pending-delete"
                )
                if tombstone.exists():
                    raise PathSafetyError("recovery archive tombstone already exists")
                compact_path.replace(tombstone)
            staged_path.replace(path)
            replaced = True
            if tombstone is not None:
                tombstone.unlink()
            self.repository.append_job_log(
                job["id"],
                "Installed recovered Profile 7 MKV and removed the converted file.\n",
                self.settings.job_log_limit,
            )
        except Exception:
            if not replaced and tombstone is not None and tombstone.exists():
                tombstone.replace(compact_path)
            if not replaced:
                staged_path.unlink(missing_ok=True)
            raise
        self.repository.finish_job(job["id"], JobState.SUCCEEDED, exit_code=0)

    async def _run_inspect(self, job: Any) -> None:
        path, payload = self._job_media_path(job)
        result = await self._run_command(
            job["id"],
            inspect_command(self.settings.dovi_convert_path, path),
        )
        succeeded = result.exit_code == 0
        fingerprint_matches = False
        if succeeded:
            try:
                current_stat = path.stat()
                fingerprint_matches = current_stat.st_size == int(
                    payload["file_size"]
                ) and current_stat.st_mtime_ns == int(payload["file_mtime_ns"])
            except OSError:
                pass
        self.repository.finish_job(
            job["id"],
            JobState.SUCCEEDED if succeeded else JobState.FAILED,
            exit_code=result.exit_code,
            error=None if succeeded else "dovi_convert inspect failed",
        )
        follow_up = payload.get("follow_up_conversion")
        if not succeeded or not isinstance(follow_up, dict):
            return
        if not fingerprint_matches:
            return
        candidate_id = job["candidate_id"]
        candidate = (
            self.repository.get_candidate(candidate_id)
            if candidate_id is not None
            else None
        )
        if (
            candidate is None
            or not candidate["active"]
            or candidate["category"] != CandidateCategory.MEL.value
            or candidate["root_id"] != payload.get("root_id")
            or candidate["relative_path"] != payload.get("relative_path")
            or int(candidate["file_size"]) != int(payload["file_size"])
            or int(candidate["file_mtime_ns"]) != int(payload["file_mtime_ns"])
            or self.repository.conversion_exists_for_fingerprint(
                candidate["id"],
                candidate["file_size"],
                candidate["file_mtime_ns"],
            )
        ):
            return
        try:
            self.repository.create_job(
                JobKind.CONVERT,
                candidate_id=candidate["id"],
                payload={
                    "root_id": candidate["root_id"],
                    "relative_path": candidate["relative_path"],
                    "file_size": candidate["file_size"],
                    "file_mtime_ns": candidate["file_mtime_ns"],
                    "include_simple": False,
                    "candidate_category": candidate["category"],
                    "queue_origin": "automatic",
                    "source_inspection_job_id": job["id"],
                    "safe_mode": bool(follow_up.get("safe_mode")),
                    "verbose": bool(follow_up.get("verbose")),
                    "create_recovery_archive": bool(
                        follow_up.get("create_recovery_archive")
                    ),
                    "backup_mode": str(
                        follow_up.get(
                            "backup_mode",
                            "both"
                            if follow_up.get("create_recovery_archive")
                            else "full_only",
                        )
                    ),
                },
                approved_by="local:auto",
            )
        except ValueError:
            return
        self.notify()

    async def _run_backup_delete(self, job: Any) -> None:
        payload = json.loads(job["payload_json"])
        retention_days = int(
            self.repository.get_setting(
                "retention_days",
                str(self.settings.retention_days),
            )
        )
        backup_sets = await asyncio.to_thread(
            discover_backup_sets,
            self.settings.media_roots,
            retention_days,
            now=datetime.now(UTC),
        )
        current: dict[tuple[str, str], Any] = {}
        for backup_set in backup_sets:
            if backup_set.full:
                current[("full", backup_set.full.selection_key)] = backup_set.full
            if backup_set.compact:
                current[("compact", backup_set.compact.selection_key)] = (
                    backup_set.compact
                )
        failures: list[str] = []
        for requested in payload.get("backups", []):
            kind = str(requested.get("kind") or "full")
            relative_path = str(requested["relative_path"])
            root_id = str(requested.get("root_id") or "default")
            selection_key = f"{root_id}:{relative_path}"
            backup = current.get((kind, selection_key))
            retention_override = bool(requested.get("retention_override"))
            can_delete = bool(
                backup
                and (
                    backup.eligible
                    or (retention_override and backup.counterpart_exists)
                )
            )
            if (
                backup is None
                or not can_delete
                or backup.size != int(requested["size"])
                or backup.mtime_ns != int(requested["mtime_ns"])
            ):
                failures.append(relative_path)
                self.repository.append_job_log(
                    job["id"],
                    f"Skipped changed or ineligible backup: {relative_path}\n",
                    self.settings.job_log_limit,
                )
                continue
            backup.path.unlink()
            self.repository.append_job_log(
                job["id"],
                f"Deleted backup: {relative_path}\n",
                self.settings.job_log_limit,
            )

        if failures:
            self.repository.finish_job(
                job["id"],
                JobState.FAILED,
                error=f"{len(failures)} backup(s) were not deleted",
            )
        else:
            self.repository.finish_job(job["id"], JobState.SUCCEEDED, exit_code=0)
