from __future__ import annotations

import asyncio
import math
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.file_browser import browse_media_root
from app.models import JobKind, ScanMode
from app.runtime_settings import RuntimeSettings
from app.safety import path_from_relative, relative_media_path, validate_media_file
from app.web import AppContext, PAGE_SIZE, redirect_with_message


def register(app: FastAPI, ctx: AppContext) -> None:
    def scan_redirect(job_id: int, label: str) -> RedirectResponse:
        ctx.worker.notify()
        return redirect_with_message(f"/jobs/{job_id}", f"{label} queued.")

    @app.get("/scans", response_class=HTMLResponse)
    async def scans_page(
        request: Request,
        selected_root: str = "",
        selected_path: str = "",
    ) -> HTMLResponse:
        runtime = RuntimeSettings.load(ctx.settings, ctx.repository)
        selected_file_root = None
        if selected_root and selected_path:
            try:
                root = ctx.settings.media_root_by_id(selected_root)
                selected_file = validate_media_file(
                    root.path,
                    path_from_relative(root.path, selected_path),
                )
                selected_file_root = root
                selected_path = relative_media_path(root.path, selected_file)
            except (OSError, ValueError):
                selected_path = ""
        return ctx.render(
            request,
            "scans.html",
            runtime=runtime,
            inventory_count=ctx.repository.inventory_count(),
            pending_requests=ctx.repository.pending_scan_request_count(),
            recent_scans=ctx.repository.list_jobs(kind=JobKind.SCAN.value, limit=8),
            recent_inspections=ctx.repository.list_jobs(
                kind=JobKind.INSPECT.value,
                limit=8,
            ),
            next_schedule=ctx.automation.next_schedule_at(),
            media_roots=ctx.settings.media_roots,
            selected_file_root=selected_file_root,
            selected_file_path=selected_path,
        )

    @app.get("/scans/browse", response_class=HTMLResponse)
    async def browse_scan_files(
        request: Request,
        root_id: str = "default",
        path: str = "",
        q: str = "",
        page: int = 1,
        fragment: bool = False,
    ) -> Response:
        try:
            root = ctx.settings.media_root_by_id(root_id)
            all_entries = await asyncio.to_thread(
                browse_media_root,
                root.path,
                path,
                search=q,
                limit=500,
            )
        except (OSError, ValueError) as exc:
            return redirect_with_message("/scans", str(exc), error=True)
        page = max(1, page)
        page_count = max(1, math.ceil(len(all_entries) / PAGE_SIZE))
        page = min(page, page_count)
        current = Path(path)
        parent = (
            None
            if not path
            else ("" if current.parent == Path(".") else current.parent.as_posix())
        )
        return ctx.render(
            request,
            "_file_browser_contents.html" if fragment else "file_browser.html",
            media_roots=ctx.settings.media_roots,
            selected_root=root,
            current_path=path,
            parent_path=parent,
            search=q.strip(),
            entries=all_entries[(page - 1) * PAGE_SIZE : page * PAGE_SIZE],
            page=page,
            page_count=page_count,
            truncated=len(all_entries) == 500,
            result_limit=500,
            fragment=fragment,
        )

    @app.post("/scan")
    @app.post("/scans/full")
    async def queue_full_scan(
        root_id: str = Form(default=""),
        _: None = Depends(ctx.require_csrf),
    ) -> RedirectResponse:
        try:
            job_id = ctx.job_service.queue_scan(
                mode=ScanMode.FULL,
                root_id=root_id or None,
            )
            return scan_redirect(job_id, "Full scan")
        except ValueError as exc:
            return redirect_with_message("/scans", str(exc), error=True)

    @app.post("/scans/smart")
    async def queue_smart_scan(
        root_id: str = Form(default=""),
        _: None = Depends(ctx.require_csrf),
    ) -> RedirectResponse:
        try:
            job_id = ctx.job_service.queue_scan(
                mode=ScanMode.SMART,
                root_id=root_id or None,
            )
            return scan_redirect(job_id, "Smart scan")
        except ValueError as exc:
            return redirect_with_message("/scans", str(exc), error=True)

    @app.post("/scans/custom")
    async def queue_custom_scan(
        target: str = Form(default=""),
        recursive: str | None = Form(default=None),
        depth: int = Form(default=5),
        debug: str | None = Form(default=None),
        root_id: str = Form(default="default"),
        _: None = Depends(ctx.require_csrf),
    ) -> RedirectResponse:
        try:
            job_id = ctx.job_service.queue_scan(
                mode=ScanMode.CUSTOM,
                target=target.strip(),
                root_id=root_id,
                recursive=recursive == "yes",
                depth=depth,
                debug=debug == "yes",
            )
            return scan_redirect(job_id, "Custom scan")
        except (OSError, ValueError) as exc:
            return redirect_with_message("/scans", str(exc), error=True)

    @app.post("/scans/file")
    async def queue_file_scan(
        target: str = Form(),
        debug: str | None = Form(default=None),
        root_id: str = Form(default="default"),
        _: None = Depends(ctx.require_csrf),
    ) -> RedirectResponse:
        try:
            job_id = ctx.job_service.queue_scan(
                mode=ScanMode.FILE,
                target=target.strip(),
                root_id=root_id,
                debug=debug == "yes",
            )
            return scan_redirect(job_id, "File scan")
        except (OSError, ValueError) as exc:
            return redirect_with_message("/scans", str(exc), error=True)
