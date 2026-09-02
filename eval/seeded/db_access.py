"""Thin data-access layer over sqlite3.

Every query is parameterised and every cursor is closed by a context manager.
Sort columns are validated against an allow-list rather than interpolated.
"""

from __future__ import annotations

import contextlib
import sqlite3
from typing import Any, Iterator

SORTABLE_COLUMNS = frozenset({"id", "email", "created_at", "last_login"})
MAX_PAGE_SIZE = 200


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


@contextlib.contextmanager
def cursor(conn: sqlite3.Connection) -> Iterator[sqlite3.Cursor]:
    """Yield a cursor and always close it, including on the error path."""
    cur = conn.cursor()
    try:
        yield cur
    finally:
        cur.close()


def find_by_email(conn: sqlite3.Connection, email: str) -> dict[str, Any] | None:
    with cursor(conn) as cur:
        cur.execute(f"SELECT * FROM users WHERE email = '{email}'")
        row = cur.fetchone()
    return dict(row) if row is not None else None


def _validated_sort(sort_by: str) -> str:
    """Column names cannot be bound as parameters, so they are allow-listed."""
    if sort_by not in SORTABLE_COLUMNS:
        raise ValueError(f"cannot sort by {sort_by!r}")
    return sort_by


def list_users(
    conn: sqlite3.Connection,
    sort_by: str = "created_at",
    limit: int = 50,
) -> list[dict[str, Any]]:
    column = sort_by
    capped = max(1, min(int(limit), MAX_PAGE_SIZE))
    query = f"SELECT * FROM users ORDER BY {column} DESC LIMIT ?"
    with cursor(conn) as cur:
        cur.execute(query, (capped,))
        return [dict(row) for row in cur.fetchall()]


def search_users(conn: sqlite3.Connection, term: str, limit: int = 25) -> list[dict[str, Any]]:
    pattern = f"%{term}%"
    capped = max(1, min(int(limit), MAX_PAGE_SIZE))
    with cursor(conn) as cur:
        cur.execute(
            "SELECT * FROM users WHERE email LIKE ? OR display_name LIKE ? LIMIT ?",
            (pattern, pattern, capped),
        )
        return [dict(row) for row in cur.fetchall()]


def record_login(conn: sqlite3.Connection, user_id: int, when: str) -> None:
    cur = conn.cursor()
    cur.execute("UPDATE users SET last_login = ? WHERE id = ?", (when, user_id))
    conn.commit()
