from __future__ import annotations

import asyncio

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.runtime_settings import RuntimeSettings
from app.safety import (
    PathSafetyError,
    recovery_restore_storage_requirement,
    require_directory_writable,
    require_storage,
)
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
        archives = await asyncio.to_thread(
            ctx.discover_recovery_archives,
            ctx.settings.media_roots,
        )
        return runtime, retention_days, backups, archives

    @app.get("/backups", response_class=HTMLResponse)
    async def backups_page(request: Request) -> HTMLResponse:
        runtime, retention_days, backups, archives = await discover_for_runtime()
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
            recovery_archives=archives,
            recovery_size=sum(item.size for item in archives),
            restorable_count=sum(
                1
                for item in archives
                if item.valid and item.counterpart_exists and not item.restored_exists
            ),
        )

    @app.post("/backups/confirm", response_class=HTMLResponse)
    async def backup_confirmation(
        request: Request,
        selected: list[str] | None = Form(default=None),
        _: None = Depends(ctx.require_csrf),
    ) -> Response:
        runtime, retention_days, backups, _ = await discover_for_runtime()
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
            unrecoverable_backups=[
                item for item in chosen if not item.recovery_archive_valid
            ],
        )

    @app.post("/backups/delete")
    async def queue_backup_delete(
        request: Request,
        selected: list[str] | None = Form(default=None),
        acknowledge_no_recovery: str | None = Form(default=None),
        _: None = Depends(ctx.require_csrf),
    ) -> RedirectResponse:
        runtime, retention_days, backups, _ = await discover_for_runtime()
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
        chosen = [item for path in (selected or []) if (item := available.get(path))]
        if any(not item.recovery_archive_valid for item in chosen) and (
            acknowledge_no_recovery != "yes"
        ):
            return redirect_with_message(
                "/backups",
                "Acknowledge permanent loss of recovery before deleting these backups.",
                error=True,
            )
        payload = []
        for item in chosen:
            requested = {
                "relative_path": item.relative_path,
                "root_id": item.root_id,
                "size": item.size,
                "mtime_ns": item.mtime_ns,
                "retention_override": not item.eligible,
            }
            if item.recovery_archive_valid and item.recovery_archive_path is not None:
                archive_stat = item.recovery_archive_path.stat()
                requested.update(
                    {
                        "recovery_archive_path": item.recovery_archive_path.relative_to(
                            ctx.settings.media_root_by_id(item.root_id).path.resolve()
                        ).as_posix(),
                        "recovery_archive_size": archive_stat.st_size,
                        "recovery_archive_mtime_ns": archive_stat.st_mtime_ns,
                    }
                )
            else:
                requested["no_recovery_acknowledged"] = True
            payload.append(requested)
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

    @app.get("/backups/restore/confirm", response_class=HTMLResponse)
    async def recovery_restore_confirmation(
        request: Request,
        archive: str,
    ) -> Response:
        _, _, _, archives = await discover_for_runtime()
        selected = next(
            (item for item in archives if item.selection_key == archive),
            None,
        )
        if (
            selected is None
            or not selected.valid
            or not selected.counterpart_exists
            or selected.restored_exists
        ):
            return redirect_with_message(
                "/backups",
                "This recovery archive cannot currently be restored.",
                error=True,
            )
        counterpart_stat = selected.counterpart_path.stat()
        reserve_bytes = ctx.settings.disk_reserve_gib * 1024**3
        requirement = recovery_restore_storage_requirement(
            selected.counterpart_path,
            ctx.settings.temp_dir,
            counterpart_stat.st_size + selected.size,
            reserve_bytes,
        )
        preflight_error = None
        try:
            require_directory_writable(selected.counterpart_path.parent)
            require_directory_writable(ctx.settings.temp_dir)
            require_storage(
                requirement,
                selected.counterpart_path.parent,
                ctx.settings.temp_dir,
            )
        except (OSError, PathSafetyError) as exc:
            preflight_error = str(exc)
        return ctx.render(
            request,
            "restore_confirm.html",
            archive=selected,
            storage_requirement=requirement,
            preflight_error=preflight_error,
        )

    @app.post("/backups/restore")
    async def queue_recovery_restore(
        request: Request,
        selected: str = Form(),
        approved: str | None = Form(default=None),
        _: None = Depends(ctx.require_csrf),
    ) -> RedirectResponse:
        _, _, _, archives = await discover_for_runtime()
        archive = next(
            (item for item in archives if item.selection_key == selected),
            None,
        )
        if (
            archive is None
            or not archive.valid
            or not archive.counterpart_exists
            or archive.restored_exists
        ):
            return redirect_with_message(
                "/backups",
                "This recovery archive cannot currently be restored.",
                error=True,
            )
        archive_stat = archive.path.stat()
        counterpart_stat = archive.counterpart_path.stat()
        root = ctx.settings.media_root_by_id(archive.root_id)
        try:
            job_id = ctx.job_service.queue_recovery_restore(
                root_id=archive.root_id,
                archive_relative_path=archive.relative_path,
                archive_size=archive_stat.st_size,
                archive_mtime_ns=archive_stat.st_mtime_ns,
                converted_relative_path=archive.counterpart_path.relative_to(
                    root.path.resolve()
                ).as_posix(),
                converted_size=counterpart_stat.st_size,
                converted_mtime_ns=counterpart_stat.st_mtime_ns,
                approved=approved == "yes",
                approved_by=current_actor(request),
            )
            ctx.worker.notify()
            return redirect_with_message(
                f"/jobs/{job_id}",
                "Recovery restore queued.",
            )
        except (ValueError, OSError) as exc:
            return redirect_with_message(
                f"/backups/restore/confirm?archive={selected}",
                str(exc),
                error=True,
            )
