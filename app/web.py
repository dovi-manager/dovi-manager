from __future__ import annotations

import asyncio
import base64
import binascii
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.automation import AutomationCoordinator
from app.config import Settings
from app.models import BackupFile, CandidateCategory, JobKind, JobState, RecoveryArchive
from app.readiness import check_readiness
from app.repository import Repository
from app.security import csrf_token, verify_csrf_token
from app.services import JobService
from app.version import BuildInfo
from app.worker import JobWorker


APP_DIR = Path(__file__).parent
PAGE_SIZE = 50
BackupDiscovery = Callable[[Any, int], list[BackupFile]]
RecoveryArchiveDiscovery = Callable[[Any], list[RecoveryArchive]]

templates = Jinja2Templates(directory=APP_DIR / "templates")
templates.env.globals["candidate_categories"] = list(CandidateCategory)
templates.env.globals["job_states"] = list(JobState)
templates.env.globals["job_kinds"] = list(JobKind)


def human_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def path_name(value: str) -> str:
    return value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def path_parent(value: str) -> str:
    normalized = value.replace("\\", "/").rstrip("/")
    if "/" not in normalized:
        return "Media root"
    return normalized.rsplit("/", 1)[0]


def job_payload(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


templates.env.filters["human_size"] = human_size
templates.env.filters["path_name"] = path_name
templates.env.filters["path_parent"] = path_parent
templates.env.filters["job_payload"] = job_payload


def redirect_with_message(
    path: str,
    message: str,
    *,
    error: bool = False,
) -> RedirectResponse:
    fragment = ""
    if "#" in path:
        path, fragment = path.split("#", 1)
        fragment = f"#{fragment}"
    separator = "&" if "?" in path else "?"
    key = "error" if error else "message"
    return RedirectResponse(
        f"{path}{separator}{key}={quote(message)}{fragment}",
        status_code=303,
    )


def current_actor(request: Request) -> str:
    return getattr(request.state, "auth_user", "local")


def add_security_headers(response: Response) -> Response:
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self'; script-src 'self'; "
        "img-src 'self'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


@dataclass
class AppContext:
    settings: Settings
    repository: Repository
    job_service: JobService
    worker: JobWorker
    automation: AutomationCoordinator
    build_info: BuildInfo
    asset_version: str
    discover_backups: BackupDiscovery
    discover_recovery_archives: RecoveryArchiveDiscovery
    which: Callable[[str], str | None]
    backup_summary_cache: dict[str, float | int] = field(
        default_factory=lambda: {"expires_at": 0.0, "count": 0, "size": 0}
    )
    backup_cache_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def readiness_checks(self) -> dict[str, bool]:
        return check_readiness(
            self.settings,
            worker_running=self.worker.running,
            which=self.which,
        )

    async def require_csrf(
        self,
        request: Request,
        csrf_token_value: str = Form(default="", alias="csrf_token"),
    ) -> None:
        secret = getattr(request.app.state, "csrf_secret", None)
        if secret is None or not verify_csrf_token(
            secret,
            current_actor(request),
            csrf_token_value,
        ):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")

    def render(self, request: Request, name: str, **context: Any) -> Response:
        secret = getattr(request.app.state, "csrf_secret", None)
        checks = self.readiness_checks()
        return templates.TemplateResponse(
            request=request,
            name=name,
            context={
                "settings": self.settings,
                "auth_enabled": self.settings.auth_username is not None,
                "build_info": self.build_info,
                "asset_version": self.asset_version,
                "shell_ready": all(checks.values()),
                "message": request.query_params.get("message"),
                "error": request.query_params.get("error"),
                "worker": self.worker,
                "csrf_token": (
                    csrf_token(secret, current_actor(request)) if secret else ""
                ),
                **context,
            },
        )


def install_security_middleware(app: FastAPI, ctx: AppContext) -> None:
    @app.middleware("http")
    async def basic_auth(request: Request, call_next):
        fetch_site = request.headers.get("Sec-Fetch-Site", "")
        webhook_request = request.url.path.startswith("/webhooks/")
        if (
            request.method == "POST"
            and fetch_site == "cross-site"
            and not webhook_request
        ):
            return add_security_headers(
                Response("Cross-site form submission rejected", status_code=403)
            )

        if (
            request.url.path in {"/healthz", "/readyz", "/versionz"}
            or webhook_request
            or ctx.settings.auth_username is None
        ):
            request.state.auth_user = "local"
            return add_security_headers(await call_next(request))

        authorization = request.headers.get("Authorization", "")
        username = password = ""
        if authorization.startswith("Basic "):
            try:
                decoded = base64.b64decode(
                    authorization[6:],
                    validate=True,
                ).decode("utf-8")
                username, password = decoded.split(":", 1)
            except (ValueError, UnicodeDecodeError, binascii.Error):
                pass

        username_ok = secrets.compare_digest(username, ctx.settings.auth_username)
        password_ok = secrets.compare_digest(password, ctx.settings.auth_password or "")
        if not (username_ok and password_ok):
            return add_security_headers(
                Response(
                    "Authentication required",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="dovi-manager"'},
                )
            )
        request.state.auth_user = username
        return add_security_headers(await call_next(request))
