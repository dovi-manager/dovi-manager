import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


SCHEMA_VERSION = 5


def connect_database(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextmanager
def database_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect_database(db_path)
    try:
        yield connection
    finally:
        connection.close()


def _create_metadata_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS app_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )


def _schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT value FROM app_metadata WHERE key = 'schema_version'"
    ).fetchone()
    return int(row["value"]) if row else 0


def _set_schema_version(connection: sqlite3.Connection, version: int) -> None:
    connection.execute(
        """
        INSERT INTO app_metadata (key, value)
        VALUES ('schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(version),),
    )


def _migrate_to_v2(connection: sqlite3.Connection) -> None:
    statements = [
        """
        CREATE TABLE candidates (
            id INTEGER PRIMARY KEY,
            relative_path TEXT NOT NULL UNIQUE,
            category TEXT NOT NULL CHECK (
                category IN ('mel', 'simple_fel', 'complex_fel', 'scan_error')
            ),
            status_text TEXT NOT NULL,
            action_text TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            file_mtime_ns INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            scan_job_id INTEGER REFERENCES jobs(id)
        )
        """,
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL CHECK (
                kind IN ('scan', 'convert', 'inspect', 'backup_delete')
            ),
            state TEXT NOT NULL CHECK (
                state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')
            ),
            candidate_id INTEGER REFERENCES candidates(id),
            payload_json TEXT NOT NULL DEFAULT '{}',
            command_json TEXT,
            log_text TEXT NOT NULL DEFAULT '',
            log_truncated INTEGER NOT NULL DEFAULT 0 CHECK (log_truncated IN (0, 1)),
            error TEXT,
            exit_code INTEGER,
            approved_at TEXT,
            approved_by TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        )
        """,
        """
        CREATE TABLE editable_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    ]
    for statement in statements:
        connection.execute(statement)


def _migrate_to_v3(connection: sqlite3.Connection) -> None:
    statements = [
        """
        CREATE INDEX idx_candidates_active_category
            ON candidates(active, category)
        """,
        """
        CREATE INDEX idx_jobs_state_created
            ON jobs(state, created_at)
        """,
        """
        CREATE INDEX idx_jobs_candidate_state
            ON jobs(candidate_id, state)
        """,
    ]
    for statement in statements:
        connection.execute(statement)


def _migrate_to_v4(connection: sqlite3.Connection) -> None:
    statements = [
        """
        CREATE TABLE media_inventory (
            relative_path TEXT PRIMARY KEY,
            file_size INTEGER NOT NULL,
            file_mtime_ns INTEGER NOT NULL,
            category TEXT CHECK (
                category IS NULL OR category IN (
                    'mel', 'simple_fel', 'complex_fel', 'scan_error'
                )
            ),
            status_text TEXT NOT NULL,
            action_text TEXT NOT NULL,
            first_scanned_at TEXT NOT NULL,
            last_scanned_at TEXT NOT NULL,
            scan_job_id INTEGER NOT NULL REFERENCES jobs(id)
        )
        """,
        """
        CREATE TABLE scan_requests (
            id INTEGER PRIMARY KEY,
            request_key TEXT NOT NULL,
            request_type TEXT NOT NULL CHECK (
                request_type IN ('smart', 'file')
            ),
            relative_path TEXT,
            trigger TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending' CHECK (
                state IN ('pending', 'claimed', 'completed')
            ),
            created_at TEXT NOT NULL,
            claimed_at TEXT,
            completed_at TEXT
        )
        """,
        """
        CREATE UNIQUE INDEX idx_scan_requests_active_key
            ON scan_requests(request_key)
            WHERE state IN ('pending', 'claimed')
        """,
        """
        CREATE INDEX idx_scan_requests_state_created
            ON scan_requests(state, created_at)
        """,
        """
        CREATE INDEX idx_media_inventory_scan_job
            ON media_inventory(scan_job_id)
        """,
    ]
    for statement in statements:
        connection.execute(statement)


def _migrate_to_v5(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE library_roots (
            id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            container_path TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO library_roots(id, label, container_path, active, updated_at)
        VALUES ('default', 'Movies', '', 1, datetime('now'))
        """
    )
    connection.execute(
        """
        CREATE TABLE candidates_v5 (
            id INTEGER PRIMARY KEY,
            root_id TEXT NOT NULL DEFAULT 'default'
                REFERENCES library_roots(id),
            relative_path TEXT NOT NULL,
            category TEXT NOT NULL CHECK (
                category IN ('mel', 'simple_fel', 'complex_fel', 'scan_error')
            ),
            status_text TEXT NOT NULL,
            action_text TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            file_mtime_ns INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            scan_job_id INTEGER,
            UNIQUE(root_id, relative_path)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO candidates_v5
        SELECT id, 'default', relative_path, category, status_text, action_text,
               file_size, file_mtime_ns, active, first_seen_at, last_seen_at,
               scan_job_id
        FROM candidates
        """
    )
    connection.execute(
        """
        CREATE TABLE jobs_v5 (
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL CHECK (
                kind IN ('scan', 'convert', 'inspect', 'backup_delete')
            ),
            state TEXT NOT NULL CHECK (
                state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')
            ),
            candidate_id INTEGER REFERENCES candidates_v5(id),
            payload_json TEXT NOT NULL DEFAULT '{}',
            command_json TEXT,
            log_text TEXT NOT NULL DEFAULT '',
            log_truncated INTEGER NOT NULL DEFAULT 0 CHECK (log_truncated IN (0, 1)),
            error TEXT,
            exit_code INTEGER,
            approved_at TEXT,
            approved_by TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO jobs_v5
        SELECT id, kind, state, candidate_id, payload_json, command_json,
               log_text, log_truncated, error, exit_code, approved_at,
               approved_by, created_at, started_at, finished_at
        FROM jobs
        """
    )
    connection.execute(
        """
        CREATE TABLE media_inventory_v5 (
            root_id TEXT NOT NULL DEFAULT 'default'
                REFERENCES library_roots(id),
            relative_path TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            file_mtime_ns INTEGER NOT NULL,
            category TEXT CHECK (
                category IS NULL OR category IN (
                    'mel', 'simple_fel', 'complex_fel', 'scan_error'
                )
            ),
            status_text TEXT NOT NULL,
            action_text TEXT NOT NULL,
            first_scanned_at TEXT NOT NULL,
            last_scanned_at TEXT NOT NULL,
            scan_job_id INTEGER NOT NULL REFERENCES jobs_v5(id),
            PRIMARY KEY(root_id, relative_path)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO media_inventory_v5
        SELECT 'default', relative_path, file_size, file_mtime_ns, category,
               status_text, action_text, first_scanned_at, last_scanned_at,
               scan_job_id
        FROM media_inventory
        """
    )
    connection.execute(
        """
        CREATE TABLE scan_requests_v5 (
            id INTEGER PRIMARY KEY,
            request_key TEXT NOT NULL,
            request_type TEXT NOT NULL CHECK (
                request_type IN ('smart', 'file')
            ),
            root_id TEXT REFERENCES library_roots(id),
            relative_path TEXT,
            trigger TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending' CHECK (
                state IN ('pending', 'claimed', 'completed')
            ),
            created_at TEXT NOT NULL,
            claimed_at TEXT,
            completed_at TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO scan_requests_v5
        SELECT id, request_key, request_type,
               CASE WHEN relative_path IS NULL THEN NULL ELSE 'default' END,
               relative_path, trigger, state, created_at, claimed_at, completed_at
        FROM scan_requests
        """
    )
    connection.execute(
        """
        CREATE TABLE webhook_path_mappings (
            id INTEGER PRIMARY KEY,
            integration TEXT NOT NULL CHECK (integration IN ('radarr', 'sonarr')),
            external_prefix TEXT NOT NULL,
            root_id TEXT NOT NULL REFERENCES library_roots(id),
            created_at TEXT NOT NULL,
            UNIQUE(integration, external_prefix)
        )
        """
    )

    connection.execute("DROP TABLE scan_requests")
    connection.execute("DROP TABLE media_inventory")
    connection.execute("DROP TABLE jobs")
    connection.execute("DROP TABLE candidates")
    connection.execute("ALTER TABLE candidates_v5 RENAME TO candidates")
    connection.execute("ALTER TABLE jobs_v5 RENAME TO jobs")
    connection.execute("ALTER TABLE media_inventory_v5 RENAME TO media_inventory")
    connection.execute("ALTER TABLE scan_requests_v5 RENAME TO scan_requests")

    statements = [
        "CREATE INDEX idx_candidates_active_category ON candidates(active, category)",
        "CREATE INDEX idx_candidates_root_active ON candidates(root_id, active)",
        "CREATE INDEX idx_jobs_state_created ON jobs(state, created_at)",
        "CREATE INDEX idx_jobs_candidate_state ON jobs(candidate_id, state)",
        """
        CREATE UNIQUE INDEX idx_scan_requests_active_key
            ON scan_requests(request_key)
            WHERE state IN ('pending', 'claimed')
        """,
        """
        CREATE INDEX idx_scan_requests_state_created
            ON scan_requests(state, created_at)
        """,
        """
        CREATE INDEX idx_media_inventory_scan_job
            ON media_inventory(scan_job_id)
        """,
    ]
    for statement in statements:
        connection.execute(statement)


def initialize_database(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with database_connection(db_path) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            connection.execute("BEGIN IMMEDIATE")
            _create_metadata_table(connection)
            version = _schema_version(connection)

            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema {version} is newer than supported "
                    f"schema {SCHEMA_VERSION}"
                )

            if version < 2:
                _migrate_to_v2(connection)
                _set_schema_version(connection, 2)
                version = 2

            if version < 3:
                _migrate_to_v3(connection)
                _set_schema_version(connection, 3)
                version = 3

            if version < 4:
                _migrate_to_v4(connection)
                _set_schema_version(connection, 4)
                version = 4
            if version < 5:
                connection.execute("PRAGMA defer_foreign_keys = ON")
                _migrate_to_v5(connection)
                _set_schema_version(connection, 5)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
