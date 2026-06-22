from __future__ import annotations

import asyncio
import time

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.models import JobKind
from app.runtime_settings import RuntimeSettings
from app.web import AppContext, job_payload


async def dashboard_backup_summary(ctx: AppContext) -> tuple[int, int]:
    now = time.monotonic()
    if now < float(ctx.backup_summary_cache["expires_at"]):
        return (
            int(ctx.backup_summary_cache["count"]),
            int(ctx.backup_summary_cache["size"]),
        )
    async with ctx.backup_cache_lock:
        now = time.monotonic()
        if now >= float(ctx.backup_summary_cache["expires_at"]):
            retention_days = int(
                ctx.repository.get_setting(
                    "retention_days",
                    str(ctx.settings.retention_days),
                )
            )
            backups = await asyncio.to_thread(
                ctx.discover_backup_sets,
                ctx.settings.media_roots,
                retention_days,
            )
            ctx.backup_summary_cache.update(
                {
                    "expires_at": now + 60,
                    "count": len(backups),
                    "size": sum(item.total_size for item in backups),
                }
            )
    return (
        int(ctx.backup_summary_cache["count"]),
        int(ctx.backup_summary_cache["size"]),
    )


def register(app: FastAPI, ctx: AppContext) -> None:
    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/versionz")
    async def versionz() -> dict[str, str]:
        return ctx.build_info.as_dict()

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        checks = ctx.readiness_checks()
        ready = all(checks.values())
        return JSONResponse(
            {"status": "ready" if ready else "not_ready", "checks": checks},
            status_code=200 if ready else 503,
        )

    @app.get("/status/summary")
    async def status_summary() -> JSONResponse:
        backup_count, backup_size = await dashboard_backup_summary(ctx)
        checks = ctx.readiness_checks()
        candidate_counts = ctx.repository.candidate_counts()
        job_counts = ctx.repository.job_counts()
        last_scan = ctx.repository.last_scan()
        last_scan_payload = job_payload(last_scan["payload_json"]) if last_scan else {}
        active_scan = ctx.repository.active_scan_job()
        active_scan_payload = (
            job_payload(active_scan["payload_json"]) if active_scan else {}
        )
        runtime = RuntimeSettings.load(ctx.settings, ctx.repository)
        next_schedule = ctx.automation.next_schedule_at()
        return JSONResponse(
            {
                "readiness": {
                    "ok": all(checks.values()),
                    "checks": checks,
                },
                "worker": {
                    "running": ctx.worker.running,
                },
                "candidates": candidate_counts,
                "jobs": job_counts,
                "last_scan": (
                    {
                        "id": last_scan["id"],
                        "state": last_scan["state"],
                        "created_at": last_scan["created_at"],
                        "finished_at": last_scan["finished_at"],
                        "mode": last_scan_payload.get("scan_mode", "full"),
                        "trigger": last_scan_payload.get("trigger", "manual"),
                    }
                    if last_scan
                    else None
                ),
                "backups": {
                    "count": backup_count,
                    "size": backup_size,
                },
                "active": bool(job_counts["queued"] or job_counts["running"]),
                "scan": {
                    "inventory_count": ctx.repository.inventory_count(),
                    "active_mode": active_scan_payload.get("scan_mode")
                    if active_scan
                    else None,
                    "active_roots": active_scan_payload.get("root_ids", []),
                },
                "roots": [
                    {
                        "id": root.id,
                        "label": root.label,
                        "inventory_count": ctx.repository.inventory_count(root.id),
                    }
                    for root in ctx.settings.media_roots
                ],
                "automation": {
                    "pending_requests": ctx.repository.pending_scan_request_count(),
                    "schedule_enabled": runtime.schedule_enabled,
                    "next_run": (next_schedule.isoformat() if next_schedule else None),
                    "webhooks_enabled": runtime.webhooks_enabled,
                    "schedule_interval": runtime.schedule_interval_label,
                },
            }
        )

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        backup_count, backup_size = await dashboard_backup_summary(ctx)
        checks = ctx.readiness_checks()
        return ctx.render(
            request,
            "dashboard.html",
            candidate_counts=ctx.repository.candidate_counts(),
            job_counts=ctx.repository.job_counts(),
            last_scan=ctx.repository.last_scan(),
            backup_count=backup_count,
            backup_size=backup_size,
            readiness_ok=all(checks.values()),
            readiness_checks=checks,
            recent_failures=ctx.repository.recent_failed_jobs(),
            recent_conversions=ctx.repository.list_jobs(
                kind=JobKind.CONVERT.value,
                limit=6,
            ),
        )
