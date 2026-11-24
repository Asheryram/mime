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


# --- Invited users ---------------------------------------------------------------
# The owner signs in as before (triage_auth). Friends the owner invites get a
# row in `invites`; when they sign up, a row in `members` holding their own
# password hash and the id of their data folder (tenants/<id>/ in the state
# repo). Removing a member deletes both rows, which ends their sessions on the
# next request (app.py re-checks membership every time).

def _ensure_member_tables(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS invites (
            email TEXT PRIMARY KEY,
            invited_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS members (
            email TEXT PRIMARY KEY,
            tenant TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    conn.commit()


def _with(fn):
    conn = _connect()
    try:
        _ensure_member_tables(conn)
        return fn(conn)
    finally:
        conn.close()


def list_invites():
    """[{email, invited_at, tenant or None}] newest first: who's invited and who has joined."""
    def q(conn):
        rows = conn.execute(
            "SELECT i.email, i.invited_at, m.tenant, m.created_at FROM invites i "
            "LEFT JOIN members m ON m.email = i.email ORDER BY i.invited_at DESC"
        ).fetchall()
        return [{"email": r[0], "invited_at": r[1], "tenant": r[2], "joined_at": r[3]} for r in rows]
    return _with(q)


def add_invite(email, when):
    def q(conn):
        conn.execute("INSERT OR IGNORE INTO invites (email, invited_at) VALUES (?, ?)", (email, when))
        conn.commit()
    return _with(q)


def is_invited(email):
    return _with(lambda conn: conn.execute("SELECT 1 FROM invites WHERE email = ?", (email,)).fetchone() is not None)


def get_member(email):
    def q(conn):
        r = conn.execute("SELECT email, tenant, password_hash FROM members WHERE email = ?", (email,)).fetchone()
        return {"email": r[0], "tenant": r[1], "password_hash": r[2]} if r else None
    return _with(q)


def create_member(email, tenant, password_hash, when):
    def q(conn):
        conn.execute(
            "INSERT INTO members (email, tenant, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (email, tenant, password_hash, when, when),
        )
        conn.commit()
    return _with(q)


def set_member_password(email, password_hash, when):
    def q(conn):
        conn.execute("UPDATE members SET password_hash = ?, updated_at = ? WHERE email = ?",
                     (password_hash, when, email))
        conn.commit()
    return _with(q)


def remove_access(email):
    """Delete the invite and the account. Returns the member's tenant id, if they had joined."""
    def q(conn):
        r = conn.execute("SELECT tenant FROM members WHERE email = ?", (email,)).fetchone()
        conn.execute("DELETE FROM members WHERE email = ?", (email,))
        conn.execute("DELETE FROM invites WHERE email = ?", (email,))
        conn.commit()
        return r[0] if r else None
    return _with(q)
