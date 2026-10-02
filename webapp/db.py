"""
Postgres-backed storage for the one login this app has: a single password
hash plus a timestamp, re-read on every request since Vercel functions don't
keep process state between invocations. Any Postgres-compatible connection
string works (Vercel Postgres, Neon, Supabase, Render Postgres, ...).
"""
import os
import psycopg2
import psycopg2.extras


def _connect():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def _ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS triage_auth (
                id INTEGER PRIMARY KEY DEFAULT 1,
                password_hash TEXT,
                updated_at TIMESTAMPTZ,
                CONSTRAINT single_row CHECK (id = 1)
            )
            """
        )
    conn.commit()


def load_auth():
    conn = _connect()
    try:
        _ensure_table(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT password_hash, updated_at FROM triage_auth WHERE id = 1")
            row = cur.fetchone()
            return dict(row) if row else {}
    finally:
        conn.close()


def save_auth(password_hash, updated_at):
    conn = _connect()
    try:
        _ensure_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO triage_auth (id, password_hash, updated_at) VALUES (1, %s, %s)
                ON CONFLICT (id) DO UPDATE SET password_hash = EXCLUDED.password_hash,
                                                updated_at = EXCLUDED.updated_at
                """,
                (password_hash, updated_at),
            )
        conn.commit()
    finally:
        conn.close()
