import sqlite3
import base64
import io
import os
import re
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from starlette.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import (
    CandidateCategory,
    FileFingerprint,
    JobKind,
    JobState,
    ScanCandidate,
)
from app.security import csrf_token


MUTATION_REQUESTS = (
    ("/scan", {}),
    ("/scans/full", {}),
    ("/scans/smart", {}),
    ("/scans/custom", {"target": "", "depth": "5"}),
    ("/scans/file", {"target": "missing.mkv"}),
    ("/candidates/1/inspect", {}),
    ("/candidates/1/recovery-backup", {}),
    ("/candidates/1/convert", {}),
    ("/candidates/bulk-mel/convert", {}),
    ("/jobs/1/cancel", {}),
    ("/backups/confirm", {}),
    ("/backups/delete", {}),
    ("/backups/restore", {}),
    ("/settings", {"retention_days": "30"}),
    ("/settings/webhook-token/regenerate", {}),
)


def make_settings(tmp_path: Path) -> Settings:
    config_dir = tmp_path / "config"
    media_root = tmp_path / "media"
    shows_root = tmp_path / "shows"
    temp_dir = tmp_path / "cache"
    media_root.mkdir()
    shows_root.mkdir()
    temp_dir.mkdir()
    config_dir.mkdir()
    return Settings(
        media_root=media_root,
        shows_root=shows_root,
        temp_dir=temp_dir,
        config_dir=config_dir,
        db_path=config_dir / "dovi-manager.db",
    )


def csrf_data(app, **values) -> dict[str, str]:
    return {
        **values,
        "csrf_token": csrf_token(app.state.csrf_secret, "local"),
    }


def write_compact_archive(path: Path) -> None:
    payload = b"enhancement-layer"
    with tarfile.open(path, "w") as archive:
        member = tarfile.TarInfo("el.hevc")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))


