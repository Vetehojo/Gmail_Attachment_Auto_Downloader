"""K2: a stuck attachment worker is detected by the tray and the watchdog.

Reproduces the external review's probe worker_liveness (pending=0,
processing=1, worker heartbeat an hour old used to report nothing) and the
watchdog gap (heartbeat.json, which the scanner keeps fresh, hid a stuck
worker), and pins the step-boundary worker heartbeat, the stall restart that
counts the hung attempt, and one customer notice per incident.
"""
import base64
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

import gmail_app
import gmail_monitor
import runtime_state
import watchdog
from job_queue import JobQueue


class FakeQueue:
    def __init__(self, metadata):
        self.metadata = metadata

    def get_metadata(self, key, default=None):
        return self.metadata.get(key, default)


def worker_issue(metadata, counts):
    with mock.patch.object(gmail_app, "queue", return_value=FakeQueue(metadata)):
        return gmail_app.TrayApp._worker_issue(None, counts)


class TrayWorkerIssueTest(unittest.TestCase):
    KEY = runtime_state.WORKER_HEARTBEAT_KEY

    def heartbeat(self, age):
        return {self.KEY: str(time.time() - age)}

    def test_worker_liveness_probe_is_fixed(self):
        stale = self.heartbeat(3600)
        self.assertNotEqual("", worker_issue(stale, {"pending": 0, "processing": 1}))
        self.assertNotEqual("", worker_issue(stale, {"pending": 1, "processing": 1}))

    def test_a_long_download_is_not_reported_while_a_job_is_processing(self):
        ten_minutes = self.heartbeat(10 * 60)
        self.assertEqual("", worker_issue(ten_minutes, {"pending": 0, "processing": 1}))
        self.assertEqual("", worker_issue(ten_minutes, {"pending": 5, "processing": 1}))
        too_long = self.heartbeat(runtime_state.WORKER_STALL_SECONDS + 60)
        self.assertNotEqual("", worker_issue(too_long, {"pending": 5, "processing": 1}))

    def test_waiting_jobs_alone_keep_the_short_limit(self):
        self.assertNotEqual("", worker_issue(self.heartbeat(6 * 60), {"pending": 1, "processing": 0}))
        self.assertEqual("", worker_issue(self.heartbeat(4 * 60), {"pending": 1, "processing": 0}))

    def test_nothing_to_do_or_a_missing_heartbeat(self):
        self.assertEqual("", worker_issue(self.heartbeat(86400), {"pending": 0, "processing": 0}))
        self.assertIn("記録がありません", worker_issue({}, {"pending": 0, "processing": 1}))

    def test_the_text_does_not_change_with_the_counts_during_one_stall(self):
        stale = self.heartbeat(3600)
        texts = {
            worker_issue(stale, counts)
            for counts in ({"pending": 1, "processing": 1}, {"pending": 9, "processing": 1},
                           {"pending": 12, "processing": 0})
        }
        self.assertEqual(1, len(texts))

    def test_a_worker_error_is_reported_as_is(self):
        metadata = dict(self.heartbeat(0), **{gmail_app.WORKER_ERROR_KEY: "thread died"})
        self.assertEqual("thread died", worker_issue(metadata, {"pending": 0, "processing": 0}))


class Execute:
    def __init__(self, value):
        self.value = value

    def execute(self, num_retries=None):
        return self.value


class CountingService:
    def __init__(self, data):
        self.data = data
        self.calls = 0

    def users(self):
        return self

    def messages(self):
        return self

    def attachments(self):
        return self

    def get(self, **kwargs):
        self.calls += 1
        return Execute({"data": base64.urlsafe_b64encode(self.data).decode("ascii")})


class StaticPool:
    def __init__(self, service):
        self.service = service

    def get(self, account_email):
        return self.service

    def invalidate(self, account_email):
        pass


