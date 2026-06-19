import asyncio
import io
import json
import os
import shutil
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config import Settings
from app.dovi import CommandResult
from app.models import (
    CandidateCategory,
    FileFingerprint,
    JobKind,
    JobState,
    ScannedFile,
    ScanCandidate,
    ScanMode,
)
from app.repository import Repository
from app.services import JobService
from app.worker import JobWorker


class FakeRunner:
    def __init__(self, result: CommandResult | None = None):
        self.result = result or CommandResult(0, "ok\n")
        self.commands: list[list[str]] = []

    async def run(self, command, on_output, *, capture_limit_bytes=None):
        self.commands.append(command)
        await on_output(self.result.output)
        if self.result.exit_code == 0 and len(command) > 2 and command[1] == "convert":
            source = Path(command[2])
            original = source.read_bytes()
            if "--backup" in command:
                write_recovery_archive(source.with_suffix(".dovi"))
            source.with_suffix(".mkv.bak.dovi_convert").write_bytes(original)
            source.write_bytes(b"converted")
        elif self.result.exit_code == 0 and len(command) > 2 and command[1] == "backup":
            write_recovery_archive(Path(command[2]).with_suffix(".dovi"))
        elif (
            self.result.exit_code == 0 and len(command) > 2 and command[1] == "restore"
        ):
            source = Path(command[2])
            source.with_name(f"{source.stem}.restored.mkv").write_bytes(b"restored")
        return self.result

    async def stop(self, grace_seconds: int) -> None:
        pass


def write_recovery_archive(path: Path) -> None:
    payload = b"enhancement-layer"
    with tarfile.open(path, "w") as archive:
        member = tarfile.TarInfo("el.hevc")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))


def add_candidate(
    settings: Settings,
    repository: Repository,
    category: CandidateCategory,
) -> tuple[int, Path]:
    path = settings.media_root / f"{category.value}.mkv"
    path.write_bytes(category.value.encode())
    stat = path.stat()
    scan_job_id = repository.create_job(JobKind.SCAN)
    repository.claim_next_job()
    repository.finish_job(scan_job_id, JobState.SUCCEEDED)
    repository.replace_candidate_snapshot(
        [
            ScanCandidate(
                relative_path=path.name,
                category=category,
                status_text=category.value,
                action_text="CONVERT",
                fingerprint=FileFingerprint(stat.st_size, stat.st_mtime_ns),
            )
        ],
        scan_job_id,
    )
    return repository.list_candidates()[0]["id"], path


def test_simple_fel_requires_approval_and_complex_is_blocked(
    settings: Settings,
    repository: Repository,
) -> None:
    service = JobService(settings, repository)
    simple_id, _ = add_candidate(
        settings,
        repository,
        CandidateCategory.SIMPLE_FEL,
    )
    with pytest.raises(ValueError, match="confirmation"):
        service.queue_conversion(simple_id, approved=False, approved_by="local")

    complex_id, _ = add_candidate(
        settings,
        repository,
        CandidateCategory.COMPLEX_FEL,
    )
    with pytest.raises(ValueError, match="cannot"):
        service.queue_conversion(complex_id, approved=True, approved_by="local")


def test_worker_runs_mel_conversion_and_deactivates_candidate(
    settings: Settings,
    repository: Repository,
) -> None:
    candidate_id, _ = add_candidate(
        settings,
        repository,
        CandidateCategory.MEL,
    )
    service = JobService(settings, repository)
    job_id = service.queue_conversion(
        candidate_id,
        approved=True,
        approved_by="tester",
    )
    runner = FakeRunner()

    async def no_sleep(_: float) -> None:
        pass

    worker = JobWorker(settings, repository, runner=runner, sleep=no_sleep)
    assert asyncio.run(worker.run_once())

    job = repository.get_job(job_id)
    assert job["state"] == JobState.SUCCEEDED.value
    assert "--yes" in runner.commands[0]
    assert "--force" not in runner.commands[0]
    assert "--delete" not in runner.commands[0]
    assert f"job-{job_id}" in runner.commands[0][4]
    assert not (settings.temp_dir / "dovi-manager" / f"job-{job_id}").exists()
    assert repository.get_candidate(candidate_id)["active"] == 0


