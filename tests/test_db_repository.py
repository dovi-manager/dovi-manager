import sqlite3
import threading
from pathlib import Path

import pytest

from app.db import connect_database, initialize_database
from app.config import MediaRoot
from app.models import (
    CandidateCategory,
    FileFingerprint,
    JobKind,
    JobState,
    ScannedFile,
    ScanCandidate,
)
from app.repository import Repository


def candidate(path: str = "Movie/movie.mkv") -> ScanCandidate:
    return ScanCandidate(
        relative_path=path,
        category=CandidateCategory.MEL,
        status_text="DV Profile 7 MEL (Safe)",
        action_text="CONVERT",
        fingerprint=FileFingerprint(size=100, mtime_ns=200),
    )


def test_migrates_existing_v1_database(tmp_path: Path) -> None:
    db_path = tmp_path / "old.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE app_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO app_metadata VALUES ('schema_version', '1')")

    initialize_database(db_path)

    connection = connect_database(db_path)
    try:
        assert (
            connection.execute(
                "SELECT value FROM app_metadata WHERE key = 'schema_version'"
            ).fetchone()["value"]
            == "5"
        )
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        connection.close()
    assert {
        "candidates",
        "jobs",
        "editable_settings",
        "media_inventory",
        "scan_requests",
        "library_roots",
        "webhook_path_mappings",
    } <= tables


def test_v5_supports_same_relative_path_in_different_roots(
    repository: Repository,
) -> None:
    repository.sync_library_roots(
        [
            MediaRoot("default", "Movies", Path("/movies")),
            MediaRoot("tv", "TV", Path("/tv")),
        ]
    )
    scan_id = repository.create_job(JobKind.SCAN)
    repository.reconcile_scan(
        [
            ScannedFile(
                root_id="default",
                relative_path="Shared/title.mkv",
                category=CandidateCategory.MEL,
                status_text="DV Profile 7 MEL (Safe)",
                action_text="CONVERT",
                fingerprint=FileFingerprint(100, 200),
            ),
            ScannedFile(
                root_id="tv",
                relative_path="Shared/title.mkv",
                category=CandidateCategory.MEL,
                status_text="DV Profile 7 MEL (Safe)",
                action_text="CONVERT",
                fingerprint=FileFingerprint(300, 400),
            ),
        ],
        scan_id,
    )

    assert repository.inventory_count() == 2


def test_candidate_snapshot_marks_missing_records_inactive(
    repository: Repository,
) -> None:
    scan_id = repository.create_job(JobKind.SCAN)
    repository.replace_candidate_snapshot([candidate()], scan_id)
    repository.replace_candidate_snapshot([], scan_id)

    assert repository.list_candidates() == []
    historical = repository.list_candidates(active_only=False)
    assert len(historical) == 1
    assert historical[0]["active"] == 0


def test_scoped_reconciliation_preserves_untouched_candidates(
    repository: Repository,
) -> None:
    scan_id = repository.create_job(JobKind.SCAN)
    first = candidate("A/first.mkv")
    second = candidate("B/second.mkv")
    repository.replace_candidate_snapshot([first, second], scan_id)

    repository.reconcile_scan(
        [
            ScannedFile(
                relative_path=first.relative_path,
                category=None,
                status_text="HDR10",
                action_text="IGNORE",
                fingerprint=first.fingerprint,
            )
        ],
        scan_id,
    )

    active = repository.list_candidates()
    assert [row["relative_path"] for row in active] == ["B/second.mkv"]
    assert repository.inventory_count() == 1


def test_full_reconciliation_removes_missing_inventory(
    repository: Repository,
) -> None:
    scan_id = repository.create_job(JobKind.SCAN)
    repository.reconcile_scan(
        [
            ScannedFile(
                relative_path="Old/movie.mkv",
                category=CandidateCategory.MEL,
                status_text="DV Profile 7 MEL (Safe)",
                action_text="CONVERT",
                fingerprint=FileFingerprint(100, 200),
            )
        ],
        scan_id,
    )

    repository.reconcile_scan([], scan_id, replace_all=True)

    assert repository.inventory_count() == 0
    assert repository.list_candidates() == []