class RecordingQueue(JobQueue):
    def __init__(self, path, probe, fail_beats=False):
        super().__init__(path)
        self.probe = probe
        self.fail_beats = fail_beats
        self.beats = []

    def set_metadata(self, key, value):
        if key == gmail_monitor.WORKER_HEARTBEAT_KEY:
            self.beats.append(self.probe())
            if self.fail_beats:
                raise sqlite3.OperationalError("database is locked")
        super().set_metadata(key, value)


class MonitorWorkerHeartbeatTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = temp.name
        self.state = os.path.join(self.root, "state")
        self.final = os.path.join(self.root, "final")
        os.makedirs(self.state)
        self.log_file = os.path.join(self.root, "log", "mail_log.txt")
        for patcher in (
            mock.patch.object(gmail_monitor, "STATE_DIR", self.state),
            mock.patch.object(gmail_monitor, "QUEUE_DB", os.path.join(self.state, "jobs.sqlite3")),
            mock.patch.object(gmail_monitor, "HEARTBEAT_FILE", os.path.join(self.state, "heartbeat.json")),
            mock.patch.object(gmail_monitor, "LOG_FILE", self.log_file),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.service = CountingService(b"attachment")

    def probe(self):
        """(attachment fetches, saved files, journal phase) at a heartbeat."""
        saved = [n for n in os.listdir(self.final) if not n.startswith(".")] if os.path.isdir(self.final) else []
        journals = [n for n in os.listdir(self.state) if n.startswith("attachmentjob_")]
        phase = None
        if journals:
            with open(os.path.join(self.state, journals[0]), encoding="utf-8") as handle:
                phase = json.load(handle)["phase"]
        return self.service.calls, len(saved), phase

    def enqueue(self, queue):
        queue.enqueue("attachment:a@example.com:m1:x", {
            "account_email": "a@example.com",
            "final_dir": self.final,
            "message_id": "m1",
            "attachment_id": "att1",
            "part_id": "",
            "inline": False,
            "filename": "document.pdf",
            "received_date": "20260825",
            "sender_email": "sender@example.com",
            "mail_subject": "subject",
        })

    def test_every_step_of_a_job_refreshes_the_worker_heartbeat(self):
        queue = RecordingQueue(os.path.join(self.state, "jobs.sqlite3"), self.probe)
        self.enqueue(queue)
        self.assertTrue(gmail_monitor.process_one_job(queue, StaticPool(self.service)))

        self.assertEqual("success", queue.get_job(1)["status"])
        self.assertEqual(
            [
                (0, 0, None),        # claimed
                (1, 0, None),        # fetched
                (1, 1, "prepared"),  # placed
                (1, 1, "prepared"),  # verified
            ],
            queue.beats,
        )
        self.assertGreater(float(queue.get_metadata(gmail_monitor.WORKER_HEARTBEAT_KEY)), time.time() - 60)

    def test_a_failed_heartbeat_write_never_fails_the_job(self):
        queue = RecordingQueue(os.path.join(self.state, "jobs.sqlite3"), self.probe, fail_beats=True)
        self.enqueue(queue)
        self.assertTrue(gmail_monitor.process_one_job(queue, StaticPool(self.service)))

        job = queue.get_job(1)
        self.assertEqual("success", job["status"])
        self.assertEqual(1, job["attempts"])
        with open(self.log_file, encoding="utf-8") as handle:
            self.assertIn("Worker heartbeat write failed: database is locked", handle.read())

    def test_monitor_start_refreshes_the_worker_heartbeat_before_the_startup_work(self):
        queue_db = os.path.join(self.state, "jobs.sqlite3")
        JobQueue(queue_db).set_metadata(gmail_monitor.WORKER_HEARTBEAT_KEY, time.time() - 7 * 86400)
        seen = []

        def recover(queue):
            seen.append(float(queue.get_metadata(gmail_monitor.WORKER_HEARTBEAT_KEY)))
            raise KeyboardInterrupt

        instance = mock.Mock()
        instance.acquire.return_value = True
        with mock.patch.object(sys, "argv", ["gmail_monitor.py"]), \
             mock.patch.object(gmail_monitor, "SingleInstance", return_value=instance), \
             mock.patch.object(gmail_monitor, "recover_interrupted_jobs", side_effect=recover):
            self.assertEqual(0, gmail_monitor.main())

        self.assertEqual(1, len(seen))
        self.assertGreater(seen[0], time.time() - 60)
        instance.release.assert_called_once()


class StalledJobReleaseTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.queue = JobQueue(os.path.join(temp.name, "jobs.sqlite3"))

    def test_the_stalled_claim_counts_as_an_attempt(self):
        self.queue.enqueue("a", {}, max_attempts=2)
        self.queue.enqueue("b", {}, max_attempts=2)
        first = self.queue.claim_next()
        waiting = self.queue.get_job(2)

        self.assertEqual({first["id"]: "retry"}, self.queue.fail_stalled_jobs("hung"))
        job = self.queue.get_job(first["id"])
        self.assertEqual("pending", job["status"])
        self.assertEqual(1, job["attempts"])
        self.assertGreater(job["next_attempt_at"], time.time())
        self.assertEqual("hung", job["last_error"])
        self.assertEqual(waiting, self.queue.get_job(2))
        self.assertEqual(0, self.queue.recover_processing_jobs())

        self.queue.claim_job(first["id"], now=time.time() + 3600)
        self.assertEqual({first["id"]: "failed"}, self.queue.fail_stalled_jobs("hung again"))
        job = self.queue.get_job(first["id"])
        self.assertEqual("failed", job["status"])
        self.assertEqual("hung again", job["last_error"])


class WatchdogWorkerStallTest(unittest.TestCase):
    """watchdog.main() against a temp queue/state with the process layer mocked:
    one owned monitor process, and heartbeat.json always fresh (the scanner)."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = temp.name
        state = os.path.join(root, "state")
        os.makedirs(state)
        self.queue_db = os.path.join(state, "jobs.sqlite3")
        self.heartbeat_file = os.path.join(state, "heartbeat.json")
        self.state_file = os.path.join(state, "watchdog_state.json")
        self.watchdog_log = os.path.join(root, "log", "watchdog_log.txt")
        self.stop = mock.Mock(return_value=True)
        self.start = mock.Mock(side_effect=self.new_monitor)
        self.notify = mock.Mock()
        for patcher in (
            mock.patch.object(watchdog, "STATE_DIR", state),
            mock.patch.object(watchdog, "QUEUE_DB", self.queue_db),
            mock.patch.object(watchdog, "HEARTBEAT_FILE", self.heartbeat_file),
            mock.patch.object(watchdog, "STATE_FILE", self.state_file),
            mock.patch.object(watchdog, "WATCHDOG_LOG", self.watchdog_log),
            mock.patch.object(watchdog, "boot_timestamp", return_value=time.time() - 86400),
            mock.patch.object(watchdog, "monitor_pids", return_value=[4242]),
            mock.patch.object(watchdog, "stop_monitor", self.stop),
            mock.patch.object(watchdog, "start_monitor", self.start),
            mock.patch.object(watchdog, "notify", self.notify),
            mock.patch.object(watchdog, "event_log"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.queue = JobQueue(self.queue_db)

    def new_monitor(self):
        """A restarted monitor recovers its jobs and beats at once."""
        self.queue.recover_processing_jobs()
        self.worker_heartbeat(0)
        return True

    def worker_heartbeat(self, age):
        self.queue.set_metadata(runtime_state.WORKER_HEARTBEAT_KEY, time.time() - age)

    def run_watchdog(self):
        runtime_state.atomic_write_json(self.heartbeat_file, {"timestamp": time.time(), "status": "ok"})
        self.assertEqual(0, watchdog.main())
        with open(self.state_file, encoding="utf-8") as handle:
            return json.load(handle)

    def stuck_job(self, max_attempts=8):
        self.queue.enqueue(f"job-{max_attempts}", {}, max_attempts=max_attempts)
        job = self.queue.claim_next()
        self.worker_heartbeat(3600)
        return job

    def read_log(self):
        with open(self.watchdog_log, encoding="utf-8") as handle:
            return handle.read()

    def test_stuck_worker_is_restarted_although_heartbeat_json_is_fresh(self):
        job = self.stuck_job()

        state = self.run_watchdog()
        self.assertEqual(1, state["worker_stall_count"])
        self.stop.assert_not_called()
        self.assertIn("observation 1/2", self.read_log())

        state = self.run_watchdog()
        self.stop.assert_called_once()
        self.start.assert_called_once()
        self.assertNotIn("worker_stall_count", state)
        released = self.queue.get_job(job["id"])
        self.assertEqual("pending", released["status"])
        self.assertEqual(1, released["attempts"])  # the killed attempt counts
        self.assertGreater(released["next_attempt_at"], time.time())
        self.assertIn("応答しなくなった", released["last_error"])
        self.assertIn("job 1 retry", self.read_log())

        # The restarted monitor beats: no further restart (no flapping).
        for _ in range(3):
            state = self.run_watchdog()
        self.stop.assert_called_once()
        self.assertNotIn("worker_stall_count", state)

    def test_a_job_that_hangs_on_every_try_ends_as_a_visible_failure(self):
        job = self.stuck_job(max_attempts=2)
        self.run_watchdog()
        self.run_watchdog()
        self.assertEqual("pending", self.queue.get_job(job["id"])["status"])

        self.queue.claim_job(job["id"], now=time.time() + 3600)
        self.worker_heartbeat(3600)
        self.run_watchdog()
        self.run_watchdog()
        failed = self.queue.get_job(job["id"])
        self.assertEqual("failed", failed["status"])
        self.assertEqual(2, failed["attempts"])
        self.assertEqual(1, self.queue.counts()["failed"])

    def test_a_long_download_or_an_idle_worker_is_left_alone(self):
        self.queue.enqueue("job", {})
        self.queue.claim_next()
        self.worker_heartbeat(10 * 60)
        for _ in range(3):
            state = self.run_watchdog()
        self.stop.assert_not_called()
        self.assertNotIn("worker_stall_count", state)

        self.queue.mark_success(1, {})
        self.worker_heartbeat(86400)  # nothing waiting: an old heartbeat means nothing
        for _ in range(3):
            self.run_watchdog()
        self.stop.assert_not_called()

    def test_paused_or_settings_update_skips_the_worker_check(self):
        self.stuck_job()
        self.queue.set_metadata(watchdog.PAUSED_KEY, "1")
        for _ in range(3):
            self.run_watchdog()
        self.queue.set_metadata(watchdog.PAUSED_KEY, "0")
        runtime_state.begin_settings_update(self.queue)
        for _ in range(3):
            self.run_watchdog()
        self.stop.assert_not_called()
        self.start.assert_not_called()

    def customer_alerts(self):
        return [c.kwargs.get("customer_alert", False) for c in self.notify.call_args_list]

    def test_one_customer_notice_per_incident(self):
        self.stop.return_value = False  # ownership cannot be verified: restart aborted
        self.stuck_job()
        for _ in range(6):
            self.run_watchdog()
        self.assertEqual(3, self.stop.call_count)
        self.assertEqual([True, False, False], self.customer_alerts())

        self.worker_heartbeat(0)  # healthy again: the incident is over
        state = self.run_watchdog()
        self.assertNotIn(watchdog.NOTICE_SENT_KEY, state)

        self.worker_heartbeat(3600)
        self.run_watchdog()
        self.run_watchdog()
        self.assertEqual([True, False, False, True], self.customer_alerts())

    def test_restart_cap_alerts_once_per_incident(self):
        now = time.time()
        state = {"restart_times": [now, now, now]}
        with mock.patch.object(watchdog, "is_paused", return_value=False), \
             mock.patch.object(watchdog, "is_settings_update", return_value=False):
            for _ in range(4):
                watchdog.perform_restart(state, "monitor process not found", now)
        self.assertEqual([True], self.customer_alerts())
        self.assertEqual(4, self.read_log().count("restart suppressed"))
        self.start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
