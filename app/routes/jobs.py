from __future__ import annotations

import json
import math

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.models import CandidateCategory, JobKind, JobState, ScanMode
from app.web import AppContext, PAGE_SIZE, job_payload, redirect_with_message


def register(app: FastAPI, ctx: AppContext) -> None:
    @app.get("/jobs", response_class=HTMLResponse)
    async def jobs_page(
        request: Request,
        state: str | None = None,
        kind: str | None = None,
        category: str | None = None,
        root_id: str | None = None,
        origin: str | None = None,
        q: str = "",
        page: int = 1,
    ) -> HTMLResponse:
        valid_states = {item.value for item in JobState}
        valid_kinds = {item.value for item in JobKind}
        state = state if state in valid_states else None
        kind = kind if kind in valid_kinds else None
        category = (
            category if category in {item.value for item in CandidateCategory} else None
        )
        root_id = (
            root_id
            if root_id in {root.id for root in ctx.settings.media_roots}
            else None
        )
        origin = origin if origin in {"manual", "automatic"} else None
        page = max(1, page)
        q = q.strip()
        total = ctx.repository.count_jobs(
            state,
            kind,
            search=q or None,
            category=category,
            root_id=root_id,
            origin=origin,
        )
        page_count = max(1, math.ceil(total / PAGE_SIZE))
        page = min(page, page_count)
        return ctx.render(
            request,
            "jobs.html",
            jobs=ctx.repository.list_jobs(
                state,
                kind,
                limit=PAGE_SIZE,
                offset=(page - 1) * PAGE_SIZE,
                search=q or None,
                category=category,
                root_id=root_id,
                origin=origin,
            ),
            selected_state=state,
            selected_kind=kind,
            selected_category=category,
            selected_root=root_id,
            selected_origin=origin,
            media_roots=ctx.settings.media_roots,
            search=q,
            page=page,
            page_count=page_count,
            total=total,
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_detail(request: Request, job_id: int) -> Response:
        job = ctx.repository.get_job(job_id)
        if job is None:
            return Response("Job not found", status_code=404)
        payload = job_payload(job["payload_json"])
        scan_candidate = None
        if (
            job["kind"] == JobKind.SCAN.value
            and payload.get("scan_mode") == ScanMode.FILE.value
            and payload.get("target")
        ):
            scan_candidate = ctx.repository.get_candidate_by_path(
                payload["target"],
                str(payload.get("root_id") or "default"),
            )
        return ctx.render(
            request,
            "job_detail.html",
            job=job,
            command=json.loads(job["command_json"]) if job["command_json"] else None,
            payload=payload,
            scan_candidate=scan_candidate,
        )

    @app.get("/jobs/{job_id}/status")
    async def job_status(job_id: int) -> JSONResponse:
        job = ctx.repository.get_job(job_id)
        if job is None:
            return JSONResponse({"error": "Job not found"}, status_code=404)
        return JSONResponse(
            {
                "id": job["id"],
                "state": job["state"],
                "started_at": job["started_at"],
                "finished_at": job["finished_at"],
                "exit_code": job["exit_code"],
                "error": job["error"],
                "log_text": job["log_text"],
                "log_truncated": bool(job["log_truncated"]),
                "active": job["state"] in {"queued", "running"},
            }
        )

    @app.post("/jobs/{job_id}/cancel")
    async def cancel_job(
        job_id: int,
        _: None = Depends(ctx.require_csrf),
    ) -> RedirectResponse:
        if ctx.repository.cancel_queued_job(job_id):
            return redirect_with_message(f"/jobs/{job_id}", "Queued job cancelled.")
        return redirect_with_message(
            f"/jobs/{job_id}",
            "Only queued jobs can be cancelled.",
            error=True,
        )
