from __future__ import annotations

import json
import secrets

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.runtime_settings import RuntimeSettings
from app.web import AppContext
from app.webhooks import (
    map_external_path,
    radarr_event_paths,
    sonarr_event_paths,
)


def register(app: FastAPI, ctx: AppContext) -> None:
    def webhook_token_valid(token: str) -> bool:
        runtime = RuntimeSettings.load(ctx.settings, ctx.repository)
        expected = ctx.repository.get_setting("webhook_token", "")
        return (
            runtime.webhooks_enabled
            and bool(expected)
            and secrets.compare_digest(token, expected)
        )

    async def webhook_payload(request: Request) -> dict:
        body = await request.body()
        if len(body) > 64 * 1024:
            raise HTTPException(status_code=413, detail="Webhook payload is too large")
        try:
            payload = json.loads(body or b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=400,
                detail="Webhook payload must be valid JSON",
            ) from exc
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=400,
                detail="Webhook payload must be a JSON object",
            )
        return payload

    @app.post("/webhooks/radarr/{token}")
    async def radarr_webhook(request: Request, token: str) -> JSONResponse:
        if not webhook_token_valid(token):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        payload = await webhook_payload(request)
        event_type = str(payload.get("eventType", "")).casefold()
        if event_type == "test":
            return JSONResponse({"accepted": True, "test": True})
        if event_type not in {"download", "rename"}:
            return JSONResponse(
                {"accepted": False, "ignored": True, "event_type": event_type}
            )

        request_ids: list[int] = []
        mappings = ctx.repository.list_webhook_mappings("radarr")
        runtime = RuntimeSettings.load(ctx.settings, ctx.repository)
        if not mappings and runtime.radarr_root_prefix:
            mappings = [
                {
                    "integration": "radarr",
                    "external_prefix": runtime.radarr_root_prefix,
                    "root_id": "default",
                }
            ]
        mapped_paths = []
        for external_path in radarr_event_paths(payload):
            try:
                mapped_paths.append(
                    map_external_path(
                        ctx.settings,
                        mappings,
                        "radarr",
                        external_path,
                    )
                )
            except (OSError, ValueError):
                continue

        if event_type == "rename":
            for root_id in dict.fromkeys(item.root_id for item in mapped_paths):
                request_id, _ = ctx.repository.enqueue_scan_request(
                    "smart",
                    root_id=root_id,
                    relative_path=None,
                    trigger="radarr",
                )
                request_ids.append(request_id)
        else:
            for mapped in dict.fromkeys(mapped_paths):
                request_id, _ = ctx.repository.enqueue_scan_request(
                    "file",
                    root_id=mapped.root_id,
                    relative_path=mapped.relative_path,
                    trigger="radarr",
                )
                request_ids.append(request_id)

        fallback = not request_ids
        if fallback:
            request_id, _ = ctx.repository.enqueue_scan_request(
                "smart",
                relative_path=None,
                trigger="radarr",
            )
            request_ids.append(request_id)
        ctx.automation.notify()
        return JSONResponse(
            {
                "accepted": True,
                "request_ids": request_ids,
                "fallback": fallback,
            },
            status_code=202,
        )

    @app.post("/webhooks/sonarr/{token}")
    async def sonarr_webhook(request: Request, token: str) -> JSONResponse:
        if not webhook_token_valid(token):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        payload = await webhook_payload(request)
        event_type = str(payload.get("eventType", "")).casefold()
        if event_type == "test":
            return JSONResponse({"accepted": True, "test": True})
        if event_type not in {"download", "rename"}:
            return JSONResponse(
                {"accepted": False, "ignored": True, "event_type": event_type}
            )

        mappings = ctx.repository.list_webhook_mappings("sonarr")
        mapped_paths = []
        for external_path in sonarr_event_paths(payload):
            try:
                mapped_paths.append(
                    map_external_path(
                        ctx.settings,
                        mappings,
                        "sonarr",
                        external_path,
                    )
                )
            except (OSError, ValueError):
                continue

        request_ids: list[int] = []
        if event_type == "rename":
            for root_id in dict.fromkeys(item.root_id for item in mapped_paths):
                request_id, _ = ctx.repository.enqueue_scan_request(
                    "smart",
                    root_id=root_id,
                    relative_path=None,
                    trigger="sonarr",
                )
                request_ids.append(request_id)
        else:
            for mapped in dict.fromkeys(mapped_paths):
                request_id, _ = ctx.repository.enqueue_scan_request(
                    "file",
                    root_id=mapped.root_id,
                    relative_path=mapped.relative_path,
                    trigger="sonarr",
                )
                request_ids.append(request_id)

        fallback = not request_ids
        if fallback:
            request_id, _ = ctx.repository.enqueue_scan_request(
                "smart",
                relative_path=None,
                trigger="sonarr",
            )
            request_ids.append(request_id)
        ctx.automation.notify()
        return JSONResponse(
            {
                "accepted": True,
                "request_ids": request_ids,
                "fallback": fallback,
            },
            status_code=202,
        )