def test_job_deduplication_and_state_transitions(repository: Repository) -> None:
    first = repository.create_job(JobKind.SCAN)
    with pytest.raises(ValueError, match="already exists"):
        repository.create_job(JobKind.SCAN)

    claimed = repository.claim_next_job()
    assert claimed["id"] == first
    assert claimed["state"] == JobState.RUNNING.value
    repository.finish_job(first, JobState.SUCCEEDED, exit_code=0)
    assert repository.get_job(first)["state"] == JobState.SUCCEEDED.value
    with pytest.raises(ValueError, match="only running"):
        repository.finish_job(first, JobState.FAILED)


def test_conversion_summary_tracks_outcome_format_and_origin(
    repository: Repository,
) -> None:
    scan_id = repository.create_job(JobKind.SCAN)
    repository.claim_next_job()
    repository.finish_job(scan_id, JobState.SUCCEEDED)
    repository.replace_candidate_snapshot([candidate()], scan_id)
    candidate_id = repository.list_candidates()[0]["id"]

    manual = repository.create_job(
        JobKind.CONVERT,
        candidate_id=candidate_id,
        payload={
            "candidate_category": "mel",
            "queue_origin": "manual",
        },
        approved_by="tester",
    )
    repository.claim_next_job()
    repository.finish_job(manual, JobState.SUCCEEDED)
    automatic = repository.create_job(
        JobKind.CONVERT,
        candidate_id=candidate_id,
        payload={
            "candidate_category": "simple_fel",
            "queue_origin": "automatic",
        },
        approved_by="local:auto",
    )
    repository.claim_next_job()
    repository.finish_job(automatic, JobState.FAILED)

    assert repository.conversion_summary() == {
        "succeeded": 1,
        "failed": 1,
        "mel": 1,
        "simple_fel": 1,
        "manual": 1,
        "automatic": 1,
    }


def test_recovery_fails_running_jobs_but_keeps_queued(
    repository: Repository,
) -> None:
    running_id = repository.create_job(JobKind.SCAN)
    repository.claim_next_job()
    queued_id = repository.create_job(JobKind.BACKUP_DELETE, payload={"backups": []})

    assert repository.recover_running_jobs() == 1
    assert repository.get_job(running_id)["state"] == JobState.FAILED.value
    assert repository.get_job(queued_id)["state"] == JobState.QUEUED.value


def test_log_is_bounded(repository: Repository) -> None:
    job_id = repository.create_job(JobKind.SCAN)
    repository.append_job_log(job_id, "12345", 6)
    repository.append_job_log(job_id, "6789", 6)

    job = repository.get_job(job_id)
    assert job["log_text"] == "456789"
    assert job["log_truncated"] == 1


def test_log_collapses_carriage_return_updates(repository: Repository) -> None:
    job_id = repository.create_job(JobKind.SCAN)
    repository.append_job_log(job_id, "start\rprogress", 100)
    repository.append_job_log(job_id, "\rdone\n", 100)

    assert repository.get_job(job_id)["log_text"] == "done\n"


def test_only_queued_jobs_can_be_cancelled(repository: Repository) -> None:
    queued_id = repository.create_job(JobKind.SCAN)
    assert repository.cancel_queued_job(queued_id)
    assert repository.get_job(queued_id)["state"] == JobState.CANCELLED.value
    assert not repository.cancel_queued_job(queued_id)


def test_concurrent_scan_creation_is_atomic(repository: Repository) -> None:
    created: list[int] = []
    rejected: list[str] = []
    barrier = threading.Barrier(2)

    def create() -> None:
        barrier.wait()
        try:
            created.append(repository.create_job(JobKind.SCAN))
        except ValueError as exc:
            rejected.append(str(exc))

    threads = [threading.Thread(target=create) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(created) == 1
    assert len(rejected) == 1