def test_worker_failure_preserves_candidate(
    settings: Settings,
    repository: Repository,
) -> None:
    candidate_id, _ = add_candidate(
        settings,
        repository,
        CandidateCategory.MEL,
    )
    service = JobService(settings, repository)
    job_id = service.queue_conversion(
        candidate_id,
        approved=True,
        approved_by="tester",
    )
    runner = FakeRunner(CommandResult(1, "conversion failed\n"))

    async def no_sleep(_: float) -> None:
        pass

    worker = JobWorker(settings, repository, runner=runner, sleep=no_sleep)
    asyncio.run(worker.run_once())

    assert repository.get_job(job_id)["state"] == JobState.FAILED.value
    assert repository.get_candidate(candidate_id)["active"] == 1


def test_worker_rejects_success_without_conversion_postconditions(
    settings: Settings,
    repository: Repository,
) -> None:
    candidate_id, _ = add_candidate(settings, repository, CandidateCategory.MEL)
    job_id = JobService(settings, repository).queue_conversion(
        candidate_id,
        approved=True,
        approved_by="tester",
    )

    class NoOutputRunner(FakeRunner):
        async def run(self, command, on_output, *, capture_limit_bytes=None):
            self.commands.append(command)
            return CommandResult(0, "reported success")

    async def no_sleep(_: float) -> None:
        pass

    worker = JobWorker(
        settings,
        repository,
        runner=NoOutputRunner(),
        sleep=no_sleep,
    )
    asyncio.run(worker.run_once())

    job = repository.get_job(job_id)
    assert job["state"] == JobState.FAILED.value
    assert "backup is missing" in job["error"]
    assert repository.get_candidate(candidate_id)["active"] == 1


def test_worker_rejects_file_changed_during_stability_window(
    settings: Settings,
    repository: Repository,
) -> None:
    candidate_id, path = add_candidate(
        settings,
        repository,
        CandidateCategory.MEL,
    )
    service = JobService(settings, repository)
    job_id = service.queue_conversion(
        candidate_id,
        approved=True,
        approved_by="tester",
    )
    runner = FakeRunner()

    async def change_file(_: float) -> None:
        path.write_bytes(b"changed while waiting")

    worker = JobWorker(settings, repository, runner=runner, sleep=change_file)
    asyncio.run(worker.run_once())

    job = repository.get_job(job_id)
    assert job["state"] == JobState.FAILED.value
    assert "stability window" in job["error"]
    assert runner.commands == []


def scan_row(name: str, status: str, action: str) -> str:
    return f"{name:<50} {status:<36} {action}"


def test_successful_scan_replaces_snapshot_and_auto_queues_mel(
    settings: Settings,
    repository: Repository,
) -> None:
    movie = settings.media_root / "Movie.mkv"
    movie.write_bytes(b"movie")
    output = "\n".join(
        [
            "Filename                                           Format                               Action",
            "-" * 96,
            scan_row("Movie.mkv", "DV Profile 7 MEL (Safe)", "CONVERT"),
        ]
    )
    repository.set_settings({"auto_queue_mel": "true"})
    scan_id = repository.create_job(JobKind.SCAN)
    runner = FakeRunner(CommandResult(0, output))
    worker = JobWorker(settings, repository, runner=runner)

    asyncio.run(worker.run_once())

    assert repository.get_job(scan_id)["state"] == JobState.SUCCEEDED.value
    candidates = repository.list_candidates()
    assert len(candidates) == 1
    jobs = repository.list_jobs()
    conversion = next(job for job in jobs if job["kind"] == JobKind.CONVERT.value)
    assert conversion["approved_by"] == "local:auto"


