from __future__ import annotations

import math

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.backups import RECOVERY_ARCHIVE_SUFFIX, validate_recovery_archive
from app.models import BackupMode, CandidateCategory, JobKind
from app.runtime_settings import RuntimeSettings
from app.safety import (
    PathSafetyError,
    conversion_storage_requirement,
    conversion_with_recovery_storage_requirement,
    path_from_relative,
    require_directory_writable,
    require_storage,
)
from app.web import (
    AppContext,
    PAGE_SIZE,
    current_actor,
    redirect_with_message,
)


def _conversion_requirement(
    path,
    temp_dir,
    source_size: int,
    reserve_bytes: int,
    backup_mode: BackupMode,
):
    if backup_mode is BackupMode.FULL_ONLY:
        return conversion_storage_requirement(
            path, temp_dir, source_size, reserve_bytes
        )
    return conversion_with_recovery_storage_requirement(
        path, temp_dir, source_size, reserve_bytes
    )


def _conversion_preflight(ctx: AppContext, candidate, mode: BackupMode):
    root = ctx.settings.media_root_by_id(candidate["root_id"])
    path = path_from_relative(root.path, candidate["relative_path"])
    require_directory_writable(path.parent)
    require_directory_writable(ctx.settings.temp_dir)
    requirement = _conversion_requirement(
        path,
        ctx.settings.temp_dir,
        int(candidate["file_size"]),
        ctx.settings.disk_reserve_gib * 1024**3,
        mode,
    )
    return path, requirement


def _require_conversion_preflight(ctx: AppContext, candidate, mode: BackupMode):
    path, requirement = _conversion_preflight(ctx, candidate, mode)
    require_storage(requirement, path.parent, ctx.settings.temp_dir)
    return requirement


