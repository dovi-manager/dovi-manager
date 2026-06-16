import asyncio
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from app.config import Settings
from app.models import ScanMode
from app.repository import Repository
from app.runtime_settings import RuntimeSettings
from app.services import JobService
from app.worker import JobWorker


Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], datetime]


def _schedule_time_minutes(value: str) -> int:
    hour, minute = (int(part) for part in value.split(":"))
    return hour * 60 + minute


def _with_schedule_time(value: datetime, schedule_time: str) -> datetime:
    minutes = _schedule_time_minutes(schedule_time)
    return value.replace(
        hour=minutes // 60,
        minute=minutes % 60,
        second=0,
        microsecond=0,
    )


def _next_at_or_after(value: datetime, schedule_time: str) -> datetime:
    candidate = _with_schedule_time(value, schedule_time)
    if candidate < value:
        candidate += timedelta(days=1)
    return candidate


def ensure_webhook_token(repository: Repository) -> str:
    token = repository.get_setting("webhook_token", "")
    if token:
        return token
    token = secrets.token_urlsafe(32)
    repository.set_settings({"webhook_token": token})
    return token


def regenerate_webhook_token(repository: Repository) -> str:
    token = secrets.token_urlsafe(32)
    repository.set_settings({"webhook_token": token})
    return token


class AutomationCoordinator:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        job_service: JobService,
        worker: JobWorker,
        *,
        sleep: Sleep = asyncio.sleep,
        clock: Clock | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.job_service = job_service
        self.worker = worker
        self.sleep = sleep
        self.clock = clock or (lambda: datetime.now(UTC))
        self._task: asyncio.Task[None] | None = None
        self._wake_event = asyncio.Event()
        self._stopping = False

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self.repository.recover_claimed_scan_requests()
        self._stopping = False
        self._task = asyncio.create_task(
            self._run_loop(),
            name="dovi-automation-coordinator",
        )

    async def stop(self) -> None:
        self._stopping = True
        self._wake_event.set()
        if self._task is not None:
            await self._task
            self._task = None

    def notify(self) -> None:
        self._wake_event.set()

    def next_schedule_at(self) -> datetime | None:
        runtime = RuntimeSettings.load(self.settings, self.repository)
        if not runtime.schedule_enabled:
            return None
        now = self.clock()
        last_scan = self.repository.last_scan_for_trigger("schedule")
        if last_scan is None:
            return _next_at_or_after(now, runtime.schedule_start_time)
        if last_scan["finished_at"] is None:
            return None
        finished_at = datetime.fromisoformat(last_scan["finished_at"])
        next_run = finished_at + timedelta(minutes=runtime.schedule_interval_minutes)
        if runtime.schedule_interval_unit in {"days", "weeks"}:
            next_run = _next_at_or_after(next_run, runtime.schedule_start_time)
        return next_run

    async def run_once(self) -> bool:
        runtime = RuntimeSettings.load(self.settings, self.repository)
        if runtime.schedule_enabled:
            next_run = self.next_schedule_at()
            if next_run is not None and next_run <= self.clock():
                self.repository.enqueue_scan_request(
                    "smart",
                    relative_path=None,
                    trigger="schedule",
                )

        if self.repository.active_scan_job() is not None:
            return False
        request = self.repository.claim_next_scan_request()
        if request is None:
            return False

        try:
            if request["request_type"] == "file" and request["relative_path"]:
                self.job_service.queue_scan(
                    mode=ScanMode.FILE,
                    trigger=request["trigger"],
                    target=request["relative_path"],
                    root_id=request["root_id"] or "default",
                )
            else:
                kwargs = {}
                if request["root_id"]:
                    kwargs["root_id"] = request["root_id"]
                self.job_service.queue_scan(
                    mode=ScanMode.SMART,
                    trigger=request["trigger"],
                    **kwargs,
                )
        except (OSError, ValueError):
            if request["request_type"] == "file":
                self.repository.enqueue_scan_request(
                    "smart",
                    root_id=request["root_id"],
                    relative_path=None,
                    trigger=request["trigger"],
                )
        finally:
            self.repository.complete_scan_request(request["id"])
        self.worker.notify()
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
