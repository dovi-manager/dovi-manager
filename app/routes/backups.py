from __future__ import annotations

import asyncio

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.runtime_settings import RuntimeSettings
from app.web import AppContext, current_actor, redirect_with_message


def register(app: FastAPI, ctx: AppContext) -> None:
    async def discover_for_runtime():
        runtime = RuntimeSettings.load(ctx.settings, ctx.repository)
        retention_days = runtime.retention_days
        backups = await asyncio.to_thread(
            ctx.discover_backups,
            ctx.settings.media_roots,
            retention_days,
        )
        return runtime, retention_days, backups

    @app.get("/backups", response_class=HTMLResponse)
    async def backups_page(request: Request) -> HTMLResponse:
        runtime, retention_days, backups = await discover_for_runtime()
        override_count = sum(
            1
            for item in backups
            if item.counterpart_exists
            and not item.eligible
            and item.age_days < retention_days
        )
        return ctx.render(
            request,
            "backups.html",
            backups=backups,
            retention_days=retention_days,
            allow_retention_override=runtime.allow_backup_retention_override,
            total_size=sum(item.size for item in backups),
            eligible_count=sum(1 for item in backups if item.eligible),
            eligible_size=sum(item.size for item in backups if item.eligible),
            override_count=override_count,
        )

    @app.post("/backups/confirm", response_class=HTMLResponse)
    async def backup_confirmation(
        request: Request,
        selected: list[str] | None = Form(default=None),
        _: None = Depends(ctx.require_csrf),
    ) -> Response:
        runtime, retention_days, backups = await discover_for_runtime()
        available = {
            item.selection_key: item
            for item in backups
            if item.eligible
            or (
                runtime.allow_backup_retention_override
                and item.counterpart_exists
                and item.age_days < retention_days
            )
        }
        chosen = [available[path] for path in (selected or []) if path in available]
        if not chosen:
            return redirect_with_message(
                "/backups",
                "Select at least one backup that can be reviewed.",
                error=True,
            )
        override_backups = [item for item in chosen if not item.eligible]
        return ctx.render(
            request,
            "backup_confirm.html",
            backups=chosen,
            override_backups=override_backups,
            total_size=sum(item.size for item in chosen),
        )

    @app.post("/backups/delete")
    async def queue_backup_delete(
        request: Request,
        selected: list[str] | None = Form(default=None),
        _: None = Depends(ctx.require_csrf),
    ) -> RedirectResponse:
        runtime, retention_days, backups = await discover_for_runtime()
        available = {
            item.selection_key: item
            for item in backups
            if item.eligible
            or (
                runtime.allow_backup_retention_override
                and item.counterpart_exists
                and item.age_days < retention_days
            )
        }
        payload = [
            {
                "relative_path": item.relative_path,
                "root_id": item.root_id,
                "size": item.size,
                "mtime_ns": item.mtime_ns,
                "retention_override": not item.eligible,
            }
            for path in (selected or [])
            if (item := available.get(path)) is not None
        ]
        try:
            job_id = ctx.job_service.queue_backup_deletion(
                payload,
                approved_by=current_actor(request),
            )
            ctx.worker.notify()
            return redirect_with_message(
                f"/jobs/{job_id}",
                "Backup deletion queued.",
            )
        except ValueError as exc:
            return redirect_with_message("/backups", str(exc), error=True)
