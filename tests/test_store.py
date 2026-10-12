"""Contract tests: session lifecycle, quotas, stale recovery (in-memory SQLite)."""
import sys
import os
import sqlite3
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.store import (
    SessionError,
    connect as store_connect,
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


class ConcurrencyTest(unittest.TestCase):
    """Mirrors production discipline: one shared connection, one lock,
    real threads racing the same visitor — exactly one may win."""

    def test_simultaneous_creates_yield_exactly_one_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = store_connect(os.path.join(tmp, "race.sqlite3"))
            try:
                lock = threading.Lock()
                barrier = threading.Barrier(8)
                results = []

                def attempt():
                    try:
                        barrier.wait(timeout=15)
                        with lock:
                            create_session(db, visitor_id="racer", ip_hash="h", now=NOW,
                                           max_seconds=300, daily_allowance_seconds=600,
                                           stale_after_seconds=600)
                        results.append("ok")
                    except SessionError as exc:
                        results.append(exc.code)
                    except Exception as exc:  # noqa: BLE001 — must surface, never pass silently
                        results.append(f"unexpected:{type(exc).__name__}: {exc}")

                threads = [threading.Thread(target=attempt) for _ in range(8)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=30)
                    self.assertFalse(t.is_alive())
                self.assertEqual(results.count("ok"), 1, results)
                self.assertTrue(all(r == "active_session" for r in results if r != "ok"), results)
                rows = db.execute("SELECT COUNT(*) FROM sessions WHERE visitor_id='racer'").fetchone()
                self.assertEqual(rows[0], 1)
            finally:
                db.close()

    def test_concurrent_quota_spend_never_exceeds_allowance(self):
        # 600s allowance, 300s cap per session: even raced back-to-back
        # creates cannot bank more than the allowance.
        with tempfile.TemporaryDirectory() as tmp:
            db = store_connect(os.path.join(tmp, "quota.sqlite3"))
            try:
                lock = threading.Lock()
                barrier = threading.Barrier(4)
                outcomes = []

                def attempt(i):
                    try:
                        barrier.wait(timeout=15)
                        with lock:
                            row = create_session(db, visitor_id="q", ip_hash="h", now=NOW + i,
                                                 max_seconds=300, daily_allowance_seconds=600,
                                                 stale_after_seconds=600)
                        with lock:
                            end_session(db, row["id"], NOW + i + 600, "user_ended")
                        outcomes.append("ok")
                    except SessionError as exc:
                        outcomes.append(exc.code)

                threads = [threading.Thread(target=attempt, args=(i,)) for i in range(4)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=30)
                with lock:
                    total = usage_today(db, "q", NOW + 3600, 300)
                self.assertLessEqual(total, 600)
            finally:
                db.close()


class BoundaryTest(unittest.TestCase):
    def setUp(self):
        self.db = fresh_db()

    def tearDown(self):
        self.db.close()

    def test_usage_resets_at_utc_midnight(self):
        day = 1_700_000_000.0 // 86400 * 86400
        self.db.execute(
            "INSERT INTO sessions (id, visitor_id, ip_hash, status, created_at, started_at, ended_at, max_seconds, last_seen_at)"
            " VALUES ('d1','day','h','ended',?,?,?,?,?)",
            (day - 100, day - 100, day - 40, 300, day - 40),
        )
        self.db.commit()
        # Entire session sits before the UTC boundary: counts for nothing today.
        self.assertEqual(usage_today(self.db, "day", day + 10, 300), 0)

    def test_negative_clock_skew_clamps_to_zero(self):
        self.db.execute(
            "INSERT INTO sessions (id, visitor_id, ip_hash, status, created_at, started_at, ended_at, max_seconds, last_seen_at)"
            " VALUES ('sk1','skew','h','ended',?,?,?,?,?)",
            (NOW, NOW, NOW - 50, 300, NOW),
        )
        self.db.commit()
        self.assertEqual(usage_today(self.db, "skew", NOW + 10, 300), 0)

    def test_live_session_counts_elapsed_time(self):
        self.db.execute(
            "INSERT INTO sessions (id, visitor_id, ip_hash, status, created_at, started_at, max_seconds, last_seen_at)"
            " VALUES ('live1','live','h','active',?,?,300,?)", (NOW - 90, NOW - 90, NOW - 10))
        self.db.commit()
        used = usage_today(self.db, "live", NOW, 300)
        self.assertGreaterEqual(used, 80)
        self.assertLessEqual(used, 100)

    def test_restart_preserves_state_for_stale_recovery(self):
        # Simulate a process restart: close and reopen the database file.
        # The live row survives; the next attempt settles it via stale
        # recovery instead of stranding quota or allowing a duplicate.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "restart.sqlite3")
            db1 = store_connect(path)
            try:
                create_session(db1, visitor_id="re", ip_hash="h", now=NOW - 3600,
                               max_seconds=300, daily_allowance_seconds=600,
                               stale_after_seconds=600)
            finally:
                db1.close()
            db2 = store_connect(path)
            try:
                settled = recover_stale(db2, now=NOW, stale_after_seconds=600)
                self.assertEqual(settled, 1)
                row = get_session(
                    db2,
                    db2.execute("SELECT id FROM sessions WHERE visitor_id='re'").fetchone()[0],
                )
                assert row is not None
                self.assertEqual(row["status"], "expired")
                fresh = create_session(db2, visitor_id="re", ip_hash="h", now=NOW,
                                       max_seconds=300, daily_allowance_seconds=600,
                                       stale_after_seconds=600)
                self.assertEqual(fresh["status"], "active")
            finally:
                db2.close()


if __name__ == "__main__":
    unittest.main()
