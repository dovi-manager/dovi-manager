# AGENTS.md

Project name: dovi-manager

Goal:
Build a Dockerized web UI that wraps the existing dovi_convert CLI.
Do not reimplement Dolby Vision conversion logic.

Target user:
Home media server users running Jellyfin/Radarr/Deluge who need Dolby Vision Profile 7 MKV files converted to Profile 8.1.

Stack for v1:
- Python 3
- FastAPI
- Jinja2 templates
- HTMX or simple server-rendered forms
- SQLite
- One background worker only
- Docker
- No React for v1
- No Celery/Redis for v1
- No Docker socket access
- No destructive actions without confirmation

Default container paths:
- MEDIA_ROOT=/media2/movies
- TEMP_DIR=/cache
- CONFIG_DIR=/config
- DB_PATH=/config/dovi-manager.db

Core dovi_convert commands:
- Scan candidates:
  dovi_convert scan "$MEDIA_ROOT" --recursive --candidates

- Convert safe MEL:
  dovi_convert convert "$FILE" --temp "$TEMP_DIR"

- Convert Simple FEL after manual approval:
  dovi_convert convert "$FILE" --temp "$TEMP_DIR" --include-simple

- Inspect:
  dovi_convert inspect "$FILE"

Safety rules:
- MEL/Safe files may be auto-converted.
- Simple FEL files require manual approval.
- Complex FEL files must never be auto-converted.
- Never use --force in v1.
- Never use --delete in v1.
- Do not process files ending in .bak.dovi_convert.
- Do not process files currently being written to.
- Only allow file paths under MEDIA_ROOT.
- Always keep backups unless user explicitly deletes them from the backups page.

V1 UI pages:
- Dashboard with counts
- Candidates page
- Jobs/logs page
- Backups page
- Settings/status page

V1 features:
- Run scan
- Parse dovi_convert scan output
- Store candidates in SQLite
- Show MEL/Safe, Simple FEL, Complex FEL separately
- Queue conversion jobs one at a time
- Show job logs
- Approve Simple FEL manually
- Show .bak.dovi_convert backup files
- Delete backups older than configured retention days, with confirmation

Tests required:
- Scan parser tests
- Path safety tests
- Backup detection tests
- Job state transition tests

Development rules:
- Make small commits.
- Add tests for parser/safety logic before adding UI complexity.
- Prefer simple, readable code over clever abstractions.
- Do not assume real media files exist in development.
- Include a docker-compose.example.yml.