def test_scan_auto_inspection_precedes_auto_conversion(
    settings: Settings,
    repository: Repository,
) -> None:
    movie = settings.media_root / "Movie.mkv"
    movie.write_bytes(b"movie")
    output = "\n".join(
        [
            "Filename                                           Format                               Action",
            "-" * 96,
            scan_row("Movie.mkv", "DV Profile 7 MEL (Safe)", "CONVERT"),
        ]
    )
    repository.set_settings(
        {
            "auto_queue_mel": "true",
            "auto_inspect_mel": "true",
        }
    )
    repository.create_job(JobKind.SCAN)

    asyncio.run(
        JobWorker(
            settings,
            repository,
            runner=FakeRunner(CommandResult(0, output)),
        ).run_once()
    )

    jobs = repository.list_jobs()
    automatic = [job for job in reversed(jobs) if job["kind"] != "scan"]
    assert [job["kind"] for job in automatic] == ["inspect", "convert"]
    inspect_payload = json.loads(automatic[0]["payload_json"])
    assert inspect_payload["trigger"] == "automatic"
    assert inspect_payload["source_scan_job_id"] == 1


def test_scan_gates_auto_conversion_until_inspection_succeeds(
    settings: Settings,
    repository: Repository,
) -> None:
    movie = settings.media_root / "Movie.mkv"
    movie.write_bytes(b"movie")
    output = "\n".join(
        [
            "Filename                                           Format                               Action",
            "-" * 96,
            scan_row("Movie.mkv", "DV Profile 7 MEL (Safe)", "CONVERT"),
        ]
    )
    repository.set_settings(
        {
            "auto_queue_mel": "true",
            "auto_inspect_mel": "true",
            "auto_convert_mel_after_inspect": "true",
            "convert_safe_mode": "true",
        }
    )
    repository.create_job(JobKind.SCAN)
    runner = FakeRunner(CommandResult(0, output))
    worker = JobWorker(settings, repository, runner=runner)

    asyncio.run(worker.run_once())

    queued = repository.list_jobs()
    assert [job["kind"] for job in queued] == ["inspect", "scan"]
    inspect_job = queued[0]
    inspect_payload = json.loads(inspect_job["payload_json"])
    assert inspect_payload["follow_up_conversion"]["safe_mode"] is True

    runner.result = CommandResult(0, "inspection complete")
    asyncio.run(worker.run_once())

    conversions = repository.list_jobs(kind=JobKind.CONVERT.value)
    assert len(conversions) == 1
    conversion_payload = json.loads(conversions[0]["payload_json"])
    assert conversion_payload["source_inspection_job_id"] == inspect_job["id"]
    assert conversion_payload["queue_origin"] == "automatic"
    assert conversion_payload["safe_mode"] is True


def test_failed_gated_inspection_does_not_queue_conversion(
    settings: Settings,
    repository: Repository,
) -> None:
    movie = settings.media_root / "Movie.mkv"
    movie.write_bytes(b"movie")
    output = "\n".join(
        [
            "Filename                                           Format                               Action",
            "-" * 96,
            scan_row("Movie.mkv", "DV Profile 7 MEL (Safe)", "CONVERT"),
        ]
    )
    repository.set_settings(
        {
            "auto_queue_mel": "true",
            "auto_inspect_mel": "true",
            "auto_convert_mel_after_inspect": "true",
        }
    )
    repository.create_job(JobKind.SCAN)
    runner = FakeRunner(CommandResult(0, output))
    worker = JobWorker(settings, repository, runner=runner)
    asyncio.run(worker.run_once())

    runner.result = CommandResult(1, "inspection failed")
    asyncio.run(worker.run_once())

    assert repository.list_jobs(kind=JobKind.CONVERT.value) == []


def test_manual_inspection_never_releases_automatic_conversion(
    settings: Settings,
    repository: Repository,
) -> None:
    candidate_id, _ = add_candidate(settings, repository, CandidateCategory.MEL)
    JobService(settings, repository).queue_inspect(candidate_id)

    asyncio.run(JobWorker(settings, repository, runner=FakeRunner()).run_once())

    assert repository.list_jobs(kind=JobKind.CONVERT.value) == []


