"""SQLite persistence for voice sessions, attempts, and usage.

Single-file database (WAL mode), stdlib sqlite3 only. All state-changing
operations use explicit BEGIN IMMEDIATE / COMMIT / ROLLBACK so concurrent
tabs, reconnect races, and duplicate requests serialize instead of
double-spending quota or creating two live sessions. No audio, no
transcripts, no PII beyond an opaque visitor id and a salted IP hash are
stored — see README (Privacy section) for the retention policy.
"""
from __future__ import annotations

import sqlite3
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  visitor_id TEXT NOT NULL,
  ip_hash TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  created_at REAL NOT NULL,
  started_at REAL,
  ended_at REAL,
  max_seconds INTEGER NOT NULL,
  last_seen_at REAL NOT NULL,
  end_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_visitor_status ON sessions(visitor_id, status);
CREATE INDEX IF NOT EXISTS idx_sessions_seen ON sessions(status, last_seen_at);
CREATE TABLE IF NOT EXISTS attempts (
  ip_hash TEXT NOT NULL,
  ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_ip_ts ON attempts(ip_hash, ts);
"""


class SessionError(Exception):
    """Machine-readable failure; maps to protocol error events / HTTP codes."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=10000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.executescript(SCHEMA)
    return conn


def _begin(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")


def _commit(conn: sqlite3.Connection) -> None:
    conn.execute("COMMIT")


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def recover_stale(conn: sqlite3.Connection, *, now: float, stale_after_seconds: int) -> int:
    """Expire live sessions unseen beyond the stale horizon. Joins the
    caller's transaction when one is open, otherwise commits its own work —
    so standalone calls never leave a dangling transaction behind."""
    joined = conn.in_transaction
    cur = conn.execute(
        """UPDATE sessions SET status='expired', end_reason='stale', ended_at=?
           WHERE status = 'active' AND last_seen_at < ?""",
        (now, now - stale_after_seconds),
    )
    count = cur.rowcount or 0
    if not joined:
        conn.commit()
    return count


def _usage_seconds(
    conn: sqlite3.Connection, visitor_id: str, day_start: float, max_seconds: int, now: float
) -> float:
    """Session active-time consumed since day_start (UTC), each session capped
    at max_seconds. Active time (not audio time) is the documented allowance
    metric: simple, auditable, impossible to game by staying silent. Live
    sessions accrue against the current time; negative clock-skew intervals
    are clamped to zero. Sessions straddling UTC midnight count only in the
    day they started (safe direction: undercounts, never overcharges)."""
    row = conn.execute(
        """SELECT COALESCE(SUM(
             CASE WHEN COALESCE(ended_at, ?1) > started_at
               THEN MIN(COALESCE(ended_at, ?1) - started_at, ?2)
               ELSE 0 END
           ), 0)
           FROM sessions
           WHERE visitor_id = ?3 AND started_at IS NOT NULL AND started_at >= ?4
             AND status IN ('active', 'ended', 'expired')""",
        (now, max_seconds, visitor_id, day_start),
    ).fetchone()
    return max(0.0, float(row[0] or 0.0))


def _day_start_utc(now: float) -> float:
    return float(int(now // 86400) * 86400)


def create_session(
    conn: sqlite3.Connection,
    *,
    visitor_id: str,
    ip_hash: str,
    now: float,
    max_seconds: int,
    daily_allowance_seconds: int,
    stale_after_seconds: int,
) -> dict:
    """Atomically enforce (stale recovery → one active session → daily quota)
    then insert. Raises SessionError on any denial. Returns the session row."""
    _begin(conn)
    try:
        recover_stale(conn, now=now, stale_after_seconds=stale_after_seconds)
        live = conn.execute(
            "SELECT id FROM sessions WHERE visitor_id = ? AND status = 'active' LIMIT 1",
            (visitor_id,),
        ).fetchone()
        if live:
            raise SessionError("active_session", "A voice session is already active for this visitor.")
        used = _usage_seconds(conn, visitor_id, _day_start_utc(now), max_seconds, now)
        if used >= daily_allowance_seconds:
            raise SessionError("daily_quota", "Daily voice allowance exhausted.")
        session_id = uuid.uuid4().hex
        conn.execute(
            """INSERT INTO sessions
               (id, visitor_id, ip_hash, status, created_at, started_at, max_seconds, last_seen_at)
               VALUES (?, ?, ?, 'active', ?, ?, ?, ?)""",
            (session_id, visitor_id, ip_hash, now, now, max_seconds, now),
        )
        _commit(conn)
    except BaseException:
        _rollback(conn)
        raise
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    assert row is not None
    return dict(row)


def touch_session(conn: sqlite3.Connection, session_id: str, now: float) -> None:
    conn.execute(
        "UPDATE sessions SET last_seen_at = ? WHERE id = ? AND status = 'active'", (now, session_id)
    )
    conn.commit()


def end_session(conn: sqlite3.Connection, session_id: str, now: float, reason: str) -> dict | None:
    """Idempotent close. Returns the settled row, or None if unknown or
    already closed (callers treat both as success)."""
    _begin(conn)
    try:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None or row["status"] != "active":
            _rollback(conn)
            return None
        conn.execute(
            "UPDATE sessions SET status='ended', end_reason=?, ended_at=?, last_seen_at=? WHERE id = ?",
            (reason, now, now, session_id),
        )
        _commit(conn)
    except BaseException:
        _rollback(conn)
        raise
    return get_session(conn, session_id)


def get_session(conn: sqlite3.Connection, session_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row is not None else None


def record_attempt(conn: sqlite3.Connection, ip_hash: str, now: float, window_seconds: int) -> None:
    conn.execute("DELETE FROM attempts WHERE ts < ?", (now - window_seconds,))
    conn.execute("INSERT INTO attempts (ip_hash, ts) VALUES (?, ?)", (ip_hash, now))
    conn.commit()


def count_attempts(conn: sqlite3.Connection, ip_hash: str, now: float, window_seconds: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM attempts WHERE ip_hash = ? AND ts >= ?", (ip_hash, now - window_seconds)
    ).fetchone()
    return int(row[0])


def usage_today(conn: sqlite3.Connection, visitor_id: str, now: float, max_seconds: int) -> float:
    return _usage_seconds(conn, visitor_id, _day_start_utc(now), max_seconds, now)


def current_time() -> float:
    return time.time()
