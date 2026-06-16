from __future__ import annotations

import math

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.models import CandidateCategory, JobKind
from app.safety import (
    PathSafetyError,
    conversion_storage_requirement,
    path_from_relative,
    require_conversion_storage,
    require_directory_writable,
)
from app.web import (
    AppContext,
    PAGE_SIZE,
    current_actor,
    redirect_with_message,
)


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
        return ctx.render(
            request,
            "candidate_detail.html",
            candidate=candidate,
            inspection_history=ctx.repository.candidate_job_history(
                candidate_id,
                JobKind.INSPECT.value,
            ),
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
        reserve_bytes = ctx.settings.disk_reserve_gib * 1024**3
        requirement = None
        preflight_error = None
        try:
            root = ctx.settings.media_root_by_id(candidate["root_id"])
            path = path_from_relative(root.path, candidate["relative_path"])
            requirement = conversion_storage_requirement(
                path,
                ctx.settings.temp_dir,
                int(candidate["file_size"]),
                reserve_bytes,
            )
            require_directory_writable(path.parent)
            require_directory_writable(ctx.settings.temp_dir)
            require_conversion_storage(
                path,
                ctx.settings.temp_dir,
                int(candidate["file_size"]),
                reserve_bytes,
            )
        except (OSError, PathSafetyError) as exc:
            preflight_error = str(exc)
        return ctx.render(
            request,
            "conversion_confirm.html",
            candidate=candidate,
            storage_requirement=requirement,
            preflight_error=preflight_error,
        )

    @app.post("/candidates/{candidate_id:int}/convert")
    async def queue_conversion(
        request: Request,
        candidate_id: int,
        approved: str | None = Form(default=None),
        _: None = Depends(ctx.require_csrf),
    ) -> RedirectResponse:
        try:
            job_id = ctx.job_service.queue_conversion(
                candidate_id,
                approved=approved == "yes",
                approved_by=current_actor(request),
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
        return ctx.render(
            request,
            "bulk_mel_confirm.html",
            candidates=ctx.repository.list_candidates(CandidateCategory.MEL.value),
        )

    @app.post("/candidates/bulk-mel/convert")
    async def queue_bulk_mel(
        request: Request,
        approved: str | None = Form(default=None),
        _: None = Depends(ctx.require_csrf),
    ) -> RedirectResponse:
        try:
            job_ids, skipped = ctx.job_service.queue_bulk_mel(
                approved=approved == "yes",
                approved_by=current_actor(request),
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