def test_gated_inspection_revalidates_file_fingerprint(
    settings: Settings,
    repository: Repository,
) -> None:
    candidate_id, path = add_candidate(settings, repository, CandidateCategory.MEL)
    candidate = repository.get_candidate(candidate_id)
    repository.create_job(
        JobKind.INSPECT,
        candidate_id=candidate_id,
        payload={
            "root_id": candidate["root_id"],
            "relative_path": candidate["relative_path"],
            "file_size": candidate["file_size"],
            "file_mtime_ns": candidate["file_mtime_ns"],
            "candidate_category": candidate["category"],
            "trigger": "automatic",
            "follow_up_conversion": {
                "include_simple": False,
                "queue_origin": "automatic",
                "safe_mode": False,
                "verbose": False,
            },
        },
    )

    class MutatingInspectRunner(FakeRunner):
        async def run(self, command, on_output, *, capture_limit_bytes=None):
            result = await super().run(
                command,
                on_output,
                capture_limit_bytes=capture_limit_bytes,
            )
            path.write_bytes(b"changed after inspection started")
            return result

    asyncio.run(
        JobWorker(settings, repository, runner=MutatingInspectRunner()).run_once()
    )

    assert repository.list_jobs(kind=JobKind.CONVERT.value) == []


def test_gated_inspection_does_not_duplicate_existing_conversion(
    settings: Settings,
    repository: Repository,
) -> None:
    candidate_id, _ = add_candidate(settings, repository, CandidateCategory.MEL)
    candidate = repository.get_candidate(candidate_id)
    payload = {
        "root_id": candidate["root_id"],
        "relative_path": candidate["relative_path"],
        "file_size": candidate["file_size"],
        "file_mtime_ns": candidate["file_mtime_ns"],
        "candidate_category": candidate["category"],
        "queue_origin": "automatic",
        "safe_mode": False,
        "verbose": False,
    }
    conversion_id = repository.create_job(
        JobKind.CONVERT,
        candidate_id=candidate_id,
        payload=payload,
        approved_by="local:auto",
    )
    repository.claim_next_job()
    repository.finish_job(conversion_id, JobState.FAILED)
    repository.create_job(
        JobKind.INSPECT,
        candidate_id=candidate_id,
        payload={
            **payload,
            "trigger": "automatic",
            "follow_up_conversion": {
                "include_simple": False,
                "queue_origin": "automatic",
                "safe_mode": False,
                "verbose": False,
            },
        },
    )

    asyncio.run(JobWorker(settings, repository, runner=FakeRunner()).run_once())

    assert len(repository.list_jobs(kind=JobKind.CONVERT.value)) == 1


def test_smart_scan_only_processes_new_or_changed_files(
    settings: Settings,
    repository: Repository,
) -> None:
    old = settings.media_root / "Old.mkv"
    new = settings.media_root / "New.mkv"
    old.write_bytes(b"old")
    old_stat = old.stat()
    seed_job = repository.create_job(JobKind.SCAN)
    repository.claim_next_job()
    repository.finish_job(seed_job, JobState.SUCCEEDED)
    repository.reconcile_scan(
        [
            ScannedFile(
                relative_path=old.name,
                category=CandidateCategory.MEL,
                status_text="DV Profile 7 MEL (Safe)",
                action_text="CONVERT",
                fingerprint=FileFingerprint(old_stat.st_size, old_stat.st_mtime_ns),
            )
        ],
        seed_job,
    )
    new.write_bytes(b"new")
    output = "\n".join(
        [
            "---------------------------------------------------",
            "File:   New.mkv",
            "Status: DV Profile 7 FEL (Simple)",
            "Action: CONVERT*",
            "---------------------------------------------------",
        ]
    )
    service = JobService(settings, repository)
    scan_id = service.queue_scan(mode=ScanMode.SMART)
    runner = FakeRunner(CommandResult(0, output))

    asyncio.run(JobWorker(settings, repository, runner=runner).run_once())

    assert repository.get_job(scan_id)["state"] == JobState.SUCCEEDED.value
    assert runner.commands[0][2:] == [str(new)]
    assert {row["relative_path"] for row in repository.list_candidates()} == {
        "Old.mkv",
        "New.mkv",
    }