def test_healthz(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path), start_worker=False)

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_versionz_is_public(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DOVI_MANAGER_VERSION", "edge")
    monkeypatch.setenv("DOVI_MANAGER_REVISION", "abc123")
    monkeypatch.setenv("DOVI_MANAGER_BUILD_DATE", "2026-06-14T17:00:00Z")
    settings = make_settings(tmp_path)
    settings = Settings(
        **{
            **settings.__dict__,
            "auth_username": "admin",
            "auth_password": "secret",
        }
    )
    app = create_app(settings, start_worker=False)

    with TestClient(app) as client:
        response = client.get("/versionz")

    assert response.status_code == 200
    assert response.json() == {
        "version": "edge",
        "revision": "abc123",
        "build_date": "2026-06-14T17:00:00Z",
    }


def test_startup_initializes_database(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings, start_worker=False)

    with TestClient(app):
        pass

    assert settings.db_path.is_file()
    with sqlite3.connect(settings.db_path) as connection:
        row = connection.execute(
            "SELECT value FROM app_metadata WHERE key = 'schema_version'"
        ).fetchone()

    assert row == ("6",)


def test_dashboard_renders_configuration(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings, start_worker=False)

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "dovi-manager" in response.text
    assert "Command center" in response.text
    assert "Run smart scan" in response.text
    assert 'rel="icon"' in response.text
    assert "/static/app.css?v=" in response.text
    assert "/static/operations.css?v=" in response.text


def test_dashboard_integrates_recent_conversion_labels(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    movie = settings.media_root / "Movie.mkv"
    movie.write_bytes(b"movie")
    stat = movie.stat()
    app = create_app(settings, start_worker=False)

    with TestClient(app) as client:
        repository = app.state.repository
        scan_id = repository.create_job(JobKind.SCAN)
        repository.claim_next_job()
        repository.finish_job(scan_id, JobState.SUCCEEDED)
        repository.replace_candidate_snapshot(
            [
                ScanCandidate(
                    relative_path=movie.name,
                    category=CandidateCategory.MEL,
                    status_text="safe",
                    action_text="CONVERT",
                    fingerprint=FileFingerprint(stat.st_size, stat.st_mtime_ns),
                )
            ],
            scan_id,
        )
        candidate_id = repository.list_candidates()[0]["id"]
        repository.create_job(
            JobKind.CONVERT,
            candidate_id=candidate_id,
            payload={
                "candidate_category": "mel",
                "queue_origin": "automatic",
            },
            approved_by="local:auto",
        )
        response = client.get("/")

    assert "Recent conversions" in response.text
    assert "All-time conversion outcomes" not in response.text
    assert "Latest jobs" not in response.text
    assert "Movie.mkv" in response.text
    assert "MEL" in response.text
    assert "Automatic" in response.text


def test_status_summary_reports_counts_and_activity(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path), start_worker=False)

    with TestClient(app) as client:
        idle = client.get("/status/summary")
        app.state.repository.create_job(JobKind.SCAN)
        active = client.get("/status/summary")

    assert idle.status_code == 200
    assert idle.json()["active"] is False
    assert idle.json()["candidates"] == {
        "mel": 0,
        "simple_fel": 0,
        "complex_fel": 0,
        "scan_error": 0,
    }
    assert idle.json()["backups"] == {"count": 0, "size": 0}
    assert active.json()["active"] is True
    assert active.json()["jobs"]["queued"] == 1
    assert active.json()["last_scan"]["state"] == "queued"


def test_status_summary_requires_authentication(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings = Settings(
        **{
            **settings.__dict__,
            "auth_username": "admin",
            "auth_password": "secret",
        }
    )
    app = create_app(settings, start_worker=False)
    token = base64.b64encode(b"admin:secret").decode()

    with TestClient(app) as client:
        rejected = client.get("/status/summary")
        accepted = client.get(
            "/status/summary",
            headers={"Authorization": f"Basic {token}"},
        )

    assert rejected.status_code == 401
    assert accepted.status_code == 200


def test_scan_post_queues_job_and_redirects(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path), start_worker=False)

    with TestClient(app) as client:
        response = client.post(
            "/scan",
            data=csrf_data(app),
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/jobs/1?")


def test_live_file_browser_lists_only_safe_mkvs(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    folder = settings.media_root / "Series"
    folder.mkdir()
    (folder / "Episode.mkv").write_bytes(b"episode")
    (folder / "Notes.txt").write_text("ignore", encoding="utf-8")
    (folder / "Episode.mkv.bak.dovi_convert").write_bytes(b"backup")
    app = create_app(settings, start_worker=False)

    with TestClient(app) as client:
        response = client.get("/scans/browse?path=Series")

    assert response.status_code == 200
    assert "Episode.mkv" in response.text
    assert "Notes.txt" not in response.text
    assert ".bak.dovi_convert" not in response.text
    assert 'data-root-id="default"' in response.text
    assert "selected_root=default" in response.text


def test_file_browser_fragment_and_standalone_selection(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    movie = settings.media_root / "Movie.mkv"
    movie.write_bytes(b"movie")
    app = create_app(settings, start_worker=False)

    with TestClient(app) as client:
        fragment = client.get("/scans/browse?fragment=1")
        selected = client.get("/scans?selected_root=default&selected_path=Movie.mkv")

    assert fragment.status_code == 200
    assert "data-file-browser-fragment" in fragment.text
    assert "<!doctype html>" not in fragment.text
    assert 'data-relative-path="Movie.mkv"' in fragment.text
    assert 'value="Movie.mkv"' in selected.text
    assert "Run file scan" in selected.text
    assert "disabled data-file-scan-submit" not in selected.text


def test_live_file_browser_rejects_path_escape(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path), start_worker=False)

    with TestClient(app) as client:
        response = client.get(
            "/scans/browse?path=../outside",
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/scans?")


def test_generic_webhook_endpoint_is_not_available(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings, start_worker=False)

    with TestClient(app) as client:
        app.state.repository.set_settings(
            {
                "webhooks_enabled": "true",
                "webhook_token": "secret-token",
            }
        )
        response = client.post("/webhooks/scan/secret-token", json={})

    assert response.status_code == 404


def test_webhook_payload_limit_is_enforced(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings, start_worker=False)

    with TestClient(app) as client:
        app.state.repository.set_settings(
            {
                "webhooks_enabled": "true",
                "webhook_token": "secret-token",
            }
        )
        response = client.post(
            "/webhooks/radarr/secret-token",
            content=b'{"padding":"' + (b"x" * (64 * 1024)) + b'"}',
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 413


def test_regenerating_webhook_token_invalidates_previous_url(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings, start_worker=False)

    with TestClient(app) as client:
        repository = app.state.repository
        repository.set_settings(
            {
                "webhooks_enabled": "true",
                "webhook_token": "old-token",
            }
        )
        confirmation = client.get("/settings/webhook-token/regenerate/confirm")
        unconfirmed = client.post(
            "/settings/webhook-token/regenerate",
            data=csrf_data(app),
            follow_redirects=False,
        )
        token_after_unconfirmed = repository.get_setting("webhook_token", "")
        regenerated = client.post(
            "/settings/webhook-token/regenerate",
            data=csrf_data(app, confirmed="yes"),
            follow_redirects=False,
        )
        new_token = repository.get_setting("webhook_token", "")
        old_url = client.post(
            "/webhooks/radarr/old-token",
            json={"eventType": "Test"},
        )
        new_url = client.post(
            f"/webhooks/radarr/{new_token}",
            json={"eventType": "Test"},
        )

    assert confirmation.status_code == 200
    assert "Regenerate webhook token" in confirmation.text
    assert 'name="confirmed"' in confirmation.text
    assert unconfirmed.status_code == 303
    assert (
        unconfirmed.headers["location"]
        == "/settings/webhook-token/regenerate/confirm?error=Confirm%20webhook%20token%20regeneration%20before%20continuing."
    )
    assert token_after_unconfirmed == "old-token"
    assert regenerated.status_code == 303
    assert "#webhooks" in regenerated.headers["location"]
    assert new_token and new_token != "old-token"
    assert old_url.status_code == 401
    assert new_url.status_code == 200


def test_radarr_download_maps_final_path_and_test_is_noop(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    folder = settings.media_root / "Movies"
    folder.mkdir()
    movie = folder / "Movie.mkv"
    movie.write_bytes(b"movie")
    app = create_app(settings, start_worker=False)

    with TestClient(app) as client:
        app.state.repository.set_settings(
            {
                "webhooks_enabled": "true",
                "webhook_token": "secret-token",
                "radarr_root_prefix": "/radarr",
            }
        )
        test_event = client.post(
            "/webhooks/radarr/secret-token",
            json={"eventType": "Test"},
        )
        download = client.post(
            "/webhooks/radarr/secret-token",
            json={
                "eventType": "Download",
                "movieFile": {"path": "/radarr/Movies/Movie.mkv"},
            },
        )

    assert test_event.status_code == 200
    assert app.state.repository.pending_scan_request_count() == 1
    assert download.status_code == 202
    assert not download.json()["fallback"]


def test_radarr_unmapped_event_falls_back_to_smart_scan(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings, start_worker=False)

    with TestClient(app) as client:
        app.state.repository.set_settings(
            {
                "webhooks_enabled": "true",
                "webhook_token": "secret-token",
                "radarr_root_prefix": "/radarr",
            }
        )
        response = client.post(
            "/webhooks/radarr/secret-token",
            json={
                "eventType": "Rename",
                "renamedFiles": [{"newPath": "/different/Movie.mkv"}],
            },
        )

    assert response.status_code == 202
    assert response.json()["fallback"]


def test_radarr_rename_scans_mapped_root(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    folder = settings.media_root / "Movies"
    folder.mkdir()
    movie = folder / "Movie.mkv"
    movie.write_bytes(b"movie")
    app = create_app(settings, start_worker=False)

    with TestClient(app) as client:
        app.state.repository.set_settings(
            {
                "webhooks_enabled": "true",
                "webhook_token": "secret-token",
                "radarr_root_prefix": "/radarr",
            }
        )
        response = client.post(
            "/webhooks/radarr/secret-token",
            json={
                "eventType": "Rename",
                "renamedFiles": [{"newPath": "/radarr/Movies/Movie.mkv"}],
            },
        )
        request = app.state.repository.claim_next_scan_request()

    assert response.status_code == 202
    assert not response.json()["fallback"]
    assert request["request_type"] == "smart"
    assert request["root_id"] == "default"
    assert request["relative_path"] is None


def test_sonarr_download_maps_to_shows_root_and_rename_scans_root(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    series = settings.shows_root / "Series"
    series.mkdir()
    episode = series / "Episode.mkv"
    episode.write_bytes(b"episode")
    app = create_app(settings, start_worker=False)

    with TestClient(app) as client:
        app.state.repository.set_settings(
            {
                "webhooks_enabled": "true",
                "webhook_token": "secret-token",
            }
        )
        app.state.repository.replace_webhook_mappings([("sonarr", "/tv", "shows")])
        download = client.post(
            "/webhooks/sonarr/secret-token",
            json={
                "eventType": "Download",
                "episodeFile": {"path": "/tv/Series/Episode.mkv"},
            },
        )
        request = app.state.repository.claim_next_scan_request()
        app.state.repository.complete_scan_request(request["id"])
        rename = client.post(
            "/webhooks/sonarr/secret-token",
            json={
                "eventType": "Rename",
                "renamedEpisodeFiles": [{"path": "/tv/Series/Episode.mkv"}],
            },
        )
        rename_request = app.state.repository.claim_next_scan_request()

    assert download.status_code == 202
    assert not download.json()["fallback"]
    assert request["request_type"] == "file"
    assert request["root_id"] == "shows"
    assert request["relative_path"] == "Series/Episode.mkv"
    assert rename.status_code == 202
    assert rename_request["request_type"] == "smart"
    assert rename_request["root_id"] == "shows"


def test_optional_basic_auth_protects_ui_but_not_health(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings = Settings(
        **{
            **settings.__dict__,
            "auth_username": "admin",
            "auth_password": "secret",
        }
    )
    app = create_app(settings, start_worker=False)
    token = base64.b64encode(b"admin:secret").decode()

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/").status_code == 401
        response = client.get("/", headers={"Authorization": f"Basic {token}"})

    assert response.status_code == 200


def test_csrf_token_is_bound_to_authenticated_user(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings = Settings(
        **{
            **settings.__dict__,
            "auth_username": "admin",
            "auth_password": "secret",
        }
    )
    app = create_app(settings, start_worker=False)
    authorization = "Basic " + base64.b64encode(b"admin:secret").decode()

    with TestClient(app) as client:
        local_token = csrf_token(app.state.csrf_secret, "local")
        rejected = client.post(
            "/scan",
            headers={"Authorization": authorization},
            data={"csrf_token": local_token},
        )
        admin_token = csrf_token(app.state.csrf_secret, "admin")
        accepted = client.post(
            "/scan",
            headers={"Authorization": authorization},
            data={"csrf_token": admin_token},
            follow_redirects=False,
        )

    assert rejected.status_code == 403
    assert accepted.status_code == 303


def test_missing_csrf_token_is_rejected(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path), start_worker=False)

    with TestClient(app) as client:
        response = client.post("/scan")

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid CSRF token"


def test_invalid_csrf_token_is_rejected(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path), start_worker=False)

    with TestClient(app) as client:
        response = client.post(
            "/scan",
            data={"csrf_token": "invalid"},
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid CSRF token"


def test_valid_csrf_is_independent_of_proxy_origin_headers(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path), start_worker=False)

    with TestClient(app) as client:
        response = client.post(
            "/scan",
            data=csrf_data(app),
            headers={
                "Origin": "https://browser-facing.example",
                "Host": "internal-service:8000",
                "X-Forwarded-Host": "another-proxy-name.example",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303


def test_cross_site_fetch_metadata_post_is_rejected(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path), start_worker=False)

    with TestClient(app) as client:
        responses = [
            client.post(
                path,
                data=csrf_data(app, **data),
                headers={"Sec-Fetch-Site": "cross-site"},
            )
            for path, data in MUTATION_REQUESTS
        ]

    assert all(response.status_code == 403 for response in responses)
    assert all(
        response.text == "Cross-site form submission rejected" for response in responses
    )


def test_every_mutation_route_rejects_missing_csrf(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path), start_worker=False)

    with TestClient(app) as client:
        responses = [client.post(path, data=data) for path, data in MUTATION_REQUESTS]

    assert all(response.status_code == 403 for response in responses)
    assert all(
        response.json()["detail"] == "Invalid CSRF token" for response in responses
    )


def test_every_mutation_route_rejects_invalid_csrf(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path), start_worker=False)

    with TestClient(app) as client:
        responses = [
            client.post(path, data={**data, "csrf_token": "invalid"})
            for path, data in MUTATION_REQUESTS
        ]

    assert all(response.status_code == 403 for response in responses)
    assert all(
        response.json()["detail"] == "Invalid CSRF token" for response in responses
    )


def test_every_mutation_route_rejects_cross_user_csrf(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings = Settings(
        **{
            **settings.__dict__,
            "auth_username": "admin",
            "auth_password": "secret",
        }
    )
    app = create_app(settings, start_worker=False)
    authorization = "Basic " + base64.b64encode(b"admin:secret").decode()

    with TestClient(app) as client:
        local_token = csrf_token(app.state.csrf_secret, "local")
        responses = [
            client.post(
                path,
                data={**data, "csrf_token": local_token},
                headers={"Authorization": authorization},
            )
            for path, data in MUTATION_REQUESTS
        ]

    assert all(response.status_code == 403 for response in responses)
    assert all(
        response.json()["detail"] == "Invalid CSRF token" for response in responses
    )


def test_every_mutation_route_accepts_valid_csrf(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path), start_worker=False)

    with TestClient(app) as client:
        responses = [
            client.post(path, data=csrf_data(app, **data))
            for path, data in MUTATION_REQUESTS
        ]

    assert all(response.status_code != 403 for response in responses)


def test_every_post_form_contains_csrf_token() -> None:
    templates = Path(__file__).parents[1] / "app" / "templates"
    forms: list[str] = []
    for path in templates.glob("*.html"):
        forms.extend(
            re.findall(
                r'<form\b[^>]*method="post"[^>]*>(.*?)</form>',
                path.read_text(encoding="utf-8"),
                flags=re.DOTALL,
            )
        )

    assert len(forms) == 15
    assert all('name="csrf_token"' in form for form in forms)


def test_security_headers_are_added(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path), start_worker=False)

    with TestClient(app) as client:
        response = client.get("/")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_readyz_is_public_and_reports_failed_checks(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings = Settings(
        **{
            **settings.__dict__,
            "auth_username": "admin",
            "auth_password": "secret",
        }
    )
    app = create_app(settings, start_worker=False)

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["checks"]["worker"] is False


def test_readyz_succeeds_when_dependencies_are_available(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.setattr("app.main.shutil.which", lambda _: "/usr/bin/tool")
    worker = SimpleNamespace(running=True)
    app = create_app(settings, start_worker=False, worker=worker)

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_static_bulk_confirmation_route_is_not_shadowed(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path), start_worker=False)

    with TestClient(app) as client:
        response = client.get("/candidates/bulk-mel/confirm")

    assert response.status_code == 200
    assert "Queue all safe MEL candidates" in response.text


def test_navigation_marks_current_page_and_candidate_filters(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path), start_worker=False)

    with TestClient(app) as client:
        response = client.get("/candidates")

    assert response.status_code == 200
    assert re.search(
        r'<a class="active" aria-current="page" href="/candidates">',
        response.text,
    )
    assert 'href="/candidates?category=mel"' in response.text
    assert 'href="/candidates?category=simple_fel"' in response.text
    assert 'href="/candidates?category=complex_fel"' in response.text
    assert 'href="/candidates?category=scan_error"' in response.text


def test_operational_pages_render_empty_states_and_responsive_labels(
    tmp_path: Path,
) -> None:
    app = create_app(make_settings(tmp_path), start_worker=False)

    with TestClient(app) as client:
        candidates = client.get("/candidates")
        jobs = client.get("/jobs")
        backups = client.get("/backups")

    assert "No candidates found" in candidates.text
    assert "No jobs found" in jobs.text
    assert "No backups found" in backups.text
    assert "Full Original" in backups.text
    assert "Compact Recovery" in backups.text
    templates = Path(__file__).parents[1] / "app" / "templates"
    assert 'data-label="Media file"' in (templates / "candidates.html").read_text(
        encoding="utf-8"
    )
    assert 'data-label="Target"' in (templates / "jobs.html").read_text(
        encoding="utf-8"
    )
    assert 'data-label="Backup types"' in (templates / "backups.html").read_text(
        encoding="utf-8"
    )


def test_backup_retention_override_is_settings_gated(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    counterpart = settings.media_root / "Movie.mkv"
    counterpart.write_bytes(b"converted")
    backup = settings.media_root / "Movie.mkv.bak.dovi_convert"
    backup.write_bytes(b"original")
    recent = (datetime.now(UTC) - timedelta(days=1)).timestamp()
    os.utime(backup, (recent, recent))
    app = create_app(settings, start_worker=False)

    with TestClient(app) as client:
        locked = client.get("/backups")
        app.state.repository.set_settings({"allow_backup_retention_override": "true"})
        unlocked = client.get("/backups")
        confirm = client.get(
            "/backups/delete/confirm?item=default%3AMovie.mkv&kind=full"
        )
        delete = client.post(
            "/backups/delete",
            data=csrf_data(
                app,
                selected="default:Movie.mkv",
                deletion_kind="full",
                approved="yes",
                acknowledge_no_recovery="yes",
            ),
            follow_redirects=False,
        )
        job = app.state.repository.get_job(1)

    assert "disabled" in locked.text
    assert 'aria-disabled="true"' in locked.text
    assert "Retained for" in locked.text
    assert "kind=full" in unlocked.text
    assert "Retention override" in confirm.text
    assert delete.status_code == 303
    assert '"retention_override":true' in job["payload_json"]


def test_backup_page_groups_two_types_and_offers_recovery_choice(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    (settings.media_root / "Movie.mkv").write_bytes(b"converted")
    (settings.media_root / "Movie.mkv.bak.dovi_convert").write_bytes(b"original")
    write_compact_archive(settings.media_root / "Movie.dovi")
    app = create_app(settings, start_worker=False)

    with TestClient(app) as client:
        response = client.get("/backups")
        manage = client.get("/backups/manage?item=default%3AMovie.mkv")

    assert response.text.count('data-label="Movie"') == 1
    assert "Full Original" in response.text
    assert "Compact Recovery" in response.text
    assert "Use Full Original" in manage.text
    assert "Use Compact Recovery" in manage.text


def test_ui_templates_do_not_use_inline_styles() -> None:
    templates = Path(__file__).parents[1] / "app" / "templates"

    for path in templates.rglob("*.html"):
        assert " style=" not in path.read_text(encoding="utf-8"), path


def test_conversion_risk_warning_is_only_on_conversion_reviews() -> None:
    templates = Path(__file__).parents[1] / "app" / "templates"
    conversion = (templates / "conversion_confirm.html").read_text(encoding="utf-8")
    bulk = (templates / "bulk_mel_confirm.html").read_text(encoding="utf-8")

    assert conversion.count('class="alert alert-warning"') == 1
    assert bulk.count('class="alert alert-warning"') == 1
    assert "Verify the converted file before deleting the full original" in conversion
    assert "Verify every converted file before deleting its full original" in bulk
    for name in ("dashboard.html", "candidates.html", "candidate_detail.html"):
        assert "Classification is not definitive" not in (
            templates / name
        ).read_text(encoding="utf-8")
        assert "scan estimates" not in (templates / name).read_text(encoding="utf-8")


def test_shell_includes_keyboard_and_reduced_motion_support(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path), start_worker=False)

    with TestClient(app) as client:
        response = client.get("/")

    assert 'class="skip-link" href="#main-content"' in response.text
    assert 'id="main-content"' in response.text
    assert 'tabindex="-1"' in response.text
    css = (Path(__file__).parents[1] / "app" / "static" / "app.css").read_text(
        encoding="utf-8"
    )
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert ":focus-visible" in css


def test_progressive_enhancements_keep_server_forms_available() -> None:
    templates = Path(__file__).parents[1] / "app" / "templates"
    backups = (templates / "backups.html").read_text(encoding="utf-8")
    settings = (templates / "settings.html").read_text(encoding="utf-8")

    assert "<noscript>" in backups
    assert 'href="/backups/manage?item=' in backups
    assert 'class="automation-disclosure"' in settings


def test_enabling_automation_requires_acknowledgement(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path), start_worker=False)

    with TestClient(app) as client:
        response = client.post(
            "/settings",
            data=csrf_data(
                app,
                retention_days="30",
                auto_queue_mel="yes",
            ),
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_scan_center_renders_four_server_side_workflows(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path), start_worker=False)

    with TestClient(app) as client:
        response = client.get("/scans")

    assert response.status_code == 200
    assert 'href="/scans"' in response.text
    assert 'aria-current="page"' in response.text
    for action in (
        "/scans/full",
        "/scans/smart",
        "/scans/custom",
        "/scans/file",
    ):
        assert f'action="{action}"' in response.text
    assert "Full scan" in response.text
    assert "Smart scan" in response.text
    assert "Custom scan" in response.text
    assert "File scan" in response.text


def test_scan_routes_snapshot_requested_modes(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    folder = settings.media_root / "Movies"
    folder.mkdir()
    movie = folder / "Movie.mkv"
    movie.write_bytes(b"movie")
    app = create_app(settings, start_worker=False)

    with TestClient(app) as client:
        smart = client.post(
            "/scans/smart",
            data=csrf_data(app),
            follow_redirects=False,
        )
        repository = app.state.repository
        smart_job = repository.get_job(1)
        repository.cancel_queued_job(1)
        file_scan = client.post(
            "/scans/file",
            data=csrf_data(app, target="Movies/Movie.mkv", debug="yes"),
            follow_redirects=False,
        )
        file_job = repository.get_job(2)

    assert smart.status_code == 303
    assert '"scan_mode":"smart"' in smart_job["payload_json"]
    assert file_scan.status_code == 303
    assert '"scan_mode":"file"' in file_job["payload_json"]
    assert '"target":"Movies/Movie.mkv"' in file_job["payload_json"]
    assert '"debug":true' in file_job["payload_json"]


def test_status_summary_includes_scan_and_automation_state(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path), start_worker=False)

    with TestClient(app) as client:
        response = client.get("/status/summary")

    body = response.json()
    assert body["scan"] == {
        "inventory_count": 0,
        "active_mode": None,
        "active_roots": [],
    }
    assert body["roots"] == [
        {"id": "default", "label": "Movies", "inventory_count": 0},
        {"id": "shows", "label": "Shows", "inventory_count": 0},
    ]
    assert body["automation"]["pending_requests"] == 0
    assert not body["automation"]["schedule_enabled"]
    assert not body["automation"]["webhooks_enabled"]


def test_operational_settings_persist_and_generate_webhook_token(
    tmp_path: Path,
) -> None:
    app = create_app(make_settings(tmp_path), start_worker=False)

    with TestClient(app) as client:
        response = client.post(
            "/settings",
            data=csrf_data(
                app,
                retention_days="45",
                scan_depth="8",
                scan_debug="yes",
                convert_safe_mode="yes",
                convert_verbose="yes",
                schedule_enabled="yes",
                schedule_interval_minutes="60",
                schedule_start_time="04:15",
                webhooks_enabled="yes",
                radarr_root_prefix="/radarr/movies",
                allow_backup_retention_override="yes",
            ),
            follow_redirects=False,
        )
        repository = app.state.repository

    assert response.status_code == 303
    assert repository.get_setting("scan_depth", "") == "8"
    assert repository.get_setting("schedule_enabled", "") == "true"
    assert repository.get_setting("schedule_start_time", "") == "04:15"
    assert repository.get_setting("webhooks_enabled", "") == "true"
    assert repository.get_setting("radarr_root_prefix", "") == "/radarr/movies"
    assert repository.get_setting("allow_backup_retention_override", "") == "true"
    assert repository.get_setting("webhook_token", "")
    assert repository.pending_scan_request_count() == 0


def test_settings_generate_webhooks_button_saves_mappings_and_returns_to_section(
    tmp_path: Path,
) -> None:
    app = create_app(make_settings(tmp_path), start_worker=False)

    with TestClient(app) as client:
        form_data = csrf_data(
            app,
            retention_days="30",
            settings_action="enable_webhooks",
        )
        form_data["mapping_integration"] = ["radarr", "sonarr"]
        form_data["mapping_prefix"] = ["/movies", "/tv"]
        form_data["mapping_root_id"] = ["default", "shows"]
        response = client.post(
            "/settings",
            data=form_data,
            follow_redirects=False,
        )
        repository = app.state.repository
        settings_page = client.get("/settings")

    assert response.status_code == 303
    assert "#webhooks" in response.headers["location"]
    assert "Webhook%20URLs%20generated" in response.headers["location"]
    assert repository.get_setting("webhooks_enabled", "") == "true"
    assert repository.get_setting("webhook_token", "")
    assert [
        {
            "integration": mapping["integration"],
            "external_prefix": mapping["external_prefix"],
            "root_id": mapping["root_id"],
        }
        for mapping in repository.list_webhook_mappings()
    ] == [
        {"integration": "radarr", "external_prefix": "/movies", "root_id": "default"},
        {"integration": "sonarr", "external_prefix": "/tv", "root_id": "shows"},
    ]
    assert 'data-copy-value="#radarr-webhook-url"' in settings_page.text
    assert 'data-copy-value="#sonarr-webhook-url"' in settings_page.text
    assert 'type="password" readonly' in settings_page.text
    assert 'href="/settings/webhook-token/regenerate/confirm"' in settings_page.text


def test_inspection_gate_forces_mel_auto_inspection(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path), start_worker=False)

    with TestClient(app) as client:
        response = client.post(
            "/settings",
            data=csrf_data(
                app,
                retention_days="30",
                auto_queue_mel="yes",
                auto_convert_mel_after_inspect="yes",
                automation_acknowledged="yes",
            ),
            follow_redirects=False,
        )
        settings_page = client.get("/settings")
        repository = app.state.repository

    assert response.status_code == 303
    assert repository.get_setting("auto_convert_mel_after_inspect", "") == "true"
    assert repository.get_setting("auto_inspect_mel", "") == "true"
    assert 'id="auto_inspect_mel"' in settings_page.text
    assert "Required by the conversion inspection gate." in settings_page.text


def test_inspection_gate_is_ignored_when_auto_conversion_is_off(
    tmp_path: Path,
) -> None:
    app = create_app(make_settings(tmp_path), start_worker=False)

    with TestClient(app) as client:
        client.post(
            "/settings",
            data=csrf_data(
                app,
                retention_days="30",
                auto_convert_mel_after_inspect="yes",
            ),
        )
        repository = app.state.repository

    assert repository.get_setting("auto_queue_mel", "") == "false"
    assert repository.get_setting("auto_convert_mel_after_inspect", "") == "false"


def test_settings_render_tooltips_schedule_units_and_sonarr(
    tmp_path: Path,
) -> None:
    app = create_app(make_settings(tmp_path), start_worker=False)

    with TestClient(app) as client:
        response = client.get("/settings")

    assert response.status_code == 200
    assert "info-tip-trigger" in response.text
    assert "slower legacy extraction path" in response.text
    assert 'name="schedule_interval_unit"' in response.text
    assert 'name="schedule_start_time"' in response.text
    assert 'name="allow_backup_retention_override"' in response.text
    assert "Sonarr" in response.text
    assert "data-add-mapping" in response.text
    assert "data-mapping-template" in response.text
    assert "Optional additional prefix" not in response.text


def test_settings_masks_webhook_urls_and_links_rotation_confirmation(
    tmp_path: Path,
) -> None:
    app = create_app(make_settings(tmp_path), start_worker=False)

    with TestClient(app) as client:
        app.state.repository.set_settings(
            {
                "webhooks_enabled": "true",
                "webhook_token": "secret-token",
            }
        )
        response = client.get("/settings")

    assert response.status_code == 200
    assert 'id="radarr-webhook-url" type="password"' in response.text
    assert 'id="sonarr-webhook-url" type="password"' in response.text
    assert 'data-secret-toggle="#radarr-webhook-url"' in response.text
    assert 'data-copy-value="#radarr-webhook-url"' in response.text
    assert 'href="/settings/webhook-token/regenerate/confirm"' in response.text
    assert 'action="/settings/webhook-token/regenerate"' not in response.text


def test_automation_acknowledgement_is_hidden_when_already_enabled(
    tmp_path: Path,
) -> None:
    app = create_app(make_settings(tmp_path), start_worker=False)

    with TestClient(app) as client:
        app.state.repository.set_settings({"auto_queue_mel": "true"})
        response = client.get("/settings")

    assert response.status_code == 200
    assert 'data-initial="true"' in response.text
    assert 'approval-check compact-check hidden" data-automation-ack-wrapper' in (
        response.text
    )


def test_simple_fel_route_requires_and_records_manual_approval(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    movie = settings.media_root / "Simple.mkv"
    movie.write_bytes(b"simple")
    stat = movie.stat()
    app = create_app(settings, start_worker=False)

    with TestClient(app) as client:
        repository = app.state.repository
        scan_id = repository.create_job(JobKind.SCAN)
        repository.replace_candidate_snapshot(
            [
                ScanCandidate(
                    relative_path=movie.name,
                    category=CandidateCategory.SIMPLE_FEL,
                    status_text="DV Profile 7 FEL (Simple)",
                    action_text="CONVERT*",
                    fingerprint=FileFingerprint(stat.st_size, stat.st_mtime_ns),
                )
            ],
            scan_id,
        )
        candidate_id = repository.list_candidates()[0]["id"]

        confirmation = client.get(f"/candidates/{candidate_id}/convert")
        rejected = client.post(
            f"/candidates/{candidate_id}/convert",
            data=csrf_data(app),
            follow_redirects=False,
        )
        accepted = client.post(
            f"/candidates/{candidate_id}/convert",
            data=csrf_data(app, approved="yes"),
            follow_redirects=False,
        )

    assert confirmation.status_code == 200
    assert "Simple FEL" in confirmation.text
    assert "Verify the converted file before deleting the full original" in (
        confirmation.text
    )
    assert "Simple FEL also requires this manual approval" in confirmation.text
    assert "--force" in confirmation.text
    assert "--delete" in confirmation.text
    assert "Backup retained" in confirmation.text
    assert "error=" in rejected.headers["location"]
    assert accepted.status_code == 303
    job = app.state.repository.list_jobs(kind=JobKind.CONVERT.value)[0]
    assert job["approved_by"] == "local"
    assert job["approved_at"] is not None


def test_candidate_search_and_pagination(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings, start_worker=False)

    with TestClient(app) as client:
        repository = app.state.repository
        scan_id = repository.create_job(JobKind.SCAN)
        candidates = []
        for index in range(55):
            path = settings.media_root / f"Movie-{index:02}.mkv"
            path.write_bytes(b"x")
            stat = path.stat()
            candidates.append(
                ScanCandidate(
                    relative_path=path.name,
                    category=CandidateCategory.MEL,
                    status_text="safe",
                    action_text="CONVERT",
                    fingerprint=FileFingerprint(stat.st_size, stat.st_mtime_ns),
                )
            )
        repository.replace_candidate_snapshot(candidates, scan_id)

        second_page = client.get("/candidates?page=2")
        searched = client.get("/candidates?q=Movie-54")

    assert "Page 2 of 2" in second_page.text
    assert "Movie-54.mkv" in second_page.text
    assert "1 result(s)" in searched.text


def test_job_status_endpoint_returns_live_fields(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path), start_worker=False)

    with TestClient(app) as client:
        job_id = app.state.repository.create_job(JobKind.SCAN)
        response = client.get(f"/jobs/{job_id}/status")

    assert response.status_code == 200
    assert response.json()["state"] == "queued"
    assert response.json()["active"] is True


def test_job_pagination(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path), start_worker=False)

    with TestClient(app) as client:
        for _ in range(55):
            app.state.repository.create_job(
                JobKind.BACKUP_DELETE,
                payload={"backups": []},
            )
        response = client.get("/jobs?page=2")

    assert response.status_code == 200
    assert "Page 2 of 2" in response.text


def test_dashboard_backup_summary_is_cached(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = 0

    def fake_discover(*args, **kwargs):
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr("app.main.discover_backup_sets", fake_discover)
    app = create_app(make_settings(tmp_path), start_worker=False)

    with TestClient(app) as client:
        client.get("/")
        client.get("/")

    assert calls == 1
