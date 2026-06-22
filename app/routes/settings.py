from __future__ import annotations

import shutil

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.automation import ensure_webhook_token, regenerate_webhook_token
from app.db import SCHEMA_VERSION
from app.runtime_settings import (
    MAX_SCHEDULE_MINUTES,
    SCHEDULE_UNIT_MINUTES,
    RuntimeSettings,
)
from app.web import AppContext, redirect_with_message
from app.webhooks import normalize_external_prefix


def register(app: FastAPI, ctx: AppContext) -> None:
    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request) -> HTMLResponse:
        runtime = RuntimeSettings.load(ctx.settings, ctx.repository)
        webhook_token = (
            ensure_webhook_token(ctx.repository) if runtime.webhooks_enabled else ""
        )
        tools = {
            name: ctx.which(name)
            for name in (
                ctx.settings.dovi_convert_path,
                "dovi_tool",
                "ffmpeg",
                "mkvmerge",
                "mkvextract",
                "mediainfo",
            )
        }
        disk_status = {}
        storage_paths = [
            (f"media: {root.label}", root.path) for root in ctx.settings.media_roots
        ]
        storage_paths.extend(
            [
                ("temp", ctx.settings.temp_dir),
                ("config", ctx.settings.config_dir),
            ]
        )
        for name, path in storage_paths:
            try:
                usage = shutil.disk_usage(path)
                disk_status[name] = {
                    "path": path,
                    "free": usage.free,
                    "total": usage.total,
                    "used_percent": (
                        round((usage.total - usage.free) / usage.total * 100)
                        if usage.total
                        else 0
                    ),
                }
            except OSError:
                disk_status[name] = {
                    "path": path,
                    "free": None,
                    "total": None,
                    "used_percent": None,
                }

        return ctx.render(
            request,
            "settings.html",
            runtime=runtime,
            webhook_token=webhook_token,
            webhook_base_url=str(request.base_url).rstrip("/"),
            next_schedule=ctx.automation.next_schedule_at(),
            inventory_count=ctx.repository.inventory_count(),
            media_roots=ctx.settings.media_roots,
            webhook_mappings=ctx.repository.list_webhook_mappings(),
            tools=tools,
            disk_status=disk_status,
            schema_version=str(SCHEMA_VERSION),
            dovi_version="8.2.0",
            build_info=ctx.build_info,
            readiness_checks=ctx.readiness_checks(),
        )

    @app.post("/settings")
    async def update_settings(
        retention_days: int = Form(),
        scan_depth: int | None = Form(default=None),
        scan_debug: str | None = Form(default=None),
        convert_safe_mode: str | None = Form(default=None),
        convert_verbose: str | None = Form(default=None),
        create_recovery_archive_on_convert: str | None = Form(default=None),
        compact_only_after_convert: str | None = Form(default=None),
        compact_only_acknowledged: str | None = Form(default=None),
        schedule_enabled: str | None = Form(default=None),
        schedule_interval_minutes: int | None = Form(default=None),
        schedule_interval_value: int | None = Form(default=None),
        schedule_interval_unit: str = Form(default="minutes"),
        schedule_start_time: str = Form(default="03:00"),
        webhooks_enabled: str | None = Form(default=None),
        radarr_root_prefix: str = Form(default=""),
        settings_action: str = Form(default="save"),
        mapping_integration: list[str] = Form(default=[]),
        mapping_prefix: list[str] = Form(default=[]),
        mapping_root_id: list[str] = Form(default=[]),
        auto_queue_mel: str | None = Form(default=None),
        auto_convert_mel_after_inspect: str | None = Form(default=None),
        auto_inspect_mel: str | None = Form(default=None),
        auto_inspect_simple_fel: str | None = Form(default=None),
        auto_inspect_complex_fel: str | None = Form(default=None),
        allow_backup_retention_override: str | None = Form(default=None),
        automation_acknowledged: str | None = Form(default=None),
        _: None = Depends(ctx.require_csrf),
    ) -> RedirectResponse:
        current = RuntimeSettings.load(ctx.settings, ctx.repository)
        scan_depth = current.scan_depth if scan_depth is None else scan_depth
        if schedule_interval_value is None:
            schedule_interval_value = (
                schedule_interval_minutes
                if schedule_interval_minutes is not None
                else current.schedule_interval_value
            )
            if schedule_interval_minutes is not None:
                schedule_interval_unit = "minutes"
        if not 1 <= retention_days <= 3650:
            return redirect_with_message(
                "/settings",
                "Retention must be between 1 and 3650 days.",
                error=True,
            )
        if not 1 <= scan_depth <= 20:
            return redirect_with_message(
                "/settings",
                "Scan depth must be between 1 and 20.",
                error=True,
            )
        if schedule_interval_unit not in SCHEDULE_UNIT_MINUTES:
            return redirect_with_message(
                "/settings",
                "Schedule interval unit is invalid.",
                error=True,
            )
        normalized_schedule_minutes = (
            schedule_interval_value * SCHEDULE_UNIT_MINUTES[schedule_interval_unit]
        )
        if not 5 <= normalized_schedule_minutes <= MAX_SCHEDULE_MINUTES:
            return redirect_with_message(
                "/settings",
                "Schedule interval must be between 5 minutes and 52 weeks.",
                error=True,
            )
        try:
            schedule_hour, schedule_minute = (
                int(part) for part in schedule_start_time.split(":", 1)
            )
        except ValueError:
            return redirect_with_message(
                "/settings",
                "Schedule start time is invalid.",
                error=True,
            )
        if not (0 <= schedule_hour <= 23 and 0 <= schedule_minute <= 59):
            return redirect_with_message(
                "/settings",
                "Schedule start time must be a valid hour and minute.",
                error=True,
            )
        normalized_schedule_start = f"{schedule_hour:02d}:{schedule_minute:02d}"
        enable_automation = auto_queue_mel == "yes"
        enable_compact = create_recovery_archive_on_convert == "yes"
        enable_compact_only = enable_compact and compact_only_after_convert == "yes"
        if (
            enable_compact_only
            and not current.compact_only_after_convert
            and compact_only_acknowledged != "yes"
        ):
            return redirect_with_message(
                "/settings#backup-retention",
                "Acknowledge that compact-only mode removes full originals.",
                error=True,
            )
        require_mel_inspection = (
            enable_automation and auto_convert_mel_after_inspect == "yes"
        )
        if (
            enable_automation
            and not current.auto_queue_mel
            and automation_acknowledged != "yes"
        ):
            return redirect_with_message(
                "/settings",
                "Acknowledge the automation warning before enabling it.",
                error=True,
            )
        enable_schedule = schedule_enabled == "yes"
        generate_webhooks = settings_action == "enable_webhooks"
        enable_webhooks = webhooks_enabled == "yes" or generate_webhooks
        ctx.repository.set_settings(
            {
                "retention_days": str(retention_days),
                "scan_depth": str(scan_depth),
                "scan_debug": "true" if scan_debug == "yes" else "false",
                "convert_safe_mode": (
                    "true" if convert_safe_mode == "yes" else "false"
                ),
                "convert_verbose": ("true" if convert_verbose == "yes" else "false"),
                "create_recovery_archive_on_convert": (
                    "true" if enable_compact else "false"
                ),
                "compact_only_after_convert": (
                    "true" if enable_compact_only else "false"
                ),
                "schedule_enabled": "true" if enable_schedule else "false",
                "schedule_interval_value": str(schedule_interval_value),
                "schedule_interval_unit": schedule_interval_unit,
                "schedule_interval_minutes": str(normalized_schedule_minutes),
                "schedule_start_time": normalized_schedule_start,
                "webhooks_enabled": "true" if enable_webhooks else "false",
                "radarr_root_prefix": radarr_root_prefix.strip(),
                "auto_queue_mel": "true" if enable_automation else "false",
                "auto_convert_mel_after_inspect": (
                    "true" if require_mel_inspection else "false"
                ),
                "auto_inspect_mel": (
                    "true"
                    if require_mel_inspection or auto_inspect_mel == "yes"
                    else "false"
                ),
                "auto_inspect_simple_fel": (
                    "true" if auto_inspect_simple_fel == "yes" else "false"
                ),
                "auto_inspect_complex_fel": (
                    "true" if auto_inspect_complex_fel == "yes" else "false"
                ),
                "allow_backup_retention_override": (
                    "true" if allow_backup_retention_override == "yes" else "false"
                ),
            }
        )
        submitted_mappings: list[tuple[str, str, str]] = []
        for integration, prefix, root_id in zip(
            mapping_integration,
            mapping_prefix,
            mapping_root_id,
            strict=False,
        ):
            if not prefix.strip():
                continue
            if integration not in {"radarr", "sonarr"}:
                return redirect_with_message(
                    "/settings",
                    "Webhook integration is invalid.",
                    error=True,
                )
            try:
                ctx.settings.media_root_by_id(root_id)
                normalized_prefix = normalize_external_prefix(prefix)
            except ValueError as exc:
                return redirect_with_message(
                    "/settings",
                    str(exc),
                    error=True,
                )
            submitted_mappings.append((integration, normalized_prefix, root_id))
        if mapping_integration:
            ctx.repository.replace_webhook_mappings(submitted_mappings)
        elif radarr_root_prefix.strip():
            ctx.repository.replace_webhook_mappings(
                [
                    (
                        "radarr",
                        normalize_external_prefix(radarr_root_prefix),
                        "default",
                    )
                ]
            )
        if enable_webhooks:
            ensure_webhook_token(ctx.repository)
        ctx.automation.notify()
        if generate_webhooks:
            return redirect_with_message(
                "/settings#webhooks",
                "Webhook URLs generated.",
            )
        return redirect_with_message("/settings", "Settings saved.")

    @app.get("/settings/webhook-token/regenerate/confirm", response_class=HTMLResponse)
    async def regenerate_token_confirmation(request: Request):
        runtime = RuntimeSettings.load(ctx.settings, ctx.repository)
        if not runtime.webhooks_enabled:
            return redirect_with_message(
                "/settings",
                "Enable webhook endpoints before rotating the token.",
                error=True,
            )
        ensure_webhook_token(ctx.repository)
        return ctx.render(request, "webhook_token_confirm.html")

    @app.post("/settings/webhook-token/regenerate")
    async def regenerate_token(
        confirmed: str | None = Form(default=None),
        _: None = Depends(ctx.require_csrf),
    ) -> RedirectResponse:
        runtime = RuntimeSettings.load(ctx.settings, ctx.repository)
        if not runtime.webhooks_enabled:
            return redirect_with_message(
                "/settings",
                "Enable webhook endpoints before rotating the token.",
                error=True,
            )
        if confirmed != "yes":
            return redirect_with_message(
                "/settings/webhook-token/regenerate/confirm",
                "Confirm webhook token regeneration before continuing.",
                error=True,
            )
        regenerate_webhook_token(ctx.repository)
        return redirect_with_message(
            "/settings#webhooks",
            "Webhook token regenerated. Update connected applications.",
        )