def test_smart_scan_processes_all_configured_roots(
    settings: Settings,
    repository: Repository,
) -> None:
    repository.sync_library_roots(settings.media_roots)
    (settings.media_root / "Movie.mkv").write_bytes(b"movie")
    (settings.shows_root / "Episode.mkv").write_bytes(b"episode")
    outputs = [
        "\n".join(
            [
                "File:   Movie.mkv",
                "Status: DV Profile 7 MEL (Safe)",
                "Action: CONVERT",
            ]
        ),
        "\n".join(
            [
                "File:   Episode.mkv",
                "Status: DV Profile 7 FEL (Simple)",
                "Action: CONVERT*",
            ]
        ),
    ]

    class RootRunner(FakeRunner):
        async def run(self, command, on_output, *, capture_limit_bytes=None):
            self.commands.append(command)
            output = outputs[len(self.commands) - 1]
            await on_output(output)
            return CommandResult(0, output)

    scan_id = JobService(settings, repository).queue_scan(mode=ScanMode.SMART)
    runner = RootRunner()
    asyncio.run(JobWorker(settings, repository, runner=runner).run_once())

    assert repository.get_job(scan_id)["state"] == JobState.SUCCEEDED.value
    assert repository.inventory_count("default") == 1
    assert repository.inventory_count("shows") == 1
    assert {row["root_id"] for row in repository.list_candidates()} == {
        "default",
        "shows",
    }


def test_smart_scan_deactivates_deleted_files_without_cli_work(
    settings: Settings,
    repository: Repository,
) -> None:
    movie = settings.media_root / "Deleted.mkv"
    movie.write_bytes(b"movie")
    stat = movie.stat()
    seed_job = repository.create_job(JobKind.SCAN)
    repository.claim_next_job()
    repository.finish_job(seed_job, JobState.SUCCEEDED)
    repository.reconcile_scan(
        [
            ScannedFile(
                relative_path=movie.name,
                category=CandidateCategory.MEL,
                status_text="DV Profile 7 MEL (Safe)",
                action_text="CONVERT",
                fingerprint=FileFingerprint(stat.st_size, stat.st_mtime_ns),
            )
        ],
        seed_job,
    )
    movie.unlink()
    scan_id = JobService(settings, repository).queue_scan(mode=ScanMode.SMART)
    runner = FakeRunner()

    asyncio.run(JobWorker(settings, repository, runner=runner).run_once())

    assert repository.get_job(scan_id)["state"] == JobState.SUCCEEDED.value
    assert runner.commands == []
    assert repository.inventory_count() == 0
    assert repository.list_candidates() == []


def test_scan_and_conversion_jobs_snapshot_runtime_options(
    settings: Settings,
    repository: Repository,
) -> None:
    folder = settings.media_root / "Movies"
    folder.mkdir()
    repository.set_settings(
        {
            "scan_depth": "9",
            "scan_debug": "true",
            "convert_safe_mode": "true",
            "convert_verbose": "true",
        }
    )
    service = JobService(settings, repository)

    scan_id = service.queue_scan(
        mode=ScanMode.CUSTOM,
        target="Movies",
        recursive=False,
    )
    scan_payload = repository.get_job(scan_id)["payload_json"]

    assert '"scan_mode":"custom"' in scan_payload
    assert '"depth":9' in scan_payload
    assert '"debug":true' in scan_payload

    repository.cancel_queued_job(scan_id)
    candidate_id, _ = add_candidate(
        settings,
        repository,
        CandidateCategory.MEL,
    )
    conversion_id = service.queue_conversion(
        candidate_id,
        approved=True,
        approved_by="local",
    )
    conversion_payload = json.loads(repository.get_job(conversion_id)["payload_json"])

    assert conversion_payload["safe_mode"] is True
    assert conversion_payload["verbose"] is True


