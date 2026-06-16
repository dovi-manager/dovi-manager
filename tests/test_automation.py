import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.automation import AutomationCoordinator
from app.db import database_connection
from app.models import JobKind, JobState, ScanMode
from app.services import JobService


def test_scan_requests_deduplicate_and_recover(repository) -> None:
    first_id, first_created = repository.enqueue_scan_request(
        "smart",
        relative_path=None,
        trigger="schedule",
    )
    second_id, second_created = repository.enqueue_scan_request(
        "smart",
        relative_path=None,
        trigger="radarr",
    )

    assert first_created
    assert not second_created
    assert second_id == first_id
    claimed = repository.claim_next_scan_request()
    assert claimed["id"] == first_id
    assert repository.recover_claimed_scan_requests() == 1
    assert repository.claim_next_scan_request()["id"] == first_id


def test_schedule_queues_initial_smart_scan(settings, repository) -> None:
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    repository.set_settings(
        {
            "schedule_enabled": "true",
            "schedule_interval_minutes": "30",
            "schedule_start_time": "12:00",
        }
    )
    worker = SimpleNamespace(notify=lambda: None)
    coordinator = AutomationCoordinator(
        settings,
        repository,
        JobService(settings, repository),
        worker,
        clock=lambda: now,
    )

    assert asyncio.run(coordinator.run_once())

    job = repository.list_jobs(kind=JobKind.SCAN.value)[0]
    payload = json.loads(job["payload_json"])
    assert payload["scan_mode"] == "smart"
    assert payload["trigger"] == "schedule"
    assert repository.pending_scan_request_count() == 0


def test_schedule_waits_for_configured_first_start_time(settings, repository) -> None:
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    repository.set_settings(
        {
            "schedule_enabled": "true",
            "schedule_interval_minutes": "30",
            "schedule_start_time": "23:15",
        }
    )
    coordinator = AutomationCoordinator(
        settings,
        repository,
        JobService(settings, repository),
        SimpleNamespace(notify=lambda: None),
        clock=lambda: now,
    )

    assert coordinator.next_schedule_at() == now.replace(hour=23, minute=15)
    assert not asyncio.run(coordinator.run_once())


def test_pending_request_waits_for_active_scan(settings, repository) -> None:
    repository.create_job(JobKind.SCAN)
    repository.enqueue_scan_request(
        "smart",
        relative_path=None,
        trigger="radarr",
    )
    coordinator = AutomationCoordinator(
        settings,
        repository,
        JobService(settings, repository),
        SimpleNamespace(notify=lambda: None),
    )

    assert not asyncio.run(coordinator.run_once())
    assert repository.pending_scan_request_count() == 1


def test_root_scoped_smart_request_preserves_root_id(settings, repository) -> None:
    repository.enqueue_scan_request(
        "smart",
        root_id="default",
        relative_path=None,
        trigger="sonarr",
    )
    coordinator = AutomationCoordinator(
        settings,
        repository,
        JobService(settings, repository),
        SimpleNamespace(notify=lambda: None),
    )

    assert asyncio.run(coordinator.run_once())

    job = repository.list_jobs(kind=JobKind.SCAN.value)[0]
    payload = json.loads(job["payload_json"])
    assert payload["scan_mode"] == "smart"
    assert payload["trigger"] == "sonarr"
    assert payload["root_id"] == "default"
    assert payload["root_ids"] == ["default"]


def test_schedule_waits_for_interval_after_previous_completion(
    settings,
    repository,
) -> None:
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    repository.set_settings(
        {
            "schedule_enabled": "true",
            "schedule_interval_minutes": "30",
        }
    )
    service = JobService(settings, repository)
    previous_id = service.queue_scan(
        mode=ScanMode.SMART,
        trigger="schedule",
    )
    repository.claim_next_job()
    repository.finish_job(previous_id, JobState.SUCCEEDED)
    with database_connection(settings.db_path) as connection, connection:
        connection.execute(
            "UPDATE jobs SET finished_at = ? WHERE id = ?",
            ((now - timedelta(minutes=29)).isoformat(), previous_id),
        )
    coordinator = AutomationCoordinator(
        settings,
        repository,
        service,
        SimpleNamespace(notify=lambda: None),
        clock=lambda: now,
    )

    assert not asyncio.run(coordinator.run_once())
    assert coordinator.next_schedule_at() == now + timedelta(minutes=1)

    with database_connection(settings.db_path) as connection, connection:
        connection.execute(
            "UPDATE jobs SET finished_at = ? WHERE id = ?",
            ((now - timedelta(minutes=31)).isoformat(), previous_id),
        )

    assert asyncio.run(coordinator.run_once())
    assert len(repository.list_jobs(kind=JobKind.SCAN.value)) == 2


def test_daily_schedule_aligns_to_start_time_after_interval(
    settings,
    repository,
) -> None:
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    repository.set_settings(
        {
            "schedule_enabled": "true",
            "schedule_interval_value": "1",
            "schedule_interval_unit": "days",
            "schedule_interval_minutes": "1440",
            "schedule_start_time": "03:30",
        }
    )
    service = JobService(settings, repository)
    previous_id = service.queue_scan(
        mode=ScanMode.SMART,
        trigger="schedule",
    )
    repository.claim_next_job()
    repository.finish_job(previous_id, JobState.SUCCEEDED)
    with database_connection(settings.db_path) as connection, connection:
        connection.execute(
            "UPDATE jobs SET finished_at = ? WHERE id = ?",
            ((now - timedelta(days=1)).isoformat(), previous_id),
        )
    coordinator = AutomationCoordinator(
        settings,
        repository,
        service,
        SimpleNamespace(notify=lambda: None),
        clock=lambda: now,
    )

    assert coordinator.next_schedule_at() == datetime(
        2026,
        6,
        16,
        3,
        30,
        tzinfo=UTC,
    )
