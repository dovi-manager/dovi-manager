from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

from app.db import database_connection


def get_setting(db_path: Path, key: str, default: str) -> str:
    with database_connection(db_path) as connection:
        row = connection.execute(
            "SELECT value FROM editable_settings WHERE key = ?",
            (key,),
        ).fetchone()
    return row["value"] if row else default


def set_settings(db_path: Path, values: dict[str, str], *, updated_at: str) -> None:
    with database_connection(db_path) as connection, connection:
        connection.executemany(
            """
            INSERT INTO editable_settings(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            [(key, value, updated_at) for key, value in values.items()],
        )


def list_webhook_mappings(
    db_path: Path,
    integration: str | None = None,
) -> list[sqlite3.Row]:
    where = "WHERE m.integration = ?" if integration else ""
    parameters = (integration,) if integration else ()
    with database_connection(db_path) as connection:
        return connection.execute(
            f"""
            SELECT m.*, r.label AS root_label
            FROM webhook_path_mappings m
            JOIN library_roots r ON r.id = m.root_id
            {where}
            ORDER BY m.integration, length(m.external_prefix) DESC,
                     m.external_prefix
            """,
            parameters,
        ).fetchall()


def replace_webhook_mappings(
    db_path: Path,
    mappings: Iterable[tuple[str, str, str]],
    *,
    created_at: str,
) -> None:
    with database_connection(db_path) as connection, connection:
        connection.execute("DELETE FROM webhook_path_mappings")
        connection.executemany(
            """
            INSERT INTO webhook_path_mappings(
                integration, external_prefix, root_id, created_at
            )
            VALUES (?, ?, ?, ?)
            """,
            [
                (integration, prefix, root_id, created_at)
                for integration, prefix, root_id in mappings
            ],
        )