def test_auto_queue_does_not_retry_failed_unchanged_conversion(
    settings: Settings,
    repository: Repository,
) -> None:
    movie = settings.media_root / "Movie.mkv"
    movie.write_bytes(b"movie")
    stat = movie.stat()
    old_scan = repository.create_job(JobKind.SCAN)
    repository.claim_next_job()
    repository.finish_job(old_scan, JobState.SUCCEEDED)
    repository.replace_candidate_snapshot(
        [
            ScanCandidate(
                relative_path=movie.name,
                category=CandidateCategory.MEL,
                status_text="DV Profile 7 MEL (Safe)",
                action_text="CONVERT",
                fingerprint=FileFingerprint(stat.st_size, stat.st_mtime_ns),
            )
        ],
        old_scan,
    )
    candidate_id = repository.list_candidates()[0]["id"]
    failed_id = repository.create_job(
        JobKind.CONVERT,
        candidate_id=candidate_id,
        payload={
            "relative_path": movie.name,
            "file_size": stat.st_size,
            "file_mtime_ns": stat.st_mtime_ns,
            "include_simple": False,
        },
    )
    repository.claim_next_job()
    repository.finish_job(failed_id, JobState.FAILED, error="failed")
    repository.set_settings({"auto_queue_mel": "true"})

    output = "\n".join(
        [
            "Filename                                           Format                               Action",
            "-" * 96,
            scan_row("Movie.mkv", "DV Profile 7 MEL (Safe)", "CONVERT"),
        ]
    )
    scan_id = repository.create_job(JobKind.SCAN)
    worker = JobWorker(
        settings,
        repository,
        runner=FakeRunner(CommandResult(0, output)),
    )
    asyncio.run(worker.run_once())

    assert repository.get_job(scan_id)["state"] == JobState.SUCCEEDED.value
    conversions = repository.list_jobs(kind=JobKind.CONVERT.value)
    assert [job["id"] for job in conversions] == [failed_id]


def test_scan_output_limit_keeps_previous_snapshot(
    settings: Settings,
    repository: Repository,
) -> None:
    candidate_id, _ = add_candidate(settings, repository, CandidateCategory.MEL)
    scan_id = repository.create_job(JobKind.SCAN)

    class LimitedRunner(FakeRunner):
        async def run(self, command, on_output, *, capture_limit_bytes=None):
            return CommandResult(0, "partial", output_limit_exceeded=True)

    worker = JobWorker(settings, repository, runner=LimitedRunner())
    asyncio.run(worker.run_once())

    assert repository.get_job(scan_id)["state"] == JobState.FAILED.value
    assert repository.get_candidate(candidate_id)["active"] == 1


def test_conversion_rejects_insufficient_media_space(
    settings: Settings,
    repository: Repository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_id, _ = add_candidate(settings, repository, CandidateCategory.MEL)
    job_id = JobService(settings, repository).queue_conversion(
        candidate_id,
        approved=True,
        approved_by="tester",
    )
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _: shutil._ntuple_diskusage(total=100, used=100, free=0),
    )
    worker = JobWorker(settings, repository, runner=FakeRunner())
    asyncio.run(worker.run_once())

    job = repository.get_job(job_id)
    assert job["state"] == JobState.FAILED.value
    assert "insufficient free space" in job["error"]


def test_failed_scan_parse_keeps_previous_snapshot(
    settings: Settings,
    repository: Repository,
) -> None:
    candidate_id, _ = add_candidate(
        settings,
        repository,
        CandidateCategory.MEL,
    )
    scan_id = repository.create_job(JobKind.SCAN)
    worker = JobWorker(
        settings,
        repository,
        runner=FakeRunner(CommandResult(0, "unexpected output")),
    )

    asyncio.run(worker.run_once())

    assert repository.get_job(scan_id)["state"] == JobState.FAILED.value
    assert repository.get_candidate(candidate_id)["active"] == 1


def make_old_backup(settings: Settings) -> Path:
    counterpart = settings.media_root / "Movie.mkv"
    counterpart.write_bytes(b"converted")
    backup = settings.media_root / "Movie.mkv.bak.dovi_convert"
    backup.write_bytes(b"original")
    old_timestamp = (datetime.now(UTC) - timedelta(days=60)).timestamp()
    os.utime(backup, (old_timestamp, old_timestamp))
    return backup


