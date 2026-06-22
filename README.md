# dovi-manager

`dovi-manager` is a Dockerized web UI for
[`dovi_convert`](https://github.com/cryptochrome/dovi_convert). It scans your
media library for Dolby Vision Profile 7 MKV files and helps convert safe files
to Profile 8.1.

This project does not implement Dolby Vision conversion itself. All conversion
work is done by `dovi_convert`; dovi-manager adds a browser UI, queueing,
history, safety checks, backups, and Radarr/Sonarr automation around it.

Credit goes to [cryptochrome/dovi_convert](https://github.com/cryptochrome/dovi_convert)
for the actual media conversion engine.

## What It Does

- Scans one or more media roots for Dolby Vision Profile 7 MKVs.
- Shows candidates grouped as safe MEL, Simple FEL, Complex FEL, and scan errors.
- Exposes full frame-by-frame RPU inspection from each candidate.
- Converts safe MEL files to Profile 8.1 manually, or automatically if enabled.
- Keeps Simple FEL conversion manual and blocks Complex FEL conversion.
- Runs one background job at a time with logs and status.
- Supports full-original, compact `.dovi`, or combined backup modes.
- Uses compact-only recovery by default after validating conversion outputs.
- Groups both backup types per movie and provides reviewed Profile 7 recovery.
- Supports scheduled Smart Scans and Radarr/Sonarr webhooks.

Safety defaults:

- dovi-manager never passes `--force` or `--delete` to `dovi_convert`.
- Compact-only mode removes the full original only after the converted MKV,
  compact archive, and temporary full backup have all been validated.
- Files must stay under configured media roots.
- Files being written to are skipped.
- Backup deletion always requires confirmation.
- Deleting the only recovery source requires an additional acknowledgement.

Simple and Complex FEL scan verdicts are estimates, not guarantees. The current
detection logic can misclassify files. Full RPU inspection is more thorough, but
important media should still be verified before full originals or recovery
archives are removed. Upstream detection changes planned for dovi_convert 9.x
will be integrated only after its command and output formats are released.

## Docker Image

No separate `cryptochrome/dovi_convert` container is required.

The published dovi-manager image is built from the pinned upstream
`cryptochrome/dovi_convert:8.2.0` image and includes the CLI. Normal users pull
one image:

```text
ghcr.io/dovi-manager/dovi-manager:latest
```

Tags:

- `latest`: latest stable release
- `0.1.0`, `0.1`, etc.: versioned release tags
- `edge`: latest successful `main` build
- `sha-<full-commit>`: immutable rollback/debug tag

`stable` is not published; use `latest` for the stable channel.

## Install

Copy the example files:

```bash
cp .env.example .env
```

Edit `.env`:

```dotenv
DOVI_MANAGER_IMAGE=ghcr.io/dovi-manager/dovi-manager:latest
PUID=1000
PGID=1000
TZ=UTC

MEDIA_PATH=/srv/media/movies
SHOWS_PATH=/srv/media/shows
CACHE_PATH=/srv/dovi-manager/cache
CONFIG_PATH=/srv/dovi-manager/config

AUTH_USERNAME=admin
AUTH_PASSWORD=replace-with-a-long-unique-password
```

Create writable cache/config folders, then start:

```bash
mkdir -p /srv/dovi-manager/cache /srv/dovi-manager/config
chown 1000:1000 /srv/dovi-manager/cache /srv/dovi-manager/config

docker compose --env-file .env -f docker-compose.example.yml up -d
```

Open `http://SERVER_IP:8000/`.

The container needs read/write/rename access to the media folders because
conversion replaces the source file and writes a sibling backup.

## Basic Use

1. Open Settings and confirm storage, helper tools, authentication, and media roots.
2. Run a scan from the Dashboard or Scan Center.
3. Review candidates before converting anything.
4. Try one copied MEL file first.
5. Confirm the converted file plays correctly before enabling automation.

Compact recovery archives use dovi_convert 8.2's Backup & Restore feature. The
Backups page shows full originals and compact archives together for each movie.
Recovery is intentionally destructive: the selected recovery method replaces
the current converted MKV. Full recovery keeps the full original but removes a
sibling compact archive; compact recovery consumes the `.dovi` archive after a
successful replacement. Archive matching is filename-based, so recover only an
archive created from that source file.

Smart Scan only calls `dovi_convert` for new or changed files. Full Scan
rescans the selected root. File Scan targets one MKV.

## Radarr And Sonarr

Enable webhooks in Settings to generate tokenized URLs. Treat the full webhook
URLs as credentials and expose them only over HTTPS or a trusted private network.

### Radarr

1. In dovi-manager Settings, add a Radarr path mapping.
2. Enter the path Radarr sees, for example `/movies`.
3. Select the matching dovi-manager root, usually `Movies`.
4. In Radarr, add a Webhook connection using the generated Radarr URL.
5. Enable download/import and rename events.
6. Use Radarr's Test action.

Download/import events queue exact File Scans. Rename events queue a Smart Scan
for the mapped root. Unmapped or incomplete events fall back to a global Smart
Scan.

### Sonarr

1. In dovi-manager Settings, add a Sonarr path mapping.
2. Enter the path Sonarr sees, for example `/tv`.
3. Select the matching dovi-manager root, usually `Shows`.
4. In Sonarr, add a Webhook connection using the generated Sonarr URL.
5. Enable download/import and rename events.
6. Use Sonarr's Test action.

Download/import events use Sonarr episode file paths and queue exact File
Scans. Rename events queue a Smart Scan for the mapped root. Unmapped or
incomplete events fall back to a global Smart Scan.

Webhook requests can request scans only. They cannot queue conversions directly.

## Extra Media Roots

The compose example includes Movies and Shows. For more roots, add another bind
mount and create `/config/media-roots.json`:

```json
[
  {"id": "anime", "label": "Anime", "path": "/media2/anime"}
]
```

Root IDs should remain stable because candidates, jobs, and webhook mappings
refer to them.

## Development

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m pytest
```

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for upstream licensing and
source information.
