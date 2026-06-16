import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.db import database_connection
from app.config import MediaRoot
from app.dovi import normalize_terminal_output
from app.models import (
    ACTIVE_JOB_STATES,
    CandidateCategory,
    JobKind,
    JobState,
    ScannedFile,
    ScanCandidate,
)
from app import repository_settings


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Repository:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    def sync_library_roots(self, roots: Iterable[MediaRoot]) -> None:
        now = utc_now()
        configured = list(roots)
        with database_connection(self.db_path) as connection, connection:
            connection.execute("UPDATE library_roots SET active = 0")
            for root in configured:
                connection.execute(
                    """
                    INSERT INTO library_roots(
                        id, label, container_path, active, updated_at
                    )
                    VALUES (?, ?, ?, 1, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        label = excluded.label,
                        container_path = excluded.container_path,
                        active = 1,
                        updated_at = excluded.updated_at
                    """,
                    (root.id, root.label, str(root.path), now),
                )

    def list_library_roots(self, *, active_only: bool = True) -> list[sqlite3.Row]:
        where = "WHERE active = 1" if active_only else ""
        with database_connection(self.db_path) as connection:
            return connection.execute(
                f"SELECT * FROM library_roots {where} ORDER BY label, id"
            ).fetchall()

    def candidate_counts(self) -> dict[str, int]:
        counts = {category.value: 0 for category in CandidateCategory}
        with database_connection(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT category, COUNT(*) AS count
                FROM candidates
                WHERE active = 1
                GROUP BY category
                """
            ).fetchall()
        counts.update({row["category"]: row["count"] for row in rows})
        return counts

    def list_candidates(
        self,
        category: str | None = None,
        *,
        root_id: str | None = None,
        active_only: bool = True,
        search: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if active_only:
            clauses.append("c.active = 1")
        if category:
            clauses.append("c.category = ?")
            parameters.append(category)
        if root_id:
            clauses.append("c.root_id = ?")
            parameters.append(root_id)
        if search:
            clauses.append("c.relative_path LIKE ? ESCAPE '\\'")
            escaped = (
                search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            parameters.append(f"%{escaped}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        paging = ""
        if limit is not None:
            paging = "LIMIT ? OFFSET ?"
            parameters.extend((limit, offset))

        with database_connection(self.db_path) as connection:
            return connection.execute(
                f"""
                SELECT c.*, r.label AS root_label,
                       j.id AS latest_job_id,
                       j.state AS latest_job_state,
                       j.kind AS latest_job_kind
                FROM candidates c
                JOIN library_roots r ON r.id = c.root_id
                LEFT JOIN jobs j ON j.id = (
                    SELECT id FROM jobs
                    WHERE candidate_id = c.id
                    ORDER BY id DESC LIMIT 1
                )
                {where}
                ORDER BY c.category, c.relative_path
                {paging}
                """,
                parameters,
            ).fetchall()

    def count_candidates(
        self,
        category: str | None = None,
        *,
        root_id: str | None = None,
        search: str | None = None,
    ) -> int:
        clauses = ["active = 1"]
        parameters: list[Any] = []
        if category:
            clauses.append("category = ?")
            parameters.append(category)
        if root_id:
            clauses.append("root_id = ?")
            parameters.append(root_id)
        if search:
            clauses.append("relative_path LIKE ? ESCAPE '\\'")
            escaped = (
                search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            parameters.append(f"%{escaped}%")
        with database_connection(self.db_path) as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS count FROM candidates WHERE {' AND '.join(clauses)}",
                parameters,
            ).fetchone()
        return int(row["count"])

    def get_candidate(self, candidate_id: int) -> sqlite3.Row | None:
        with database_connection(self.db_path) as connection:
            return connection.execute(
                """
                SELECT c.*, r.label AS root_label
                FROM candidates c
                JOIN library_roots r ON r.id = c.root_id
                WHERE c.id = ?
                """,
                (candidate_id,),
            ).fetchone()

    def replace_candidate_snapshot(
        self,
        candidates: Iterable[ScanCandidate],
        scan_job_id: int,
        *,
        root_id: str = "default",
    ) -> None:
        now = utc_now()
        with database_connection(self.db_path) as connection, connection:
            connection.execute(
                "UPDATE candidates SET active = 0 WHERE root_id = ?",
                (root_id,),
            )
            for candidate in candidates:
                connection.execute(
                    """
                    INSERT INTO candidates (
                        root_id, relative_path, category, status_text, action_text,
                        file_size, file_mtime_ns, active, first_seen_at,
                        last_seen_at, scan_job_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    ON CONFLICT(root_id, relative_path) DO UPDATE SET
                        category = excluded.category,
                        status_text = excluded.status_text,
                        action_text = excluded.action_text,
                        file_size = excluded.file_size,
                        file_mtime_ns = excluded.file_mtime_ns,
                        active = 1,
                        last_seen_at = excluded.last_seen_at,
                        scan_job_id = excluded.scan_job_id
                    """,
                    (
                        candidate.root_id,
                        candidate.relative_path,
                        candidate.category.value,
                        candidate.status_text,
                        candidate.action_text,
                        candidate.fingerprint.size,
                        candidate.fingerprint.mtime_ns,
                        now,
                        now,
                        scan_job_id,
                    ),
                )

    def inventory(self, root_id: str = "default") -> dict[str, sqlite3.Row]:
        with database_connection(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM media_inventory
                WHERE root_id = ?
                ORDER BY relative_path
                """,
                (root_id,),
            ).fetchall()
        return {row["relative_path"]: row for row in rows}

    def inventory_count(self, root_id: str | None = None) -> int:
        where = "WHERE root_id = ?" if root_id else ""
        parameters = (root_id,) if root_id else ()
        with database_connection(self.db_path) as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS count FROM media_inventory {where}",
                parameters,
            ).fetchone()
        return int(row["count"])

    def active_scan_job(self) -> sqlite3.Row | None:
        with database_connection(self.db_path) as connection:
            return connection.execute(
                """
                SELECT * FROM jobs
                WHERE kind = 'scan' AND state IN ('queued', 'running')
                ORDER BY id LIMIT 1
                """
            ).fetchone()

    def last_scan_for_trigger(self, trigger: str) -> sqlite3.Row | None:
        with database_connection(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE kind = 'scan'
                ORDER BY id DESC
                LIMIT 200
                """
            ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            if payload.get("trigger") == trigger:
                return row
        return None

    def enqueue_scan_request(
        self,
        request_type: str,
        *,
        root_id: str | None = None,
        relative_path: str | None,
        trigger: str,
    ) -> tuple[int, bool]:
        request_key = f"{request_type}:{root_id or '*'}:{relative_path or '*'}"
        now = utc_now()
        with database_connection(self.db_path) as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO scan_requests (
                        request_key, request_type, root_id, relative_path,
                        trigger, state, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        request_key,
                        request_type,
                        root_id,
                        relative_path,
                        trigger,
                        now,
                    ),
                )
                connection.commit()
                return int(cursor.lastrowid), True
            except sqlite3.IntegrityError:
                connection.rollback()
                row = connection.execute(
                    """
                    SELECT id FROM scan_requests
                    WHERE request_key = ?
                      AND state IN ('pending', 'claimed')
                    """,
                    (request_key,),
                ).fetchone()
                if row is None:
                    raise
                return int(row["id"]), False

    def claim_next_scan_request(self) -> sqlite3.Row | None:
        with database_connection(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM scan_requests
                WHERE state = 'pending'
                ORDER BY id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE scan_requests
                SET state = 'claimed', claimed_at = ?
                WHERE id = ? AND state = 'pending'
                """,
                (utc_now(), row["id"]),
            )
            connection.commit()
        with database_connection(self.db_path) as connection:
            return connection.execute(
                "SELECT * FROM scan_requests WHERE id = ?",
                (row["id"],),
            ).fetchone()

    def complete_scan_request(self, request_id: int) -> None:
        with database_connection(self.db_path) as connection, connection:
            connection.execute(
                """
                UPDATE scan_requests
                SET state = 'completed', completed_at = ?
                WHERE id = ? AND state = 'claimed'
                """,
                (utc_now(), request_id),
            )

    def recover_claimed_scan_requests(self) -> int:
        with database_connection(self.db_path) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE scan_requests
                SET state = 'pending', claimed_at = NULL
                WHERE state = 'claimed'
                """
            )
            return cursor.rowcount

    def pending_scan_request_count(self) -> int:
        with database_connection(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM scan_requests
                WHERE state IN ('pending', 'claimed')
                """
            ).fetchone()
        return int(row["count"])

    def get_candidate_by_path(
        self,
        relative_path: str,
        root_id: str = "default",
    ) -> sqlite3.Row | None:
        with database_connection(self.db_path) as connection:
            return connection.execute(
                """
                SELECT * FROM candidates
                WHERE root_id = ? AND relative_path = ?
                """,
                (root_id, relative_path),
            ).fetchone()

    def reconcile_scan(
        self,
        results: Iterable[ScannedFile],
        scan_job_id: int,
        *,
        root_id: str = "default",
        replace_all: bool = False,
        removed_paths: Iterable[str] = (),
    ) -> None:
        scanned = list(results)
        removed = set(removed_paths)
        result_paths = {result.relative_path for result in scanned}
        now = utc_now()
        with database_connection(self.db_path) as connection, connection:
            if replace_all:
                connection.execute(
                    "UPDATE candidates SET active = 0 WHERE root_id = ?",
                    (root_id,),
                )
                existing_paths = {
                    row["relative_path"]
                    for row in connection.execute(
                        """
                        SELECT relative_path FROM media_inventory
                        WHERE root_id = ?
                        """,
                        (root_id,),
                    ).fetchall()
                }
                removed.update(existing_paths - result_paths)

            if removed:
                connection.executemany(
                    """
                    DELETE FROM media_inventory
                    WHERE root_id = ? AND relative_path = ?
                    """,
                    [(root_id, path) for path in removed],
                )
                connection.executemany(
                    """
                    UPDATE candidates SET active = 0
                    WHERE root_id = ? AND relative_path = ?
                    """,
                    [(root_id, path) for path in removed],
                )

            for result in scanned:
                result_root_id = result.root_id or root_id
                category = result.category.value if result.category else None
                connection.execute(
                    """
                    INSERT INTO media_inventory (
                        root_id, relative_path, file_size, file_mtime_ns, category,
                        status_text, action_text, first_scanned_at,
                        last_scanned_at, scan_job_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(root_id, relative_path) DO UPDATE SET
                        file_size = excluded.file_size,
                        file_mtime_ns = excluded.file_mtime_ns,
                        category = excluded.category,
                        status_text = excluded.status_text,
                        action_text = excluded.action_text,
                        last_scanned_at = excluded.last_scanned_at,
                        scan_job_id = excluded.scan_job_id
                    """,
                    (
                        result_root_id,
                        result.relative_path,
                        result.fingerprint.size,
                        result.fingerprint.mtime_ns,
                        category,
                        result.status_text,
                        result.action_text,
                        now,
                        now,
                        scan_job_id,
                    ),
                )
                if result.category is None:
                    connection.execute(
                        """
                        UPDATE candidates SET active = 0
                        WHERE root_id = ? AND relative_path = ?
                        """,
                        (result_root_id, result.relative_path),
                    )
                    continue
                connection.execute(
                    """
                    INSERT INTO candidates (
                        root_id, relative_path, category, status_text, action_text,
                        file_size, file_mtime_ns, active, first_seen_at,
                        last_seen_at, scan_job_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    ON CONFLICT(root_id, relative_path) DO UPDATE SET
                        category = excluded.category,
                        status_text = excluded.status_text,
                        action_text = excluded.action_text,
                        file_size = excluded.file_size,
                        file_mtime_ns = excluded.file_mtime_ns,
                        active = 1,
                        last_seen_at = excluded.last_seen_at,
                        scan_job_id = excluded.scan_job_id
                    """,
                    (
                        result_root_id,
                        result.relative_path,
                        result.category.value,
                        result.status_text,
                        result.action_text,
                        result.fingerprint.size,
                        result.fingerprint.mtime_ns,
                        now,
                        now,
                        scan_job_id,
                    ),
                )

    def deactivate_candidate(self, candidate_id: int) -> None:
        with database_connection(self.db_path) as connection, connection:
            connection.execute(
                "UPDATE candidates SET active = 0 WHERE id = ?",
                (candidate_id,),
            )

    def create_job(
        self,
        kind: JobKind,
        *,
        candidate_id: int | None = None,
        payload: dict[str, Any] | None = None,
        approved_by: str | None = None,
    ) -> int:
        now = utc_now()
        with database_connection(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if kind is JobKind.SCAN:
                duplicate = connection.execute(
                    """
                    SELECT id FROM jobs
                    WHERE kind = 'scan' AND state IN ('queued', 'running')
                    """
                ).fetchone()
            elif candidate_id is not None:
                duplicate = connection.execute(
                    """
                    SELECT id FROM jobs
                    WHERE kind = ? AND candidate_id = ?
                      AND state IN ('queued', 'running')
                    """,
                    (kind.value, candidate_id),
                ).fetchone()
            else:
                duplicate = None

            if duplicate:
                raise ValueError(f"an active {kind.value} job already exists")

            cursor = connection.execute(
                """
                INSERT INTO jobs (
                    kind, state, candidate_id, payload_json,
                    approved_at, approved_by, created_at
                )
                VALUES (?, 'queued', ?, ?, ?, ?, ?)
                """,
                (
                    kind.value,
                    candidate_id,
                    json.dumps(payload or {}, separators=(",", ":")),
                    now if approved_by else None,
                    approved_by,
                    now,
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def claim_next_job(self) -> sqlite3.Row | None:
        with database_connection(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE state = 'queued'
                ORDER BY id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE jobs
                SET state = 'running', started_at = ?
                WHERE id = ? AND state = 'queued'
                """,
                (utc_now(), row["id"]),
            )
            connection.commit()
        return self.get_job(row["id"])

    def get_job(self, job_id: int) -> sqlite3.Row | None:
        with database_connection(self.db_path) as connection:
            return connection.execute(
                """
                SELECT j.*, c.relative_path AS candidate_path,
                       c.category AS candidate_category,
                       c.root_id AS candidate_root_id,
                       r.label AS candidate_root_label
                FROM jobs j
                LEFT JOIN candidates c ON c.id = j.candidate_id
                LEFT JOIN library_roots r ON r.id = c.root_id
                WHERE j.id = ?
                """,
                (job_id,),
            ).fetchone()

    def list_jobs(
        self,
        state: str | None = None,
        kind: str | None = None,
        *,
        limit: int = 200,
        offset: int = 0,
        search: str | None = None,
        category: str | None = None,
        root_id: str | None = None,
        origin: str | None = None,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if state:
            clauses.append("j.state = ?")
            parameters.append(state)
        if kind:
            clauses.append("j.kind = ?")
            parameters.append(kind)
        if search:
            clauses.append(
                "(c.relative_path LIKE ? ESCAPE '\\' OR CAST(j.id AS TEXT) = ?)"
            )
            escaped = (
                search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            parameters.extend((f"%{escaped}%", search.lstrip("#")))
        if category:
            clauses.append(
                "COALESCE(c.category, json_extract(j.payload_json, '$.candidate_category')) = ?"
            )
            parameters.append(category)
        if root_id:
            clauses.append(
                "COALESCE(c.root_id, json_extract(j.payload_json, '$.root_id')) = ?"
            )
            parameters.append(root_id)
        if origin == "automatic":
            clauses.append(
                "(j.approved_by = 'local:auto' OR "
                "json_extract(j.payload_json, '$.queue_origin') = 'automatic')"
            )
        elif origin == "manual":
            clauses.append(
                "j.kind = 'convert' AND j.approved_by != 'local:auto' "
                "AND COALESCE(json_extract(j.payload_json, '$.queue_origin'), 'manual') "
                "= 'manual'"
            )
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.extend((limit, offset))

        with database_connection(self.db_path) as connection:
            return connection.execute(
                f"""
                SELECT j.*, c.relative_path AS candidate_path,
                       c.category AS candidate_category,
                       c.root_id AS candidate_root_id,
                       r.label AS candidate_root_label
                FROM jobs j
                LEFT JOIN candidates c ON c.id = j.candidate_id
                LEFT JOIN library_roots r ON r.id = c.root_id
                {where}
                ORDER BY j.id DESC
                LIMIT ? OFFSET ?
                """,
                parameters,
            ).fetchall()

    def count_jobs(
        self,
        state: str | None = None,
        kind: str | None = None,
        *,
        search: str | None = None,
        category: str | None = None,
        root_id: str | None = None,
        origin: str | None = None,
    ) -> int:
        clauses: list[str] = []
        parameters: list[Any] = []
        if state:
            clauses.append("j.state = ?")
            parameters.append(state)
        if kind:
            clauses.append("j.kind = ?")
            parameters.append(kind)
        if search:
            clauses.append(
                "(c.relative_path LIKE ? ESCAPE '\\' OR CAST(j.id AS TEXT) = ?)"
            )
            escaped = (
                search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            parameters.extend((f"%{escaped}%", search.lstrip("#")))
        if category:
            clauses.append(
                "COALESCE(c.category, json_extract(j.payload_json, '$.candidate_category')) = ?"
            )
            parameters.append(category)
        if root_id:
            clauses.append(
                "COALESCE(c.root_id, json_extract(j.payload_json, '$.root_id')) = ?"
            )
            parameters.append(root_id)
        if origin == "automatic":
            clauses.append(
                "(j.approved_by = 'local:auto' OR "
                "json_extract(j.payload_json, '$.queue_origin') = 'automatic')"
            )
        elif origin == "manual":
            clauses.append(
                "j.kind = 'convert' AND j.approved_by != 'local:auto' "
                "AND COALESCE(json_extract(j.payload_json, '$.queue_origin'), 'manual') "
                "= 'manual'"
            )
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with database_connection(self.db_path) as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM jobs j
                LEFT JOIN candidates c ON c.id = j.candidate_id
                {where}
                """,
                parameters,
            ).fetchone()
        return int(row["count"])

    def conversion_failed_for_fingerprint(
        self,
        candidate_id: int,
        file_size: int,
        file_mtime_ns: int,
    ) -> bool:
        with database_connection(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM jobs
                WHERE kind = 'convert' AND state = 'failed' AND candidate_id = ?
                """,
                (candidate_id,),
            ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            if (
                int(payload.get("file_size", -1)) == file_size
                and int(payload.get("file_mtime_ns", -1)) == file_mtime_ns
            ):
                return True
        return False

    def conversion_exists_for_fingerprint(
        self,
        candidate_id: int,
        file_size: int,
        file_mtime_ns: int,
    ) -> bool:
        with database_connection(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM jobs
                WHERE kind = 'convert' AND candidate_id = ?
                """,
                (candidate_id,),
            ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            if (
                int(payload.get("file_size", -1)) == file_size
                and int(payload.get("file_mtime_ns", -1)) == file_mtime_ns
            ):
                return True
        return False

    def inspection_exists_for_fingerprint(
        self,
        candidate_id: int,
        file_size: int,
        file_mtime_ns: int,
    ) -> bool:
        with database_connection(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM jobs
                WHERE kind = 'inspect' AND candidate_id = ?
                """,
                (candidate_id,),
            ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            if (
                int(payload.get("file_size", -1)) == file_size
                and int(payload.get("file_mtime_ns", -1)) == file_mtime_ns
            ):
                return True
        return False

    def recent_failed_jobs(self, limit: int = 5) -> list[sqlite3.Row]:
        return self.list_jobs(JobState.FAILED.value, limit=limit)

    def candidate_job_history(
        self,
        candidate_id: int,
        kind: str,
        *,
        limit: int = 10,
    ) -> list[sqlite3.Row]:
        with database_connection(self.db_path) as connection:
            return connection.execute(
                """
                SELECT * FROM jobs
                WHERE candidate_id = ? AND kind = ?
                ORDER BY id DESC LIMIT ?
                """,
                (candidate_id, kind, limit),
            ).fetchall()

    def conversion_summary(self) -> dict[str, int]:
        summary = {
            "succeeded": 0,
            "failed": 0,
            "mel": 0,
            "simple_fel": 0,
            "manual": 0,
            "automatic": 0,
        }
        conversions = self.list_jobs(kind=JobKind.CONVERT.value, limit=1_000_000)
        for job in conversions:
            payload = json.loads(job["payload_json"])
            if job["state"] in {"succeeded", "failed"}:
                summary[job["state"]] += 1
            category = (
                payload.get("candidate_category")
                or job["candidate_category"]
                or ("simple_fel" if payload.get("include_simple") else "mel")
            )
            if category in {"mel", "simple_fel"}:
                summary[category] += 1
            origin = payload.get("queue_origin")
            if origin not in {"manual", "automatic"}:
                origin = "automatic" if job["approved_by"] == "local:auto" else "manual"
            summary[origin] += 1
        return summary

    def set_job_command(self, job_id: int, command: list[str]) -> None:
        with database_connection(self.db_path) as connection, connection:
            connection.execute(
                "UPDATE jobs SET command_json = ? WHERE id = ?",
                (json.dumps(command), job_id),
            )

    def append_job_log(self, job_id: int, text: str, limit: int) -> None:
        if not text:
            return
        with database_connection(self.db_path) as connection, connection:
            row = connection.execute(
                "SELECT log_text, log_truncated FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                return
            combined = normalize_terminal_output(row["log_text"] + text)
            truncated = row["log_truncated"]
            if len(combined) > limit:
                combined = combined[-limit:]
                truncated = 1
            connection.execute(
                """
                UPDATE jobs
                SET log_text = ?, log_truncated = ?
                WHERE id = ?
                """,
                (combined, truncated, job_id),
            )

    def finish_job(
        self,
        job_id: int,
        state: JobState,
        *,
        exit_code: int | None = None,
        error: str | None = None,
    ) -> None:
        if state not in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED):
            raise ValueError("finish_job requires a terminal state")
        with database_connection(self.db_path) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET state = ?, exit_code = ?, error = ?, finished_at = ?
                WHERE id = ? AND state = 'running'
                """,
                (state.value, exit_code, error, utc_now(), job_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("only running jobs can enter a terminal state")

    def cancel_queued_job(self, job_id: int) -> bool:
        with database_connection(self.db_path) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET state = 'cancelled', finished_at = ?
                WHERE id = ? AND state = 'queued'
                """,
                (utc_now(), job_id),
            )
            return cursor.rowcount == 1

    def recover_running_jobs(self) -> int:
        with database_connection(self.db_path) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET state = 'failed',
                    error = 'Interrupted by application restart',
                    finished_at = ?
                WHERE state = 'running'
                """,
                (utc_now(),),
            )
            return cursor.rowcount

    def job_counts(self) -> dict[str, int]:
        counts = {state.value: 0 for state in JobState}
        with database_connection(self.db_path) as connection:
            rows = connection.execute(
                "SELECT state, COUNT(*) AS count FROM jobs GROUP BY state"
            ).fetchall()
        counts.update({row["state"]: row["count"] for row in rows})
        return counts

    def last_scan(self) -> sqlite3.Row | None:
        with database_connection(self.db_path) as connection:
            return connection.execute(
                """
                SELECT * FROM jobs
                WHERE kind = 'scan'
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()

    def get_setting(self, key: str, default: str) -> str:
        return repository_settings.get_setting(self.db_path, key, default)

    def set_settings(self, values: dict[str, str]) -> None:
        repository_settings.set_settings(
            self.db_path,
            values,
            updated_at=utc_now(),
        )

    def list_webhook_mappings(
        self,
        integration: str | None = None,
    ) -> list[sqlite3.Row]:
        return repository_settings.list_webhook_mappings(self.db_path, integration)

    def replace_webhook_mappings(
        self,
        mappings: Iterable[tuple[str, str, str]],
    ) -> None:
        repository_settings.replace_webhook_mappings(
            self.db_path,
            mappings,
            created_at=utc_now(),
        )

    def migrate_legacy_radarr_mapping(self) -> None:
        prefix = self.get_setting("radarr_root_prefix", "").strip()
        if not prefix or self.list_webhook_mappings():
            return
        self.replace_webhook_mappings([("radarr", prefix, "default")])

    def active_jobs_for_candidate(self, candidate_id: int) -> list[sqlite3.Row]:
        placeholders = ",".join("?" for _ in ACTIVE_JOB_STATES)
        with database_connection(self.db_path) as connection:
            return connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE candidate_id = ? AND state IN ({placeholders})
                ORDER BY id
                """,
                (candidate_id, *ACTIVE_JOB_STATES),
            ).fetchall()
