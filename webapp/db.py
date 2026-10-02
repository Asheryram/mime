"""
Turso (libSQL)-backed storage for the one login this app has: a single
password hash plus a timestamp, re-read on every request since Vercel
functions don't keep process state between invocations. Connects directly to
the remote primary on every call - no local embedded-replica file, since a
stateless function has nowhere durable to keep one.
"""
import os
import libsql


def _connect():
    return libsql.connect(
        database=os.environ["TURSO_DATABASE_URL"],
        auth_token=os.environ["TURSO_AUTH_TOKEN"],
    )


def _ensure_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS triage_auth (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            password_hash TEXT,
            updated_at TEXT
        )
        """
    )
    conn.commit()


def load_auth():
    conn = _connect()
    try:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT password_hash, updated_at FROM triage_auth WHERE id = 1"
        ).fetchone()
        if not row:
            return {}
        return {"password_hash": row[0], "updated_at": row[1]}
    finally:
        conn.close()


def save_auth(password_hash, updated_at):
    conn = _connect()
    try:
        _ensure_table(conn)
        ts = updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at)
        conn.execute(
            """
            INSERT INTO triage_auth (id, password_hash, updated_at) VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET password_hash = excluded.password_hash,
                                           updated_at = excluded.updated_at
            """,
            (password_hash, ts),
        )
        conn.commit()
    finally:
        conn.close()
