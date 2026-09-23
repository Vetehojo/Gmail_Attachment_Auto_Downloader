import os
import tempfile
import unittest

from job_queue import JobQueue


class JobQueueTest(unittest.TestCase):
    def test_dedup_claim_retry_success(self):
        with tempfile.TemporaryDirectory() as td:
            queue = JobQueue(os.path.join(td, "jobs.sqlite3"))
            payload = {"message_id": "m1", "attachment_id": "a1"}
            self.assertTrue(queue.enqueue("attachment:m1:a1", payload, max_attempts=3))
            self.assertFalse(queue.enqueue("attachment:m1:a1", payload, max_attempts=3))

            job = queue.claim_next(now=100)
            self.assertEqual(1, job["attempts"])
            self.assertEqual("retry", queue.mark_failure(job["id"], "boom", retry_delays=(1,)))
            self.assertIsNone(queue.claim_next(now=100))

            job = queue.claim_next(now=10**12)
            self.assertEqual(2, job["attempts"])
            self.assertTrue(queue.mark_success(job["id"], {"ok": True}))
            self.assertEqual(1, queue.counts()["success"])

    def test_auth_defer_does_not_consume_attempt(self):
        with tempfile.TemporaryDirectory() as td:
            queue = JobQueue(os.path.join(td, "jobs.sqlite3"))
            queue.enqueue("attachment:m1:x", {"x": 1})
            job = queue.claim_next(now=100)
            self.assertEqual(1, job["attempts"])
            self.assertTrue(queue.defer_job(job["id"], "auth required", delay=900))
            deferred = queue.get_job(job["id"])
            self.assertEqual("pending", deferred["status"])
            self.assertEqual(0, deferred["attempts"])
            self.assertGreater(deferred["next_attempt_at"], 100)

    def test_recover_processing_restores_attempt(self):
        with tempfile.TemporaryDirectory() as td:
            queue = JobQueue(os.path.join(td, "jobs.sqlite3"))
            queue.enqueue("attachment:m1:x", {"x": 1})
            job = queue.claim_next()
            self.assertEqual(1, job["attempts"])
            self.assertEqual(1, queue.recover_processing_jobs())
            recovered = queue.get_job(job["id"])
            self.assertEqual(0, recovered["attempts"])
            self.assertEqual("pending", recovered["status"])

    def test_ignored_job_can_be_shown_unignored_and_retried(self):
        with tempfile.TemporaryDirectory() as td:
            queue = JobQueue(os.path.join(td, "jobs.sqlite3"))
            queue.enqueue("attachment:m2:x", {"mail_subject": "sample"}, max_attempts=1)
            job = queue.claim_next()
            self.assertEqual("failed", queue.mark_failure(job["id"], "broken"))
            self.assertTrue(queue.ignore_job(job["id"]))
            self.assertEqual([], queue.list_failed())
            all_failed = queue.list_failed(include_ignored=True)
            self.assertEqual(1, len(all_failed))
            self.assertTrue(all_failed[0]["ignored"])
            self.assertTrue(queue.unignore_job(job["id"]))
            self.assertEqual(1, len(queue.list_failed()))
            self.assertTrue(queue.retry_job(job["id"]))
            self.assertEqual("pending", queue.get_job(job["id"])["status"])

    def test_mark_success_does_not_resurrect_failed_job(self):
        with tempfile.TemporaryDirectory() as td:
            queue = JobQueue(os.path.join(td, "jobs.sqlite3"))
            queue.enqueue("attachment:m1:x", {"x": 1}, max_attempts=1)
            job = queue.claim_next()
            self.assertEqual("failed", queue.mark_failure(job["id"], "broken"))
            self.assertFalse(queue.mark_success(job["id"], {"ok": True}))
            self.assertEqual("failed", queue.get_job(job["id"])["status"])

    def test_recent_success_can_be_explicitly_requeued(self):
        with tempfile.TemporaryDirectory() as td:
            queue = JobQueue(os.path.join(td, "jobs.sqlite3"))
            queue.enqueue("attachment:m1:x", {"message_id": "m1"})
            job = queue.claim_next()
            queue.mark_success(job["id"], {"target": "x"})
            self.assertEqual(1, len(queue.list_recent_success()))
            self.assertTrue(queue.retry_success_job(job["id"]))
            retried = queue.get_job(job["id"])
            self.assertEqual("pending", retried["status"])
            self.assertEqual(0, retried["attempts"])

    def test_success_retention_compacts_then_deletes(self):
        with tempfile.TemporaryDirectory() as td:
            queue = JobQueue(os.path.join(td, "jobs.sqlite3"))
            now = 2_000_000_000.0

            queue.enqueue("old-delete", {"old": "payload"})
            old_job = queue.claim_next(now=now)
            queue.mark_success(old_job["id"], {"old": "result"})

            queue.enqueue("old-compact", {"large": "payload"})
            compact_job = queue.claim_next(now=now)
            queue.mark_success(compact_job["id"], {"large": "result"})

            with queue._connect() as conn:
                conn.execute(
                    "UPDATE jobs SET updated_at = ? WHERE id = ?",
                    (now - 800 * 86400, old_job["id"]),
                )
                conn.execute(
                    "UPDATE jobs SET updated_at = ? WHERE id = ?",
                    (now - 100 * 86400, compact_job["id"]),
                )

            result = queue.apply_retention(now=now, full_days=90, dedupe_days=730)
            self.assertEqual(1, result["compacted"])
            self.assertEqual(1, result["deleted"])
            with queue._connect() as conn:
                compacted = conn.execute(
                    "SELECT payload_json, result_json FROM jobs WHERE id = ?",
                    (compact_job["id"],),
                ).fetchone()
                deleted = conn.execute(
                    "SELECT 1 FROM jobs WHERE id = ?",
                    (old_job["id"],),
                ).fetchone()
            self.assertEqual("{}", compacted["payload_json"])
            self.assertIsNone(compacted["result_json"])
            self.assertIsNone(deleted)


if __name__ == "__main__":
    unittest.main()
