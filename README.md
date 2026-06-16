# dovi-manager

`dovi-manager` is a Dockerized, server-rendered web UI for
[`dovi_convert`](https://github.com/cryptochrome/dovi_convert). It scans a
movie library for Dolby Vision Profile 7 MKV files and serializes conversions
to Profile 8.1.

The application does not reimplement conversion logic. The container is pinned
to `cryptochrome/dovi_convert:8.2.0` and invokes that CLI directly.

## Safety model

- Safe MEL files can be manually queued or optionally auto-queued.
- Simple FEL files always require explicit manual approval.
- Complex FEL files can be inspected but cannot be converted.
- `--force` and `--delete` are never passed to `dovi_convert`.
- Every conversion preserves the original as `.mkv.bak.dovi_convert`.
- Files must resolve beneath `MEDIA_ROOT`, remain unchanged since scanning,
  and remain stable for the configured window before conversion.
- Conversion starts only after write-permission and conservative free-space
  checks pass for both media and temporary storage.
- A successful CLI exit is accepted only when the converted MKV and a
  full-size original backup both exist.
- Backup deletion requires a separate review page and confirmation.
- Every mutation uses an actor-bound CSRF token backed by a secret persisted
  in `/config/csrf-secret`.
- Only one background job runs at a time.

## Pages

- Dashboard with candidate, job, backup, and worker status
- Scan Center with Full, Smart, Custom, and File scan workflows
- Candidates grouped as MEL, Simple FEL, Complex FEL, and scan errors
- Jobs with command details, bounded logs, states, and queued cancellation
- Backups with retention and orphan protection
- Settings/status with storage, helper tools, authentication, and automation

## Docker deployment

The recommended deployment path is the published multi-architecture container
image. Users do not need a Git checkout or a registry token for normal
installation.

Copy `docker-compose.example.yml` and `.env.example` to the host that can read
and write the media library. Set at least these environment values:

```dotenv
DOVI_MANAGER_IMAGE=ghcr.io/dovi-manager/dovi-manager:edge
PUID=1000
PGID=1000
TZ=UTC

MEDIA_PATH=/srv/media/movies
CACHE_PATH=/srv/dovi-manager/cache
CONFIG_PATH=/srv/dovi-manager/config

AUTH_USERNAME=admin
AUTH_PASSWORD=replace-with-a-long-unique-password
```

The `edge` tag is published after every successful CI run on `main`. The image
is available for both `linux/amd64` and `linux/arm64`.

### Prepare host storage

Use the UID and GID that own the media library:

```bash
id your-media-user
```

Create the writable cache and configuration directories:

```bash
mkdir -p /srv/dovi-manager/cache /srv/dovi-manager/config
chown 1000:1000 /srv/dovi-manager/cache /srv/dovi-manager/config
```

The configured user also needs read/write/rename permission throughout the
movie library. Conversion replaces the source path and creates a sibling
backup, so read-only media mounts cannot work.

### Start and update

Start the service with Docker Compose:

```bash
docker compose --env-file .env -f docker-compose.example.yml up -d
```

After a successful `main` build, pull the current image and recreate the
container:

```bash
docker compose --env-file .env -f docker-compose.example.yml pull
docker compose --env-file .env -f docker-compose.example.yml up -d
```

Verify the deployed revision:

```bash
curl http://SERVER_IP:8000/versionz
```

The returned `revision` must match the expected full Git commit. Refresh any
open browser page after an update so its forms contain a CSRF token generated
from the current persistent secret.

The Compose stack has no Docker socket, uses `no-new-privileges`, runs under
`PUID:PGID`, and mounts the container root filesystem read-only.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DOVI_MANAGER_IMAGE` | `ghcr.io/dovi-manager/dovi-manager:edge` | Image tag or immutable rollback tag |
| `MEDIA_ROOT` | `/media2/movies` | Container movie-library root |
| `MEDIA_ROOT_LABEL` | `Movies` | Display label for the backward-compatible default root |
| `ADDITIONAL_MEDIA_ROOTS_JSON` | `[]` | Additional stable root IDs, labels, and absolute container paths |
| `TEMP_DIR` | `/cache` | Fast temporary storage for conversion |
| `CONFIG_DIR` | `/config` | Persistent application data |
| `DB_PATH` | `/config/dovi-manager.db` | SQLite database |
| `SCAN_DEPTH` | `5` | Recursive `dovi_convert scan` depth |
| `STABILITY_SECONDS` | `30` | Required unchanged period before conversion |
| `RETENTION_DAYS` | `30` | Initial backup deletion threshold |
| `JOB_LOG_LIMIT` | `1000000` | Maximum stored characters per job |
| `SCAN_OUTPUT_LIMIT_BYTES` | `20971520` | Maximum scan output retained for parsing |
| `DISK_RESERVE_GIB` | `2` | Free space retained beyond conversion estimates |
| `DOVI_CONVERT_PATH` | `dovi_convert` | CLI executable |
| `AUTH_USERNAME` | unset | Optional Basic Auth username |
| `AUTH_PASSWORD` | unset | Optional Basic Auth password |
| `SHUTDOWN_GRACE_SECONDS` | `20` | CLI cleanup time during shutdown |
| `STOP_GRACE_PERIOD` | `45s` | Compose container shutdown allowance |
| `PUID` / `PGID` | `1000` | Numeric container process identity |
| `TZ` | `UTC` | Container timezone |

`AUTH_USERNAME` and `AUTH_PASSWORD` must both be set or both be unset. The UI
shows a warning when authentication is disabled.

Operational settings are editable in the UI and persisted in SQLite:

- default scan depth and debug logging;
- conversion `--safe` and `--verbose` defaults;
- scheduled Smart Scan state and interval in minutes, hours, days, or weeks;
- webhook state and Radarr/Sonarr path mappings;
- backup retention, acknowledged safe-MEL automation, and per-format automatic
  full inspection.

Library paths remain environment-controlled. The default root continues to use
`MEDIA_ROOT`; additional roots use JSON:

```json
[{"id":"tv","label":"TV Shows","path":"/media2/tv"}]
```

Each path must also be mounted into the container. IDs must remain stable
because candidates, inventory, jobs, and webhook requests store them. Roots
may not overlap or use symlinks. A removed root remains in historical database
records but is inactive until the same ID is configured again.

For each additional library, add one bind mount and set
`ADDITIONAL_MEDIA_ROOTS_JSON`. For example:

```yaml
environment:
  MEDIA_ROOT: /media2/movies
  MEDIA_ROOT_LABEL: Movies
  ADDITIONAL_MEDIA_ROOTS_JSON: >-
    [{"id":"tv","label":"TV Shows","path":"/media2/tv"}]
volumes:
  - /srv/media/movies:/media2/movies
  - /srv/media/tv:/media2/tv
```

Environment variables remain the deployment defaults. Each queued job stores a
snapshot of its effective options, so later settings changes do not alter
already queued work. Safe-MEL automation and scheduled scans are disabled
initially. Retention never causes automatic deletion.

Set `STOP_GRACE_PERIOD` at least ten seconds higher than
`SHUTDOWN_GRACE_SECONDS`. Conversion estimates reserve 110% of the source size
on both media and temporary filesystems, plus `DISK_RESERVE_GIB`. When both
paths share a filesystem, the two conversion estimates are combined and the
reserve is added once.

Each conversion receives an isolated directory under
`TEMP_DIR/dovi-manager/job-ID`. Only directories matching that application
owned pattern are cleaned.

The CSRF secret is generated on first startup at `/config/csrf-secret` with
mode `0600`. Keep it with the rest of the configuration backup. A missing
secret is regenerated; an unreadable or malformed secret prevents startup.

## Scan modes

- **Full Scan** scans all configured roots, or one selected root, to the
  configured depth and replaces the matching candidate and inventory snapshot.
- **Smart Scan** inventories the same selected scope but invokes
  `dovi_convert` only for new files or files whose size or modification time
  changed. Deleted inventory entries are deactivated without a CLI call.
- **Custom Scan** targets one root and relative directory with per-run recursion,
  depth, and debug options. Only that scope is reconciled.
- **File Scan** classifies one MKV selected through the bounded live filesystem
  browser or entered as a root-relative path. The browser excludes symlinks and
  `.bak.dovi_convert` files.

**Full RPU Inspection** is the existing `dovi_convert inspect` operation. It
performs frame-by-frame analysis, appears in Scan Center and candidate history,
and remains manually available. Settings can automatically inspect changed
MEL, Simple FEL, and Complex FEL candidates independently. These options
default off and never authorize conversion.

The first Smart Scan after upgrading scans every eligible MKV because older
database versions did not record non-candidate files. Unchanged scan errors are
not retried automatically; use File Scan to retry one explicitly.

Scheduled discovery uses the same single worker and never overlaps another
scan. Enabling the schedule queues an initial global Smart Scan. Later scans
check every enabled root after the configured interval, measured from
completion of the previous scheduled scan. Intervals range from 5 minutes
through 52 weeks.

Discovery automation does not independently authorize conversion. Newly found
MEL candidates are converted only when the separate acknowledged
**Automatic safe MEL queueing** setting is enabled. Simple FEL remains manual
and Complex FEL remains blocked.

## Webhook automation

Enable webhooks in Settings to generate tokenized Radarr and Sonarr endpoint
URLs. Regenerating the token immediately invalidates every previous URL. Treat
the complete URLs as credentials and expose them only over HTTPS or a trusted
private network.

### Radarr

1. Add a Radarr path mapping in Settings. Enter the movie path visible inside
   Radarr, for example `/movies`, and select the matching dovi-manager root.
2. In Radarr, add a Webhook connection using the generated Radarr URL.
3. Enable download/import and rename events, then use Radarr's Test action.

Download/import events beneath a configured mapping queue exact File Scans in
the target root. Rename events queue a Smart Scan for the mapped root so both
new paths and removed inventory paths are reconciled. Missing or unmappable
events safely fall back to global Smart Scan. When prefixes overlap, the
longest matching prefix wins. Test events never queue work.

### Sonarr

1. Add a Sonarr mapping for each TV path visible inside Sonarr, such as `/tv`,
   and select the matching dovi-manager root.
2. In Sonarr, add a Webhook connection using the generated Sonarr URL.
3. Enable download/import and rename events, then run Sonarr's Test action.

Download/import events use `episodeFile.path` or `episodeFiles[].path` and
queue exact File Scans. Rename events queue a Smart Scan for the mapped root so
both the new file and removed inventory path are reconciled. Incomplete or
unmapped payloads safely fall back to a global Smart Scan.

Webhook requests are limited to 64 KiB, independently token-authenticated, and
cannot invoke conversion directly. Unmapped paths, traversal, symlinks,
non-MKV files, backup files, and paths outside configured roots are rejected or
fall back to Smart Scan.

## First server test

Use a temporary test directory or one expendable media copy before pointing the
app at the entire library.

1. Confirm `/healthz` returns exactly `{"status":"ok"}`.
2. Confirm `/versionz` reports the expected Git revision.
3. Confirm `/readyz` returns status `200` and every check is `true`.
4. Open Settings and verify every helper tool is found.
5. Confirm media, cache, and config storage show available space.
6. Hard-refresh the page and run a scan.
7. Review MEL, Simple FEL, and Complex FEL classifications.
8. Queue an inspection and check its captured log.
9. Convert one copied MEL file.
10. Verify the converted MKV exists at the original path.
11. Verify the original exists as `.mkv.bak.dovi_convert` at the original size.
12. Confirm your media server can play the converted file before considering deletion.
13. Lower retention only for testing, review the backup deletion page, and
    confirm orphaned backups remain protected.

Do not enable automatic MEL queueing until manual conversion has succeeded with
the server's actual mounts and permissions.

## Reverse proxy and TLS

Basic Auth credentials are only protected in transit when HTTPS is used.
Prefer one of these deployments:

- expose the service only on a trusted private network;
- place it behind an HTTPS reverse proxy with its own authentication; or
- enable application Basic Auth and terminate TLS at the reverse proxy.

When trusting proxy headers, set `FORWARDED_ALLOW_IPS` to the proxy address or
CIDR. Do not use `*` on an untrusted network.

## Upgrades and recovery

Before upgrading, back up the persistent configuration directory while the
container is stopped:

```bash
docker stop dovi-manager
tar -C /srv/dovi-manager -czf dovi-manager-config-backup.tgz config
docker start dovi-manager
```

Schema migrations run transactionally at startup. A database created by a newer
unsupported application version causes startup to fail instead of being
downgraded.

If the container stops during a job:

- the active CLI receives `SIGINT` and gets a cleanup grace period;
- a job left in `running` is marked failed on restart;
- queued jobs remain queued;
- original backups are not automatically removed.

Restore the configuration archive only while the application is stopped.

### Rollback

Every `main` build also publishes an immutable image tag:

```text
ghcr.io/dovi-manager/dovi-manager:sha-<full-commit>
```

Set `DOVI_MANAGER_IMAGE` in `.env` to the desired immutable tag, then pull and
recreate the container. Set it back to `:edge` to resume the testing channel.
Version tags such as `v0.1.0` publish `0.1.0`, `0.1`, and `latest`.

## Local development

Python 3.12 is the supported development version.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt

$env:MEDIA_ROOT = "$PWD\dev-media"
$env:TEMP_DIR = "$PWD\dev-cache"
$env:CONFIG_DIR = "$PWD\config"

New-Item -ItemType Directory -Force dev-media, dev-cache, config
python -m uvicorn app.main:app --reload
```

Run tests:

```powershell
python -m pytest
python -m compileall -q app tests
python -m ruff check app tests
python -m ruff format --check app tests
```

Tests use temporary fake media files and fake subprocess runners. Real media is
not required.

For a local Docker build, combine the deployment file with the build override:

```bash
docker compose \
  -f docker-compose.example.yml \
  -f docker-compose.local.yml \
  up --build
```

## Current limitations

- No outbound notifications
- No Complex FEL conversion
- No automatic safe-mode retry after a standard conversion failure
- No compact `.dovi` backup/restore workflow
- No running-job cancellation from the UI
- Scan parsing targets the human table output of `dovi_convert 8.2.0`; ambiguous
  truncated filenames fail the scan without replacing the previous snapshot
- `/healthz` is liveness only; `/readyz` validates the worker, database,
  storage permissions, and required conversion tools

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for upstream licensing and
source information.
