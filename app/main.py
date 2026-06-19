from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.automation import AutomationCoordinator
from app.backups import discover_all_backups, discover_all_recovery_archives
from app.config import Settings, load_settings, validate_media_roots
from app.db import initialize_database
from app.repository import Repository
from app.routes import backups, candidates, dashboard, jobs, scans, webhooks
from app.routes import settings as settings_routes
from app.security import load_or_create_csrf_secret
from app.services import JobService
from app.version import load_build_info
from app.web import APP_DIR, AppContext, install_security_middleware
from app.worker import JobWorker


def _asset_version(revision: str) -> str:
    if revision != "unknown":
        return revision[:12]
    static_mtime = max(
        int((APP_DIR / "static" / name).stat().st_mtime)
        for name in ("app.css", "operations.css", "app.js")
    )
    return str(static_mtime)


def create_app(
    settings: Settings | None = None,
    *,
    start_worker: bool = True,
    worker: JobWorker | None = None,
) -> FastAPI:
    app_settings = settings or load_settings()
    repository = Repository(app_settings.db_path)
    job_service = JobService(app_settings, repository)
    app_worker = worker or JobWorker(app_settings, repository)
    automation = AutomationCoordinator(
        app_settings,
        repository,
        job_service,
        app_worker,
    )
    build_info = load_build_info()
    ctx = AppContext(
        settings=app_settings,
        repository=repository,
        job_service=job_service,
        worker=app_worker,
        automation=automation,
        build_info=build_info,
        asset_version=_asset_version(build_info.revision),
        discover_backups=discover_all_backups,
        discover_recovery_archives=discover_all_recovery_archives,
        which=shutil.which,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        validate_media_roots(app_settings)
        initialize_database(app_settings.db_path)
        repository.sync_library_roots(app_settings.media_roots)
        repository.migrate_legacy_radarr_mapping()
        app.state.csrf_secret = load_or_create_csrf_secret(app_settings.config_dir)
        if start_worker:
            await app_worker.start()
            await automation.start()
        yield
        if start_worker:
            await automation.stop()
            await app_worker.stop()

    app = FastAPI(title="dovi-manager", lifespan=lifespan)
    app.state.settings = app_settings
    app.state.repository = repository
    app.state.job_service = job_service
    app.state.worker = app_worker
    app.state.automation = automation
    app.state.build_info = build_info
    app.state.context = ctx
    app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")

    install_security_middleware(app, ctx)
    dashboard.register(app, ctx)
    webhooks.register(app, ctx)
    scans.register(app, ctx)
    candidates.register(app, ctx)
    jobs.register(app, ctx)
    backups.register(app, ctx)
    settings_routes.register(app, ctx)
    return app


app = create_app()
