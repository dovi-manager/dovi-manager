from __future__ import annotations

import asyncio
import shutil

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.models import BackupKind, BackupSet
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
        sets = await asyncio.to_thread(
            ctx.discover_backup_sets,
            ctx.settings.media_roots,
            runtime.retention_days,
        )
        return runtime, sets

    async def selected_set(selection_key: str) -> tuple[RuntimeSettings, BackupSet | None]:
        runtime, sets = await discover_for_runtime()
        return runtime, next(
            (item for item in sets if item.selection_key == selection_key), None
        )

    @app.get("/backups", response_class=HTMLResponse)
    async def backups_page(request: Request) -> HTMLResponse:
        runtime, sets = await discover_for_runtime()
        return ctx.render(
            request,
            "backups.html",
            backup_sets=sets,
            retention_days=runtime.retention_days,
            allow_retention_override=runtime.allow_backup_retention_override,
            total_size=sum(item.total_size for item in sets),
            full_count=sum(1 for item in sets if item.full),
            compact_count=sum(1 for item in sets if item.compact),
        )

    @app.get("/backups/manage", response_class=HTMLResponse)
    async def manage_backup(request: Request, item: str) -> Response:
        runtime, selected = await selected_set(item)
        if selected is None:
            return redirect_with_message("/backups", "Backup set not found.", error=True)
        return ctx.render(
            request,
            "backup_manage.html",
            backup_set=selected,
            retention_days=runtime.retention_days,
            allow_retention_override=runtime.allow_backup_retention_override,
        )

    @app.get("/backups/recover/confirm", response_class=HTMLResponse)
    async def recovery_confirmation(
        request: Request,
        item: str,
        kind: BackupKind,
    ) -> Response:
        _, selected = await selected_set(item)
        if selected is None:
            return redirect_with_message("/backups", "Backup set not found.", error=True)
        artifact = selected.full if kind is BackupKind.FULL else selected.compact
        if artifact is None:
            return redirect_with_message(
                f"/backups/manage?item={item}",
                "The selected recovery type is unavailable.",
                error=True,
            )
        if kind is BackupKind.COMPACT and (
            not selected.compact.valid
            or not selected.counterpart_exists
            or selected.compact.restored_exists
        ):
            return redirect_with_message(
                f"/backups/manage?item={item}",
                selected.compact.reason,
                error=True,
            )

        reserve_bytes = ctx.settings.disk_reserve_gib * 1024**3
        requirement = None
        preflight_error = None
        try:
            require_directory_writable(selected.counterpart_path.parent)
            if kind is BackupKind.COMPACT:
                require_directory_writable(ctx.settings.temp_dir)
                current_size = selected.counterpart_path.stat().st_size
                requirement = recovery_restore_storage_requirement(
                    selected.counterpart_path,
                    ctx.settings.temp_dir,
                    current_size + artifact.size,
                    reserve_bytes,
                )
                require_storage(
                    requirement,
                    selected.counterpart_path.parent,
                    ctx.settings.temp_dir,
                )
            elif shutil.disk_usage(selected.counterpart_path.parent).free < (
                artifact.size + reserve_bytes
            ):
                raise PathSafetyError("insufficient free space for full recovery")
        except (OSError, PathSafetyError) as exc:
            preflight_error = str(exc)

        return ctx.render(
            request,
            "restore_confirm.html",
            backup_set=selected,
            recovery_kind=kind.value,
            artifact=artifact,
            storage_requirement=requirement,
            preflight_error=preflight_error,
        )

    @app.post("/backups/recover")
    async def queue_recovery(
        request: Request,
        selected: str = Form(),
        recovery_kind: BackupKind = Form(),
        approved: str | None = Form(default=None),
        _: None = Depends(ctx.require_csrf),
    ) -> RedirectResponse:
        _, backup_set = await selected_set(selected)
        if backup_set is None:
            return redirect_with_message("/backups", "Backup set not found.", error=True)
        artifact = (
            backup_set.full
            if recovery_kind is BackupKind.FULL
            else backup_set.compact
        )
        if artifact is None:
            return redirect_with_message("/backups", "Recovery type unavailable.", error=True)
        if recovery_kind is BackupKind.COMPACT and (
            not backup_set.compact.valid or not backup_set.counterpart_exists
        ):
            return redirect_with_message("/backups", "Compact recovery unavailable.", error=True)

        root = ctx.settings.media_root_by_id(backup_set.root_id)
        current = backup_set.counterpart_path
        current_stat = current.stat() if current.is_file() else None
        compact = backup_set.compact
        try:
            job_id = ctx.job_service.queue_recovery_restore(
                root_id=backup_set.root_id,
                converted_relative_path=backup_set.relative_path,
                converted_size=current_stat.st_size if current_stat else None,
                converted_mtime_ns=current_stat.st_mtime_ns if current_stat else None,
                recovery_kind=recovery_kind.value,
                recovery_relative_path=artifact.path.relative_to(
                    root.path.resolve()
                ).as_posix(),
                recovery_size=artifact.size,
                recovery_mtime_ns=artifact.mtime_ns,
                compact_relative_path=(
                    compact.path.relative_to(root.path.resolve()).as_posix()
                    if compact
                    else None
                ),
                compact_size=compact.size if compact else None,
                compact_mtime_ns=compact.mtime_ns if compact else None,
                approved=approved == "yes",
                approved_by=current_actor(request),
            )
            ctx.worker.notify()
            return redirect_with_message(f"/jobs/{job_id}", "Recovery queued.")
        except (ValueError, OSError) as exc:
            return redirect_with_message(
                f"/backups/recover/confirm?item={selected}&kind={recovery_kind.value}",
                str(exc),
                error=True,
            )

    def artifact_is_selectable(runtime: RuntimeSettings, artifact) -> bool:
        return bool(
            artifact
            and (
                artifact.eligible
                or (
                    runtime.allow_backup_retention_override
                    and artifact.counterpart_exists
                )
            )
        )

    @app.get("/backups/delete/confirm", response_class=HTMLResponse)
    async def deletion_confirmation(
        request: Request,
        item: str,
        kind: str,
    ) -> Response:
        if kind not in {"full", "compact", "both"}:
            return redirect_with_message("/backups", "Invalid deletion type.", error=True)
        runtime, selected = await selected_set(item)
        if selected is None:
            return redirect_with_message("/backups", "Backup set not found.", error=True)
        artifacts = [
            artifact
            for artifact in (
                selected.full if kind in {"full", "both"} else None,
                selected.compact if kind in {"compact", "both"} else None,
            )
            if artifact is not None
        ]
        if not artifacts or any(
            not artifact_is_selectable(runtime, artifact) for artifact in artifacts
        ):
            return redirect_with_message(
                f"/backups/manage?item={item}",
                "One or more selected backups are still protected by retention.",
                error=True,
            )
        remaining = (1 if selected.full else 0) + (1 if selected.compact else 0) - len(
            artifacts
        )
        return ctx.render(
            request,
            "backup_confirm.html",
            backup_set=selected,
            deletion_kind=kind,
            artifacts=artifacts,
            total_size=sum(artifact.size for artifact in artifacts),
            retention_override=any(not artifact.eligible for artifact in artifacts),
            removes_last=remaining == 0,
        )

    @app.post("/backups/delete")
    async def queue_backup_delete(
        request: Request,
        selected: str = Form(),
        deletion_kind: str = Form(),
        approved: str | None = Form(default=None),
        acknowledge_no_recovery: str | None = Form(default=None),
        _: None = Depends(ctx.require_csrf),
    ) -> RedirectResponse:
        if deletion_kind not in {"full", "compact", "both"} or approved != "yes":
            return redirect_with_message("/backups", "Deletion confirmation required.", error=True)
        runtime, backup_set = await selected_set(selected)
        if backup_set is None:
            return redirect_with_message("/backups", "Backup set not found.", error=True)
        pairs = [
            ("full", backup_set.full if deletion_kind in {"full", "both"} else None),
            (
                "compact",
                backup_set.compact if deletion_kind in {"compact", "both"} else None,
            ),
        ]
        chosen = [(kind, artifact) for kind, artifact in pairs if artifact is not None]
        if not chosen or any(
            not artifact_is_selectable(runtime, artifact) for _, artifact in chosen
        ):
            return redirect_with_message("/backups", "Selected backup is protected.", error=True)
        remaining = (
            (1 if backup_set.full else 0)
            + (1 if backup_set.compact else 0)
            - len(chosen)
        )
        if remaining == 0 and acknowledge_no_recovery != "yes":
            return redirect_with_message(
                "/backups",
                "Acknowledge that no recovery source will remain.",
                error=True,
            )
        payload = [
            {
                "kind": kind,
                "root_id": backup_set.root_id,
                "relative_path": artifact.relative_path,
                "size": artifact.size,
                "mtime_ns": artifact.mtime_ns,
                "retention_override": not artifact.eligible,
            }
            for kind, artifact in chosen
        ]
        try:
            job_id = ctx.job_service.queue_backup_deletion(
                payload,
                approved_by=current_actor(request),
            )
            ctx.worker.notify()
            return redirect_with_message(f"/jobs/{job_id}", "Backup deletion queued.")
        except ValueError as exc:
            return redirect_with_message("/backups", str(exc), error=True)

    # Preserve CSRF behavior for bookmarks/forms from the previous UI.
    @app.post("/backups/confirm")
    async def legacy_confirmation(
        _: None = Depends(ctx.require_csrf),
    ) -> RedirectResponse:
        return redirect_with_message("/backups", "Choose Manage backups for a movie.")

    @app.get("/backups/restore/confirm")
    async def legacy_restore_confirmation() -> RedirectResponse:
        return redirect_with_message("/backups", "Choose Manage backups for a movie.")

    @app.post("/backups/restore")
    async def legacy_restore(
        _: None = Depends(ctx.require_csrf),
    ) -> RedirectResponse:
        return redirect_with_message("/backups", "Choose Manage backups for a movie.")
