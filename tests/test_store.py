"""Contract tests: session lifecycle, quotas, stale recovery (in-memory SQLite)."""
import sys
import os
import sqlite3
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.store import (
    SessionError,
    connect,
    count_attempts,
    create_session,
    end_session,
    get_session,
    record_attempt,
    recover_stale,
    touch_session,
    usage_today,
)

NOW = 1_700_000_000.0


def fresh_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
CREATE TABLE sessions (
  id TEXT PRIMARY KEY, visitor_id TEXT NOT NULL, ip_hash TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active', created_at REAL NOT NULL,
  started_at REAL, ended_at REAL, max_seconds INTEGER NOT NULL,
  last_seen_at REAL NOT NULL, end_reason TEXT NOT NULL DEFAULT '');
CREATE TABLE attempts (ip_hash TEXT NOT NULL, ts REAL NOT NULL);
""")
    return conn


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.db = fresh_db()

    def tearDown(self):
        self.db.close()

    def _create(self, visitor="v1", **kw):
        args = dict(visitor_id=visitor, ip_hash="h", now=NOW, max_seconds=300,
                    daily_allowance_seconds=600, stale_after_seconds=600)
        args.update(kw)
        return create_session(self.db, **args)

    def test_create_and_lookup(self):
        row = self._create()
        self.assertEqual(row["status"], "active")
        self.assertIsNotNone(get_session(self.db, row["id"]))
        self.assertIsNone(get_session(self.db, "nope"))

    def test_second_concurrent_session_rejected(self):
        self._create()
        with self.assertRaises(SessionError) as ctx:
            self._create()
        self.assertEqual(ctx.exception.code, "active_session")

    def test_end_is_idempotent_and_credits_usage(self):
        row = self._create()
        settled = end_session(self.db, row["id"], NOW + 100, "user_ended")
        assert settled is not None
        self.assertEqual(settled["status"], "ended")
        self.assertEqual(end_session(self.db, row["id"], NOW + 100, "user_ended"), None)
        self.assertEqual(end_session(self.db, "missing", NOW, "user_ended"), None)
        self.assertAlmostEqual(usage_today(self.db, "v1", NOW + 100, 300), 100.0)

    def test_daily_quota_blocks_but_counts_capped_sessions(self):
        for _ in range(2):
            row = self._create(visitor="quota")
            end_session(self.db, row["id"], NOW + 600, "user_ended")
        # Each 600s session counts capped at max_seconds=300: 2*300 = 600.
        with self.assertRaises(SessionError) as ctx:
            self._create(visitor="quota")
        self.assertEqual(ctx.exception.code, "daily_quota")

    def test_stale_sessions_expire_and_free_the_visitor(self):
        old = NOW - 3600
        self.db.execute(
            "INSERT INTO sessions (id, visitor_id, ip_hash, status, created_at, started_at, max_seconds, last_seen_at)"
            " VALUES ('old1','stale','h','active',?,?,300,?)", (old, old, old))
        self.db.commit()
        self.db.commit()
        self.assertEqual(recover_stale(self.db, now=NOW, stale_after_seconds=600), 1)
        row = get_session(self.db, "old1")
        assert row is not None
        self.assertEqual(row["status"], "expired")
        # Fresh session allowed afterwards; stale usage still counted.
        self._create(visitor="stale")
        self.assertGreater(usage_today(self.db, "stale", NOW, 300), 0)

    def test_touch_and_attempt_accounting(self):
        row = self._create()
        touch_session(self.db, row["id"], NOW + 5)
        self.assertEqual(get_session(self.db, row["id"])["last_seen_at"], NOW + 5)
        record_attempt(self.db, "ip1", NOW, 600)
        record_attempt(self.db, "ip1", NOW + 1, 600)
        self.assertEqual(count_attempts(self.db, "ip1", NOW + 2, 600), 2)
        self.assertEqual(count_attempts(self.db, "ip1", NOW + 602, 600), 0)
        self.assertEqual(count_attempts(self.db, "other", NOW + 2, 600), 0)


if __name__ == "__main__":
    unittest.main()