def register(app: FastAPI, ctx: AppContext) -> None:
    @app.get("/candidates", response_class=HTMLResponse)
    async def candidates_page(
        request: Request,
        category: str | None = None,
        q: str = "",
        page: int = 1,
    ) -> HTMLResponse:
        if category and category not in {item.value for item in CandidateCategory}:
            category = None
        page = max(1, page)
        q = q.strip()
        total = ctx.repository.count_candidates(category, search=q or None)
        page_count = max(1, math.ceil(total / PAGE_SIZE))
        page = min(page, page_count)
        return ctx.render(
            request,
            "candidates.html",
            candidates=ctx.repository.list_candidates(
                category,
                search=q or None,
                limit=PAGE_SIZE,
                offset=(page - 1) * PAGE_SIZE,
            ),
            selected_category=category,
            counts=ctx.repository.candidate_counts(),
            search=q,
            page=page,
            page_count=page_count,
            total=total,
        )

    @app.get("/candidates/{candidate_id:int}", response_class=HTMLResponse)
    async def candidate_detail(
        request: Request,
        candidate_id: int,
    ) -> Response:
        candidate = ctx.repository.get_candidate(candidate_id)
        if candidate is None:
            return Response("Candidate not found", status_code=404)
        archive_exists = False
        archive_valid = False
        archive_reason = "Recovery archive has not been created"
        try:
            root = ctx.settings.media_root_by_id(candidate["root_id"])
            candidate_path = path_from_relative(root.path, candidate["relative_path"])
            archive_path = candidate_path.with_suffix(RECOVERY_ARCHIVE_SUFFIX)
            archive_exists = archive_path.exists()
            if archive_exists:
                archive_valid, archive_reason = validate_recovery_archive(archive_path)
        except (OSError, PathSafetyError):
            pass
        return ctx.render(
            request,
            "candidate_detail.html",
            candidate=candidate,
            inspection_history=ctx.repository.candidate_job_history(
                candidate_id,
                JobKind.INSPECT.value,
            ),
            archive_exists=archive_exists,
            archive_valid=archive_valid,
            archive_reason=archive_reason,
        )

    @app.post("/candidates/{candidate_id:int}/inspect")
    async def queue_inspect(
        request: Request,
        candidate_id: int,
        _: None = Depends(ctx.require_csrf),
    ) -> RedirectResponse:
        try:
            job_id = ctx.job_service.queue_inspect(candidate_id)
            ctx.worker.notify()
            return redirect_with_message(f"/jobs/{job_id}", "Inspection queued.")
        except (ValueError, OSError) as exc:
            return redirect_with_message(
                f"/candidates/{candidate_id}",
                str(exc),
                error=True,
            )

    @app.post("/candidates/{candidate_id:int}/recovery-backup")
    async def queue_recovery_backup(
        request: Request,
        candidate_id: int,
        _: None = Depends(ctx.require_csrf),
    ) -> RedirectResponse:
        try:
            job_id = ctx.job_service.queue_recovery_backup(candidate_id)
            ctx.worker.notify()
            return redirect_with_message(
                f"/jobs/{job_id}",
                "Recovery archive queued.",
            )
        except (ValueError, OSError) as exc:
            return redirect_with_message(
                f"/candidates/{candidate_id}",
                str(exc),
                error=True,
            )

    @app.get(
        "/candidates/{candidate_id:int}/convert",
        response_class=HTMLResponse,
    )
    async def conversion_confirmation(
        request: Request,
        candidate_id: int,
    ) -> Response:
        candidate = ctx.repository.get_candidate(candidate_id)
        if candidate is None or not candidate["active"]:
            return Response("Candidate not found", status_code=404)
        if candidate["category"] not in (
            CandidateCategory.MEL.value,
            CandidateCategory.SIMPLE_FEL.value,
        ):
            return Response("Candidate cannot be converted", status_code=400)
        runtime = RuntimeSettings.load(ctx.settings, ctx.repository)
        preflights = {
            mode.value: {"requirement": None, "error": None} for mode in BackupMode
        }
        try:
            for mode in BackupMode:
                try:
                    path, requirement = _conversion_preflight(ctx, candidate, mode)
                    preflights[mode.value]["requirement"] = requirement
                    require_storage(requirement, path.parent, ctx.settings.temp_dir)
                except (OSError, PathSafetyError) as exc:
                    preflights[mode.value]["error"] = str(exc)
        except (OSError, PathSafetyError) as exc:
            for preflight in preflights.values():
                preflight["error"] = str(exc)
        selected_preflight = preflights[runtime.backup_mode.value]
        return ctx.render(
            request,
            "conversion_confirm.html",
            candidate=candidate,
            storage_requirement=selected_preflight["requirement"],
            preflight_error=selected_preflight["error"],
            conversion_preflights=preflights,
            create_recovery_archive_default=(
                runtime.create_recovery_archive_on_convert
            ),
            backup_mode_default=runtime.backup_mode.value,
        )

    @app.post("/candidates/{candidate_id:int}/convert")
    async def queue_conversion(
        request: Request,
        candidate_id: int,
        approved: str | None = Form(default=None),
        backup_mode: str | None = Form(default=None),
        create_recovery_archive: str | None = Form(default=None),
        _: None = Depends(ctx.require_csrf),
    ) -> RedirectResponse:
        try:
            if approved == "yes":
                candidate = ctx.repository.get_candidate(candidate_id)
                if candidate is None or not candidate["active"]:
                    raise ValueError("candidate not found")
                try:
                    selected_mode = (
                        BackupMode(backup_mode)
                        if backup_mode is not None
                        else (
                            BackupMode.BOTH
                            if create_recovery_archive == "yes"
                            else BackupMode.FULL_ONLY
                        )
                    )
                except ValueError as exc:
                    raise ValueError("invalid backup mode") from exc
                _require_conversion_preflight(ctx, candidate, selected_mode)
            job_id = ctx.job_service.queue_conversion(
                candidate_id,
                approved=approved == "yes",
                approved_by=current_actor(request),
                backup_mode=backup_mode,
                create_recovery_archive=create_recovery_archive == "yes",
            )
            ctx.worker.notify()
            return redirect_with_message(f"/jobs/{job_id}", "Conversion queued.")
        except (ValueError, OSError) as exc:
            return redirect_with_message(
                f"/candidates/{candidate_id}/convert",
                str(exc),
                error=True,
            )

    @app.get("/candidates/bulk-mel/confirm", response_class=HTMLResponse)
    async def bulk_mel_confirmation(request: Request) -> HTMLResponse:
        runtime = RuntimeSettings.load(ctx.settings, ctx.repository)
        return ctx.render(
            request,
            "bulk_mel_confirm.html",
            candidates=ctx.repository.list_candidates(CandidateCategory.MEL.value),
            create_recovery_archive_default=(
                runtime.create_recovery_archive_on_convert
            ),
            backup_mode_default=runtime.backup_mode.value,
        )

    @app.post("/candidates/bulk-mel/convert")
    async def queue_bulk_mel(
        request: Request,
        approved: str | None = Form(default=None),
        backup_mode: str | None = Form(default=None),
        create_recovery_archive: str | None = Form(default=None),
        _: None = Depends(ctx.require_csrf),
    ) -> RedirectResponse:
        try:
            job_ids, skipped = ctx.job_service.queue_bulk_mel(
                approved=approved == "yes",
                approved_by=current_actor(request),
                backup_mode=backup_mode,
                create_recovery_archive=create_recovery_archive == "yes",
            )
            ctx.worker.notify()
            return redirect_with_message(
                "/jobs",
                (
                    f"Queued {len(job_ids)} safe MEL conversion job(s); "
                    f"skipped {skipped}."
                ),
            )
        except ValueError as exc:
            return redirect_with_message(
                "/candidates/bulk-mel/confirm",
                str(exc),
                error=True,
            )