def make_recent_backup(settings: Settings) -> Path:
    counterpart = settings.media_root / "Recent.mkv"
    counterpart.write_bytes(b"converted")
    backup = settings.media_root / "Recent.mkv.bak.dovi_convert"
    backup.write_bytes(b"original")
    recent_timestamp = (datetime.now(UTC) - timedelta(days=1)).timestamp()
    os.utime(backup, (recent_timestamp, recent_timestamp))
    return backup


def test_backup_deletion_revalidates_and_records_approver(
    settings: Settings,
    repository: Repository,
) -> None:
    backup = make_old_backup(settings)
    stat = backup.stat()
    service = JobService(settings, repository)
    job_id = service.queue_backup_deletion(
        [
            {
                "relative_path": backup.name,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "no_recovery_acknowledged": True,
            }
        ],
        approved_by="tester",
    )
    worker = JobWorker(settings, repository, runner=FakeRunner())

    asyncio.run(worker.run_once())

    assert not backup.exists()
    job = repository.get_job(job_id)
    assert job["state"] == JobState.SUCCEEDED.value
    assert job["approved_by"] == "tester"


def test_backup_deletion_allows_snapshotted_retention_override(
    settings: Settings,
    repository: Repository,
) -> None:
    backup = make_recent_backup(settings)
    stat = backup.stat()
    service = JobService(settings, repository)
    job_id = service.queue_backup_deletion(
        [
            {
                "relative_path": backup.name,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "retention_override": True,
                "no_recovery_acknowledged": True,
            }
        ],
        approved_by="tester",
    )
    worker = JobWorker(settings, repository, runner=FakeRunner())

    asyncio.run(worker.run_once())

    assert not backup.exists()
    assert repository.get_job(job_id)["state"] == JobState.SUCCEEDED.value


def test_changed_backup_is_not_deleted(
    settings: Settings,
    repository: Repository,
) -> None:
    backup = make_old_backup(settings)
    stat = backup.stat()
    service = JobService(settings, repository)
    job_id = service.queue_backup_deletion(
        [
            {
                "relative_path": backup.name,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "no_recovery_acknowledged": True,
            }
        ],
        approved_by="tester",
    )
    backup.write_bytes(b"changed after approval")
    worker = JobWorker(settings, repository, runner=FakeRunner())

    asyncio.run(worker.run_once())

    assert backup.exists()
    assert repository.get_job(job_id)["state"] == JobState.FAILED.value


def test_shutdown_interrupts_stability_wait(
    settings: Settings,
    repository: Repository,
) -> None:
    candidate_id, _ = add_candidate(settings, repository, CandidateCategory.MEL)
    job_id = JobService(settings, repository).queue_conversion(
        candidate_id,
        approved=True,
        approved_by="tester",
    )
    sleep_started = asyncio.Event()

    async def long_sleep(_: float) -> None:
        sleep_started.set()
        await asyncio.sleep(60)

    async def exercise() -> None:
        worker = JobWorker(
            settings,
            repository,
            runner=FakeRunner(),
            sleep=long_sleep,
        )
        await worker.start()
        await asyncio.wait_for(sleep_started.wait(), timeout=2)
        await asyncio.wait_for(worker.stop(), timeout=2)

    asyncio.run(exercise())

    job = repository.get_job(job_id)
    assert job["state"] == JobState.FAILED.value
    assert "shutdown" in job["error"]


def test_worker_start_cleans_only_owned_stale_temp_directories(
    settings: Settings,
    repository: Repository,
) -> None:
    temp_root = settings.temp_dir / "dovi-manager"
    stale = temp_root / "job-123"
    unrelated = temp_root / "keep-me"
    stale.mkdir(parents=True)
    unrelated.mkdir()

    async def exercise() -> None:
        worker = JobWorker(settings, repository, runner=FakeRunner())
        await worker.start()
        await worker.stop()

    asyncio.run(exercise())

    assert not stale.exists()
    assert unrelated.exists()
