import json
import os
import sqlite3
import time
from contextlib import contextmanager


class JobQueue:
    """Durable SQLite queue for Gmail attachment download jobs."""

    def __init__(self, db_path):
        self.db_path = os.path.abspath(db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self):
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'processing', 'success', 'failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 8,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    result_json TEXT,
                    ignored INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_runnable
                    ON jobs(status, ignored, next_attempt_at, id);
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )

    def enqueue(self, job_key, payload, max_attempts=8):
        now = time.time()
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO jobs
                    (job_key, payload_json, status, attempts, max_attempts,
                     next_attempt_at, ignored, created_at, updated_at)
                VALUES (?, ?, 'pending', 0, ?, 0, 0, ?, ?)
                """,
                (job_key, payload_json, int(max_attempts), now, now),
            )
            return cur.rowcount == 1

    def claim_next(self, now=None):
        now = time.time() if now is None else float(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM jobs
                WHERE status = 'pending' AND ignored = 0 AND next_attempt_at <= ?
                ORDER BY next_attempt_at ASC, id ASC
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE jobs
                SET status = 'processing', attempts = attempts + 1, updated_at = ?
                WHERE id = ? AND status = 'pending' AND ignored = 0
                """,
                (now, row["id"]),
            )
            claimed = conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
            return self._row_to_job(claimed)

    def claim_job(self, job_id, now=None):
        """claim_next for one given job, with the same guards: it must be
        pending, not ignored and due (next_attempt_at <= now). Returns None
        when the job is not claimable."""
        now = time.time() if now is None else float(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                UPDATE jobs
                SET status = 'processing', attempts = attempts + 1, updated_at = ?
                WHERE id = ? AND status = 'pending' AND ignored = 0 AND next_attempt_at <= ?
                """,
                (now, int(job_id), now),
            )
            if cur.rowcount != 1:
                return None
            claimed = conn.execute("SELECT * FROM jobs WHERE id = ?", (int(job_id),)).fetchone()
            return self._row_to_job(claimed)

    def mark_success(self, job_id, result=None):
        """Mark only an active, non-ignored job successful."""
        now = time.time()
        result_json = json.dumps(result or {}, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE jobs
                SET status = 'success', result_json = ?, last_error = NULL,
                    ignored = 0, next_attempt_at = 0, updated_at = ?
                WHERE id = ? AND status IN ('pending', 'processing') AND ignored = 0
                """,
                (result_json, now, int(job_id)),
            )
            return cur.rowcount == 1

    def mark_failure(self, job_id, error, retry_delays=(60, 300, 900, 1800, 3600, 7200)):
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT attempts, max_attempts FROM jobs WHERE id = ?",
                (int(job_id),),
            ).fetchone()
            if row is None:
                return "missing"
            attempts = int(row["attempts"])
            max_attempts = int(row["max_attempts"])
            if attempts >= max_attempts:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'failed', last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (str(error)[:4000], now, int(job_id)),
                )
                return "failed"

            index = min(max(attempts - 1, 0), len(retry_delays) - 1)
            delay = float(retry_delays[index]) if retry_delays else 60.0
            conn.execute(
                """
                UPDATE jobs
                SET status = 'pending', next_attempt_at = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (now + delay, str(error)[:4000], now, int(job_id)),
            )
            return "retry"

    def defer_job(self, job_id, error, delay=900):
        """Retry later without consuming an attempt (authentication/config outage)."""
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE jobs
                SET status = 'pending', attempts = CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END,
                    next_attempt_at = ?, last_error = ?, updated_at = ?
                WHERE id = ? AND status = 'processing' AND ignored = 0
                """,
                (now + max(60, float(delay)), str(error)[:4000], now, int(job_id)),
            )
            return cur.rowcount == 1

    def retry_job(self, job_id):
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE jobs
                SET status = 'pending', ignored = 0, attempts = 0,
                    next_attempt_at = 0, last_error = NULL, updated_at = ?
                WHERE id = ? AND status = 'failed'
                """,
                (now, int(job_id)),
            )
            return cur.rowcount == 1

    def retry_success_job(self, job_id):
        """Explicit user-requested re-download of a recent successful attachment."""
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM jobs WHERE id = ? AND status = 'success'",
                (int(job_id),),
            ).fetchone()
            if row is None or row["payload_json"] == "{}":
                return False
            cur = conn.execute(
                """
                UPDATE jobs
                SET status = 'pending', ignored = 0, attempts = 0,
                    next_attempt_at = 0, last_error = NULL, result_json = NULL, updated_at = ?
                WHERE id = ? AND status = 'success'
                """,
                (now, int(job_id)),
            )
            return cur.rowcount == 1

    def ignore_job(self, job_id):
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE jobs SET ignored = 1, updated_at = ? WHERE id = ? AND status = 'failed'",
                (now, int(job_id)),
            )
            return cur.rowcount == 1

    def unignore_job(self, job_id):
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE jobs SET ignored = 0, updated_at = ? WHERE id = ? AND status = 'failed'",
                (now, int(job_id)),
            )
            return cur.rowcount == 1

    def list_failed(self, limit=200, include_ignored=False):
        condition = "status = 'failed'" if include_ignored else "status = 'failed' AND ignored = 0"
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM jobs WHERE {condition} ORDER BY updated_at DESC, id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def list_recent_success(self, limit=200):
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM jobs
                WHERE status = 'success' AND payload_json != '{}'
                ORDER BY updated_at DESC, id DESC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def get_job(self, job_id):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (int(job_id),)).fetchone()
        return self._row_to_job(row)

    def get_job_by_key(self, job_key):
        """Like get_job, plus "result": the recorded save result of a success
        (None when there is none, e.g. after retention compaction)."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_key = ?", (job_key,)).fetchone()
        job = self._row_to_job(row)
        if job is not None:
            job["result"] = json.loads(row["result_json"]) if row["result_json"] else None
        return job

    def recover_processing_jobs(self):
        """Processing rows are stale after a monitor restart; the claim itself is not an attempt."""
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE jobs
                SET status = 'pending', next_attempt_at = ?,
                    attempts = CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END,
                    last_error = COALESCE(last_error || ' | ', '') || 'Recovered after monitor restart',
                    updated_at = ?
                WHERE status = 'processing' AND ignored = 0
                """,
                (now, now),
            )
            return cur.rowcount

    def apply_retention(self, now=None, full_days=90, dedupe_days=730):
        """Expire attachment dedupe records first, then compact the successes that remain.

        Deleting before compacting keeps the two counters disjoint and avoids
        rewriting rows that are about to be removed in the same pass.
        """
        now = time.time() if now is None else float(now)
        full_cutoff = now - max(1, int(full_days)) * 86400
        dedupe_cutoff = now - max(int(full_days) + 1, int(dedupe_days)) * 86400
        with self._connect() as conn:
            deleted = conn.execute(
                "DELETE FROM jobs WHERE status = 'success' AND updated_at < ?",
                (dedupe_cutoff,),
            ).rowcount
            compacted = conn.execute(
                """
                UPDATE jobs
                SET payload_json = '{}', result_json = NULL, last_error = NULL
                WHERE status = 'success' AND updated_at < ?
                  AND (payload_json != '{}' OR result_json IS NOT NULL OR last_error IS NOT NULL)
                """,
                (full_cutoff,),
            ).rowcount
        return {"compacted": compacted, "deleted": deleted}

    def get_metadata(self, key, default=None):
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
            return default if row is None else row["value"]

    def set_metadata(self, key, value):
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO metadata(key, value, updated_at) VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, str(value), now),
            )

    def delete_metadata(self, key):
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM metadata WHERE key = ?", (key,))
            return cur.rowcount == 1

    def counts(self):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, ignored, COUNT(*) AS count FROM jobs GROUP BY status, ignored"
            ).fetchall()
        result = {"pending": 0, "processing": 0, "success": 0, "failed": 0, "ignored": 0}
        for row in rows:
            count = int(row["count"])
            if int(row["ignored"]):
                result["ignored"] += count
            else:
                result[row["status"]] += count
        return result

    @staticmethod
    def _row_to_job(row):
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "payload": json.loads(row["payload_json"]),
            "status": row["status"],
            "attempts": int(row["attempts"]),
            "max_attempts": int(row["max_attempts"]),
            "next_attempt_at": float(row["next_attempt_at"]),
            "last_error": row["last_error"],
            "ignored": bool(row["ignored"]),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }
